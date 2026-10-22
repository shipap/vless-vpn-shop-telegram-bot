"""Личный журнал и очередь доставки. Сетевые вызовы выполняются после фиксации события."""

import hashlib
import html
import json
from datetime import datetime, timedelta
from html.parser import HTMLParser
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
from sqlalchemy import and_, func, or_, select, text
from sunrise_core.errors import DomainError, missing

from .db import (
    Account,
    Device,
    DeviceUnlink,
    Notification,
    NotificationDelivery,
    NotificationPreference,
    Order,
    Payment,
    ProviderOperation,
    PushSubscription,
    Quote,
    Setting,
    Subscription,
    now,
    uid,
)

SUPPRESSED_NOTIFICATION_KINDS = frozenset({"checkout_ready", "access_ready"})
TELEGRAM_RECIPIENT_BLOCKS = frozenset({"telegram_recipient_blocked", "telegram_recipient_deactivated"})


def _telegram_result(response):
    """Отрицательный ответ допускает повтор; неизвестный исход требует проверки."""
    try:
        data = response.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return "telegram_delivery_unknown"
    if response.status_code == 200 and data.get("ok") is True:
        result = data.get("result")
        if isinstance(result, dict) and type(result.get("message_id")) is int:
            return None
        return "telegram_delivery_unknown"
    status = response.status_code
    if data.get("ok") is not False or type(data.get("error_code")) is not int or data["error_code"] != status:
        return "telegram_delivery_unknown"
    description = data.get("description")
    description = description.casefold() if isinstance(description, str) else ""
    if status == 403:
        return {
            "forbidden: bot was blocked by the user": "telegram_recipient_blocked",
            "forbidden: user is deactivated": "telegram_recipient_deactivated",
        }.get(description, "telegram_forbidden")
    if status == 400:
        if description == "bad request: chat not found":
            return "telegram_chat_not_found"
        if description.startswith(
            ("bad request: can't parse entities", "bad request: can't find end of the entity")
        ):
            return "telegram_format_rejected"
        return "telegram_rejected"
    if status == 401:
        return "telegram_auth_failed"
    if status == 404:
        return "telegram_endpoint_invalid"
    if status == 429:
        parameters = data.get("parameters")
        seconds = parameters.get("retry_after") if isinstance(parameters, dict) else None
        if type(seconds) is int and 1 <= seconds <= 3600:
            raise _TelegramRetryAfter(seconds)
    if 500 <= status <= 599:
        raise _TelegramRetryAfter(60, code="telegram_temporary_failure")
    return "telegram_delivery_unknown"


def _telegram_plain(body):
    """Сохраняет текст и адреса ссылок при отказе HTML-разметки."""

    class Plain(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts, self.links = [], []

        def handle_data(self, data):
            self.parts.append(data)

        def handle_starttag(self, tag, attrs):
            if tag == "a":
                self.links.append(dict(attrs).get("href", ""))

        def handle_endtag(self, tag):
            if tag == "a" and self.links:
                self.parts.append(" (" + self.links.pop() + ")")

    parser = Plain()
    parser.feed(body)
    parser.close()
    return "".join(parser.parts)


def validate_telegram_content(title, body, nodes=None, *, max_units=4096, title_nodes=None):
    """Проверяет лимит текста после HTML entities, без усечения сообщения."""
    from sunrise_core.telegram_content import render_classic

    rendered = render_classic(nodes, custom_emoji=True) if nodes else html.escape(body)
    if title_nodes:
        from sunrise_core.content import validate_content

        validate_content(title_nodes)
    result = (
        (render_classic(title_nodes, custom_emoji=True) if title_nodes else html.escape(title))
        + "\n\n"
        + rendered
    )

    class TextLength(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.units = 0

        def handle_data(self, data):
            self.units += len(data.encode("utf-16-le")) // 2

    parser = TextLength()
    parser.feed(result)
    parser.close()
    if parser.units > max_units:
        raise DomainError(
            "telegram_message_too_long",
            f"Сообщение слишком длинное для Telegram. Сократите текст до {max_units} символов вместе с заголовком.",
            400,
        )
    return result


def safe_target(value):
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) > 300
        or not value.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or any(ord(c) < 32 for c in value)
    ):
        raise DomainError("notification_target_invalid", "Некорректная ссылка уведомления.", 400)
    return value


class NotificationService:
    def __init__(self, db, settings, *, mail_transport=None, telegram_transport=None, push_transport=None):
        self.db, self.settings = db, settings
        from .settings import runtime_policy

        self.runtime_policy = runtime_policy(settings)
        self.mail_transport, self.telegram_transport, self.push_transport = (
            mail_transport,
            telegram_transport,
            push_transport,
        )

    def resume_telegram(self, account_id, telegram_id):
        """Только для свежего личного сообщения, проверенного адаптером бота."""
        from .accounts import require_account_access_in
        from .db import Alias

        with self.db.transaction() as session:
            account = session.scalar(select(Account).where(Account.id == int(account_id)).with_for_update())
            if (
                not account
                or account.telegram_id is None
                or str(account.telegram_id) != str(telegram_id)
                or session.get(Alias, account.id)
            ):
                raise missing()
            require_account_access_in(session, account, self.runtime_policy)
            account.telegram_notifications_blocked = False
            key = "telegram-activity:" + str(account.id)
            marker = session.get(Setting, key)
            if marker is None:
                marker = Setting(key=key)
                session.add(marker)
            marker.value = {"generation": uid()}
        return {"status": "resumed"}

    def emit_in(
        self,
        session,
        account_id,
        event_key,
        kind,
        title,
        body,
        *,
        target=None,
        nodes=None,
        channel="auto",
        telegram_id=None,
        owner_only=False,
        actions=None,
        allow_push=True,
    ):
        if (
            not all(isinstance(v, str) and v for v in (event_key, kind, title, body))
            or len(event_key) > 200
            or len(title) > 200
            or len(body) > 10000
            or len(kind) > 64
        ):
            raise DomainError("notification_invalid", "Некорректное уведомление.", 400)
        target = safe_target(target)
        if nodes:
            from sunrise_core.content import validate_content

            validate_content(nodes)
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"notification:{account_id}:{event_key}"},
        )
        existing = session.scalar(
            select(Notification).where(
                Notification.account_id == int(account_id), Notification.event_key == event_key
            )
        )
        if existing:
            return existing
        account = session.get(Account, int(account_id))
        if not account:
            raise missing()
        from types import SimpleNamespace

        from .staff_permissions import StaffPermissions

        if (
            owner_only
            and StaffPermissions(SimpleNamespace(db=self.db, settings=self.settings)).role_in(
                session, account
            )
            != "owner"
        ):
            raise DomainError("notification_owner_required", "Уведомление предназначено владельцу.", 403)
        if telegram_id is not None and str(telegram_id) != str(account.telegram_id):
            raise missing()
        item = Notification(
            account_id=account.id,
            event_key=event_key,
            kind=kind,
            audience="owner"
            if owner_only or kind == "identity_review"
            else "staff"
            if kind == "sale" or target and target.split("#")[0].split("?")[0] == "/admin"
            else "account",
            title=title,
            body=body,
            target=target,
            nodes=nodes or [],
            actions=actions or [],
        )
        session.add(item)
        session.flush()
        if channel not in {"auto", "inbox", "telegram", "email", "all"}:
            raise DomainError("notification_channel_invalid", "Некорректный канал.", 400)
        channels = []
        if account.telegram_id is not None and channel in {"auto", "telegram", "all"}:
            channels.append(("telegram", str(account.telegram_id)))
        if (
            account.email_verified
            and account.email
            and (channel in {"email", "all"} or channel == "auto" and not channels)
        ):
            channels.append(("email", account.email))
        for delivery_channel, recipient in channels:
            session.add(
                NotificationDelivery(notification_id=item.id, channel=delivery_channel, recipient=recipient)
            )
        preference = session.get(NotificationPreference, account.id)
        if allow_push and preference and preference.browser_enabled and kind != "broadcast":
            for subscription in session.scalars(
                select(PushSubscription).where(
                    PushSubscription.account_id == account.id, PushSubscription.disabled_at.is_(None)
                )
            ):
                session.add(
                    NotificationDelivery(notification_id=item.id, channel="push", recipient=subscription.id)
                )
        return item

    def _audiences_in(self, session, account):
        from .staff_permissions import StaffPermissions

        allowed = {"account"}
        if account:
            permissions = StaffPermissions(SimpleNamespace(db=self.db, settings=self.settings))
            role = permissions.role_in(session, account)
            if role:
                allowed.add("staff")
            if role == "owner":
                allowed.add("owner")
        return allowed

    def _owned_actions(self, session, item, *, include_generic=False):
        from .retention import fenced_in

        result = []
        for action in item.actions[:5] if isinstance(item.actions, list) else []:
            if (
                not isinstance(action, dict)
                or not isinstance(action.get("label"), str)
                or not 1 <= len(action["label"]) <= 100
            ):
                continue
            if "operation" not in action:
                if include_generic:
                    try:
                        target = safe_target(action.get("target"))
                        if target:
                            result.append({"label": action["label"], "target": target})
                    except DomainError:
                        pass
                continue
            if (
                item.kind == "device_unlink"
                and item.audience == "owner"
                and action.get("operation")
                in {"device_unlink_profile", "device_unlink_warn", "device_unlink_block"}
            ):
                metadata = action.get("metadata")
                if (
                    not isinstance(metadata, dict)
                    or set(metadata) != {"account_id", "unlink_event_id"}
                    or action.get("target") != "/admin#users"
                ):
                    continue
                event = session.get(DeviceUnlink, metadata.get("unlink_event_id"))
                if (
                    not event
                    or item.event_key != "owner-device-unlink:" + event.id
                    or str(event.account_id) != metadata.get("account_id")
                ):
                    continue
                from .moderation import device_unlink_event_in

                try:
                    device_unlink_event_in(session, event, self.runtime_policy)
                except DomainError:
                    continue
                result.append(action)
                continue
            if (
                set(action) != {"label", "target", "operation", "metadata"}
                or action["operation"] not in {"connect", "devices", "renew"}
                or action["target"] != "/#subscriptions"
            ):
                continue
            metadata = action["metadata"]
            expected = (
                {"subscription_id", "quote_id"} if action["operation"] == "renew" else {"subscription_id"}
            )
            if (
                not isinstance(metadata, dict)
                or set(metadata) != expected
                or not all(isinstance(value, str) and 1 <= len(value) <= 64 for value in metadata.values())
            ):
                continue
            sub = session.get(Subscription, metadata["subscription_id"])
            if sub is None or sub.account_id != item.account_id or fenced_in(session, sub):
                continue
            if action["operation"] == "renew":
                quote = session.get(Quote, metadata["quote_id"])
                if (
                    quote is None
                    or quote.account_id != item.account_id
                    or quote.subscription_id != sub.id
                    or quote.snapshot.get("action") != "renew"
                ):
                    continue
            result.append(action)
        return result

    def list(self, account_id, limit=50, *, offset=0):
        limit = max(1, min(int(limit), 100))
        offset = max(0, min(int(offset), 100000))
        with self.db.transaction() as session:
            audiences = self._audiences_in(session, session.get(Account, int(account_id)))
            rows = list(
                session.scalars(
                    select(Notification)
                    .where(
                        Notification.account_id == int(account_id),
                        Notification.audience.in_(audiences),
                        Notification.kind.not_in(SUPPRESSED_NOTIFICATION_KINDS),
                    )
                    .order_by(Notification.created_at.desc(), Notification.id.desc())
                    .offset(offset)
                    .limit(limit)
                )
            )
            unread = session.scalar(
                select(func.count())
                .select_from(Notification)
                .where(
                    Notification.account_id == int(account_id),
                    Notification.read_at.is_(None),
                    Notification.audience.in_(audiences),
                    Notification.kind.not_in(SUPPRESSED_NOTIFICATION_KINDS),
                )
            )
            total = session.scalar(
                select(func.count())
                .select_from(Notification)
                .where(
                    Notification.account_id == int(account_id),
                    Notification.audience.in_(audiences),
                    Notification.kind.not_in(SUPPRESSED_NOTIFICATION_KINDS),
                )
            )
            return {
                "items": [
                    {
                        "id": row.id,
                        "kind": row.kind,
                        "title": row.title,
                        "body": row.body,
                        "severity": row.severity,
                        "created_at": row.created_at.isoformat(),
                        "read_at": row.read_at.isoformat() if row.read_at else None,
                        "target": row.target,
                        "nodes": row.nodes,
                        "actions": self._owned_actions(session, row),
                    }
                    for row in rows
                ],
                "unread_count": unread,
                "total_count": total,
                "next_offset": offset + limit if offset + limit < total else None,
                "previous_offset": max(0, offset - limit) if offset else None,
            }

    def mark_read(self, account_id, notification_id):
        with self.db.transaction() as session:
            audiences = self._audiences_in(session, session.get(Account, int(account_id)))
            item = session.scalar(
                select(Notification)
                .where(
                    Notification.id == notification_id,
                    Notification.kind.not_in(SUPPRESSED_NOTIFICATION_KINDS),
                    Notification.account_id == int(account_id),
                    Notification.audience.in_(audiences),
                )
                .with_for_update()
            )
            if not item:
                raise missing()
            item.read_at = item.read_at or now()
        return {"status": "read"}

    def mark_all_read(self, account_id):
        from sqlalchemy import update

        with self.db.transaction() as session:
            audiences = self._audiences_in(session, session.get(Account, int(account_id)))
            session.execute(
                update(Notification)
                .where(
                    Notification.account_id == int(account_id),
                    Notification.read_at.is_(None),
                    Notification.audience.in_(audiences),
                    Notification.kind.not_in(SUPPRESSED_NOTIFICATION_KINDS),
                )
                .values(read_at=now())
            )
        return {"status": "read"}

    def preferences(self, account_id):
        with self.db.transaction() as session:
            preference = session.get(NotificationPreference, int(account_id))
            subscriptions = session.scalars(
                select(PushSubscription).where(
                    PushSubscription.account_id == int(account_id), PushSubscription.disabled_at.is_(None)
                )
            )
            return {
                "browser_enabled": bool(preference and preference.browser_enabled),
                "push_subscriptions": [
                    {"id": row.id, "created_at": row.created_at.isoformat()} for row in subscriptions
                ],
            }

    def set_browser_preference(self, account_id, enabled):
        if not isinstance(enabled, bool):
            raise DomainError("push_preference_invalid", "Выберите состояние уведомлений.", 400)
        with self.db.transaction() as session:
            account = session.scalar(select(Account).where(Account.id == int(account_id)).with_for_update())
            if not account:
                raise missing()
            preference = session.scalar(
                select(NotificationPreference)
                .where(NotificationPreference.account_id == account.id)
                .with_for_update()
            )
            if not preference:
                preference = NotificationPreference(account_id=account.id)
                session.add(preference)
            preference.browser_enabled = enabled
            if not enabled:
                rows = session.scalars(
                    select(NotificationDelivery)
                    .join(Notification, Notification.id == NotificationDelivery.notification_id)
                    .where(
                        Notification.account_id == account.id,
                        NotificationDelivery.channel == "push",
                        NotificationDelivery.state == "pending",
                    )
                    .with_for_update(of=NotificationDelivery)
                )
                for delivery in rows:
                    delivery.state, delivery.last_error = "cancelled", "push_opted_out"
        return {"browser_enabled": enabled}

    def _cipher(self):
        import base64

        from cryptography.fernet import Fernet

        return Fernet(
            base64.urlsafe_b64encode(
                hashlib.sha256((self.settings.secret_key + ":browser-push:v11").encode()).digest()
            )
        )

    def public_key(self):
        key = getattr(self.settings, "push_private_key", None)
        if not key:
            return None
        import base64

        from cryptography.hazmat.primitives import serialization

        private = serialization.load_der_private_key(
            base64.urlsafe_b64decode(key + "=" * (-len(key) % 4)), None
        )
        public = private.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        return base64.urlsafe_b64encode(public).rstrip(b"=").decode()

    def register_push(self, account_id, value):
        import base64

        endpoint = value.get("endpoint", "") if isinstance(value, dict) else ""
        parsed = urlsplit(endpoint)
        allowed = {
            "fcm.googleapis.com",
            "updates.push.services.mozilla.com",
            "web.push.apple.com",
            "wns2-par02p.notify.windows.com",
        }
        if (
            parsed.scheme != "https"
            or parsed.hostname not in allowed
            or parsed.username
            or parsed.password
            or parsed.port not in {None, 443}
            or parsed.fragment
            or len(endpoint) > 2048
        ):
            raise DomainError("push_endpoint_denied", "Сервис уведомлений браузера не поддерживается.", 400)
        keys = value.get("keys", {})
        try:
            decoded = {
                key: base64.urlsafe_b64decode(keys[key] + "=" * (-len(keys[key]) % 4))
                for key in ("p256dh", "auth")
            }
            if len(decoded["p256dh"]) != 65 or decoded["p256dh"][0] != 4 or len(decoded["auth"]) != 16:
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise DomainError("push_keys_invalid", "Не удалось зарегистрировать браузер.", 400) from None
        if not getattr(self.settings, "push_private_key", None):
            raise DomainError("push_unavailable", "Уведомления браузера ещё не настроены.", 503)
        digest = hashlib.sha256(endpoint.encode()).hexdigest()
        with self.db.transaction() as session:
            if not session.scalar(select(Account).where(Account.id == int(account_id)).with_for_update()):
                raise missing()
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": "push:" + digest}
            )
            row = session.scalar(
                select(PushSubscription).where(PushSubscription.endpoint_digest == digest).with_for_update()
            )
            if row and row.account_id != int(account_id):
                raise DomainError("push_in_use", "Этот браузер уже связан с другим аккаунтом.", 409)
            encrypted = (
                self._cipher()
                .encrypt(
                    json.dumps({"account_id": str(account_id), "endpoint": endpoint, "keys": keys}).encode()
                )
                .decode()
            )
            if not row:
                row = PushSubscription(
                    account_id=int(account_id), endpoint_digest=digest, subscription_ciphertext=encrypted
                )
                session.add(row)
            row.disabled_at, row.subscription_ciphertext = None, encrypted
            preference = session.get(NotificationPreference, int(account_id))
            if not preference:
                preference = NotificationPreference(account_id=int(account_id))
                session.add(preference)
                preference.browser_enabled = True
            session.flush()
            return {"id": row.id, "browser_enabled": preference.browser_enabled}

    def remove_push(self, account_id, identifier):
        with self.db.transaction() as session:
            if not session.scalar(select(Account).where(Account.id == int(account_id)).with_for_update()):
                raise missing()
            row = session.scalar(
                select(PushSubscription)
                .where(PushSubscription.id == identifier, PushSubscription.account_id == int(account_id))
                .with_for_update()
            )
            if not row:
                raise missing()
            row.disabled_at = row.disabled_at or now()
            session.flush()
            active = session.scalar(
                select(PushSubscription.id)
                .where(PushSubscription.account_id == int(account_id), PushSubscription.disabled_at.is_(None))
                .limit(1)
            )
            preference = session.get(NotificationPreference, int(account_id))
            if preference and not active:
                preference.browser_enabled = False
        return {"status": "disabled"}

    def _allowed(self, session, account, delivery):
        if not account:
            return False
        if delivery.channel == "telegram":
            allowed = {
                item.strip()
                for item in getattr(self.settings, "test_telegram_recipients", "").split(",")
                if item.strip()
            }
            return (
                str(account.telegram_id) == delivery.recipient
                and (
                    bool(self.runtime_policy and self.runtime_policy.production)
                    or delivery.recipient in allowed
                )
                and not account.telegram_notifications_blocked
            )
        if delivery.channel == "email":
            return (
                account.email_verified
                and account.email == delivery.recipient
                and (
                    bool(self.runtime_policy and self.runtime_policy.production)
                    or delivery.recipient in self.settings.email_allowlist
                )
            )
        if delivery.channel == "push" and (
            getattr(self.settings, "push_test_authorized", False)
            or bool(self.runtime_policy and self.runtime_policy.production)
        ):
            preference = session.get(NotificationPreference, account.id)
            subscription = session.get(PushSubscription, delivery.recipient)
            return bool(
                preference
                and preference.browser_enabled
                and subscription
                and subscription.account_id == account.id
                and subscription.disabled_at is None
            )
        return False

    @staticmethod
    def _scoped_paid_join():
        return or_(
            and_(Notification.kind == "payment_received", Notification.event_key == "payment:" + Order.id),
            and_(Notification.kind == "sale", Notification.event_key == "sale:" + Order.id),
            and_(
                Notification.kind.in_(["access_ready", "subscription_renewed"]),
                Notification.event_key == "access-ready:" + Order.id,
            ),
        )

    def _scoped_paid_order_in(self, session, account, item):
        policy = self.runtime_policy
        if not policy or not policy.scoped_payments or not account or not item:
            return False
        policy.require_current(session)
        prefixes = {
            "payment_received": "payment:",
            "sale": "sale:",
            "access_ready": "access-ready:",
            "subscription_renewed": "access-ready:",
        }
        prefix = prefixes.get(item.kind)
        if not prefix or not item.event_key.startswith(prefix):
            return False
        order = session.get(Order, item.event_key[len(prefix) :])
        if (
            not order
            or order.environment != "scoped_live"
            or order.status != "paid"
            or order.fulfilled_at is None
            or (item.kind == "sale") != (item.audience == "staff")
            or item.kind != "sale"
            and (item.audience != "account" or item.account_id != order.account_id)
        ):
            return False
        try:
            policy.require_paid_service_in(session, order)
        except DomainError:
            return False
        if order.amount > 0:
            operation = session.scalar(
                select(ProviderOperation).where(ProviderOperation.order_id == order.id)
            )
            payment = session.scalar(select(Payment).where(Payment.order_id == order.id))
            if (
                not operation
                or not payment
                or payment.status != "paid"
                or payment.provider != "rollypay"
                or payment.environment != "scoped_live"
                or payment.terminal != policy.scope.rollypay.terminal
                or payment.provider_id != operation.provider_id
                or payment.account_id != order.account_id
                or not policy.owns_original_in(
                    session,
                    session.get(Account, order.account_id),
                    str(payment.original_account_id),
                )
                or payment.amount != order.amount
                or payment.currency != order.currency
            ):
                return False
        return True

    @classmethod
    def _scoped_notice_filter(cls):
        paid = select(Order.id).where(Order.environment == "scoped_live", cls._scoped_paid_join()).exists()
        subscription = (
            select(Subscription.id)
            .where(
                Subscription.environment == "scoped_live",
                or_(
                    and_(
                        Notification.kind == "new_device",
                        Notification.event_key.startswith("panel_device:" + Subscription.id + ":"),
                    ),
                    and_(
                        Notification.kind == "connection_help",
                        Notification.event_key.startswith("connection-help:" + Subscription.id + ":"),
                    ),
                    and_(
                        Notification.kind == "connection_success",
                        Notification.event_key.startswith("first-connected:" + Subscription.id + ":"),
                    ),
                    and_(
                        Notification.kind == "subscription_deleted",
                        Notification.event_key == "subscription-deleted:" + Subscription.id,
                    ),
                    and_(
                        Notification.kind == "subscription_deleted_owner",
                        Notification.event_key == "owner-subscription-deleted:" + Subscription.id,
                    ),
                ),
            )
            .exists()
        )
        device = (
            select(DeviceUnlink.id)
            .join(Subscription, Subscription.id == DeviceUnlink.subscription_id)
            .where(
                Subscription.environment == "scoped_live",
                Notification.kind == "device_unlink",
                Notification.event_key == "owner-device-unlink:" + DeviceUnlink.id,
            )
            .exists()
        )
        return or_(paid, subscription, device)

    def _scoped_notice_in(self, session, account, item):
        if item.kind in {"payment_received", "sale", "subscription_renewed"}:
            return self._scoped_paid_order_in(session, account, item)
        policy = self.runtime_policy
        policy.require_current(session)
        if item.kind == "device_unlink":
            from .moderation import device_unlink_event_in

            event = session.get(DeviceUnlink, item.event_key.removeprefix("owner-device-unlink:"))
            try:
                device_unlink_event_in(session, event, policy)
                return item.audience == "owner" and item.event_key == "owner-device-unlink:" + event.id
            except DomainError:
                return False
        prefixes = {
            "new_device": "panel_device",
            "connection_help": "connection-help",
            "connection_success": "first-connected",
            "subscription_deleted": "subscription-deleted",
            "subscription_deleted_owner": "owner-subscription-deleted",
        }
        prefix = prefixes.get(item.kind)
        parts = item.event_key.split(":")
        if not prefix or len(parts) < 2 or parts[0] != prefix:
            return False
        sub = session.get(Subscription, parts[1])
        if not sub or sub.environment != "scoped_live":
            return False
        try:
            if item.kind in {"subscription_deleted", "subscription_deleted_owner"}:
                from .subscription_archive import state_in

                state = state_in(session, sub)
                policy.require_account_in(session, session.get(Account, sub.account_id))
                policy.require_subscription_in(session, sub)
                if (
                    not state
                    or state.environment != "scoped_live"
                    or state.panel_host != urlsplit(policy.scope.panel.origin).hostname
                ):
                    return False
                return (
                    (item.audience == "owner" and state.owner_intent_id == item.id)
                    if item.kind.endswith("_owner")
                    else (
                        item.audience == "account"
                        and item.account_id == sub.account_id
                        and state.user_intent_id == item.id
                    )
                )
            if item.audience != "account" or item.account_id != sub.account_id:
                return False
            policy.require_subscription_in(session, sub)
            if item.kind == "new_device":
                from .panel_entitlements import verified_observation

                device = session.get(Device, parts[2]) if len(parts) == 4 else None
                return bool(
                    device
                    and device.subscription_id == sub.id
                    and not device.revoked
                    and str(device.generation) == parts[3]
                    and verified_observation(session, sub)
                )
            from .subscription_engagement import SubscriptionEngagement, state_in

            state = state_in(session, sub)
            if not state or len(parts) != 3 or state.origin_nonce != parts[2]:
                return False
            if item.kind == "connection_success":
                return state.connected_intent_id == item.id and state.ever_connected_at is not None
            from .commerce import Commerce
            from .panel import configured_panel

            panel = configured_panel(self.settings)
            try:
                commerce = Commerce(self.db, public_settings=self.settings, panel_provider=panel)
                return SubscriptionEngagement(commerce, self).help_notice_current_in(session, sub, item.id)
            finally:
                panel.client.close()
        except (DomainError, ValueError, TypeError):
            return False

    def _recover_claims(self, *, scoped_only=False):
        with self.db.transaction() as session:
            query = (
                select(NotificationDelivery)
                .where(NotificationDelivery.state == "in_flight", NotificationDelivery.available_at <= now())
                .order_by(NotificationDelivery.available_at)
                .with_for_update(skip_locked=True)
                .limit(100)
            )
            if scoped_only:
                self.runtime_policy.require_current(session)
                query = query.join(Notification).where(
                    self._scoped_notice_filter(), NotificationDelivery.channel == "telegram"
                )
            rows = session.scalars(query)
            for delivery in rows:
                delivery.state, delivery.last_error = "review", "delivery_outcome_unknown"

    def _expiry_current(self, session, account, item):
        from . import panel_entitlements
        from .retention import fenced_in

        try:
            _, subscription_id, remainder = item.event_key.split(":", 2)
            stamp, bucket = remainder.rsplit(":", 1)
            expiry = datetime.fromisoformat(stamp)
        except (ValueError, TypeError):
            return False
        subscription = session.scalar(
            select(Subscription)
            .where(Subscription.id == subscription_id, Subscription.account_id == account.id)
            .with_for_update()
        )
        if not subscription or fenced_in(session, subscription):
            return False
        current_expiry = panel_entitlements.verified_service_horizon(session, subscription, now())
        if panel_entitlements.bound(subscription) and current_expiry is None:
            return False
        if (current_expiry or subscription.expires_at) != expiry:
            return False
        seconds = (expiry - now()).total_seconds()
        current = (
            "expired" if seconds <= 0 else "1d" if seconds <= 86400 else "3d" if seconds <= 259200 else None
        )
        return current == bucket and seconds >= -86400

    def deliver_pending(self, limit=20, *, scoped_only=False):
        if self.runtime_policy:
            self.runtime_policy.require()
        if scoped_only and not (self.runtime_policy and self.runtime_policy.scoped_payments):
            raise DomainError("scoped_test_stage_denied", "Операция недоступна в тестовом контуре.", 403)
        self._recover_claims(scoped_only=scoped_only)
        completed = 0
        for _ in range(max(0, min(limit, 100))):
            with self.db.transaction() as session:
                query = (
                    select(NotificationDelivery.id, Notification.account_id)
                    .join(Notification, Notification.id == NotificationDelivery.notification_id)
                    .where(
                        NotificationDelivery.state == "pending",
                        NotificationDelivery.available_at <= now(),
                    )
                    .order_by(NotificationDelivery.available_at)
                    .limit(1)
                )
                if scoped_only:
                    self.runtime_policy.require_current(session)
                    query = query.where(
                        self._scoped_notice_filter(), NotificationDelivery.channel == "telegram"
                    )
                candidate = session.execute(query).first()
                if not candidate:
                    break
                account = session.scalar(
                    select(Account)
                    .where(Account.id == candidate.account_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                delivery = session.scalar(
                    select(NotificationDelivery)
                    .where(NotificationDelivery.id == candidate.id, NotificationDelivery.state == "pending")
                    .with_for_update(skip_locked=True)
                )
                if not delivery:
                    continue
                item = session.get(Notification, delivery.notification_id)
                if item and item.kind in SUPPRESSED_NOTIFICATION_KINDS:
                    if self.runtime_policy:
                        try:
                            self.runtime_policy.require_current(session)
                            if self.runtime_policy.scoped_payments:
                                self.runtime_policy.require_account_in(session, account)
                                if scoped_only and not self._scoped_paid_order_in(session, account, item):
                                    return completed
                        except DomainError:
                            return completed
                    delivery.state, delivery.last_error = "cancelled", "notification_suppressed"
                    continue
                if self.runtime_policy and self.runtime_policy.production and item:
                    try:
                        self.runtime_policy.require_current(
                            session,
                            {
                                "email": "notice_email",
                                "telegram": "notice_telegram",
                                "push": "push_delivery",
                            }.get(delivery.channel, "invalid"),
                        )
                        if item.kind == "broadcast":
                            self.runtime_policy.require_current(session, "broadcast_delivery")
                    except DomainError:
                        return completed
                if (
                    not item
                    or not account
                    or item.account_id != account.id
                    or item.kind == "broadcast"
                    and (
                        delivery.channel == "push"
                        or delivery.channel == "email"
                        and account.telegram_id is not None
                        and not self._media_self_in(session, item)
                    )
                    or item.audience not in self._audiences_in(session, account)
                    or not self._allowed(session, account, delivery)
                ):
                    delivery.state, delivery.last_error = "blocked", "recipient_not_authorized"
                    continue
                if scoped_only and not self._scoped_notice_in(session, account, item):
                    delivery.state, delivery.last_error = "blocked", "scoped_paid_notice_denied"
                    continue
                paid_content = None
                if item.kind in {"payment_received", "sale"}:
                    paid_content = self._paid_notice_content_in(session, item)
                    if scoped_only and paid_content is None:
                        delivery.state, delivery.last_error = "blocked", "paid_notice_snapshot_missing"
                        continue
                if item.kind == "subscription_expiry" and not self._expiry_current(session, account, item):
                    delivery.state, delivery.last_error = "cancelled", "subscription_expiry_changed"
                    continue
                checkout_url = None
                if item.kind == "broadcast" and not (self.runtime_policy and self.runtime_policy.production):
                    from .legal import LEGAL_LOCK, Legal

                    session.execute(select(func.pg_advisory_xact_lock(LEGAL_LOCK)))
                    if not Legal(SimpleNamespace(db=self.db)).status_in(session, account)["accepted"]:
                        delivery.state, delivery.last_error = "blocked", "legal_consent_required"
                        continue
                media = None
                if item.kind == "broadcast":
                    from .broadcast_media_registry import notification_media

                    try:
                        media = notification_media(self.db, self.settings, item, session)
                    except DomainError as exc:
                        delivery.state, delivery.last_error = "review", exc.code
                        continue
                delivery.state, delivery.attempts = "in_flight", delivery.attempts + 1
                delivery.available_at = now() + timedelta(minutes=5)
                identifier, channel, recipient = delivery.id, delivery.channel, delivery.recipient
                claimed_account_id = account.id
                activity = session.get(Setting, "telegram-activity:" + str(account.id))
                activity_generation = activity.value if activity else None
                payload = {
                    "title": paid_content[0] if paid_content else item.title,
                    "kind": item.kind,
                    "body": paid_content[1] if paid_content else item.body,
                    "target": item.target,
                    "nodes": item.nodes,
                    "actions": self._owned_actions(session, item, include_generic=True),
                    "notification_id": item.id,
                    "audience": item.audience,
                    "tag": item.id,
                    "checkout_url": checkout_url,
                    **({"media": media} if media is not None else {}),
                }
                if item.kind == "sale":
                    order = session.get(Order, item.event_key.removeprefix("sale:"))
                    quote = session.get(Quote, order.quote_id) if order else None
                    if quote:
                        payload |= {
                            "product_name": quote.snapshot.get("product_name"),
                            "product_content_name": quote.snapshot.get("product_content_name"),
                            "payment_method": (quote.snapshot.get("payment_method") or {}).get("id"),
                        }
                theme = session.get(Setting, "telegram_theme")
                payload = self._notice_presentation(theme.value if theme else {}, payload)
            result, error, retry_after = "delivered", None, None
            try:
                self._deliver(channel, recipient, payload, identifier)
            except DomainError as exc:
                result, error = (
                    (
                        "cancelled"
                        if exc.code in TELEGRAM_RECIPIENT_BLOCKS | {"telegram_chat_not_found"}
                        else "review"
                    ),
                    exc.code,
                )
                if isinstance(exc, _TelegramRetryAfter):
                    result, retry_after = "pending", exc.seconds
            except Exception:  # noqa: BLE001 -- network outcome must be quarantined without leaking transport credentials
                # Unknown outcome must not be automatically resent.
                result, error = "review", "delivery_outcome_unknown"
            with self.db.transaction() as session:
                current_item = session.get(Notification, payload["notification_id"])
                account = (
                    session.scalar(
                        select(Account)
                        .where(Account.id == current_item.account_id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                    if current_item
                    else None
                )
                delivery = session.scalar(
                    select(NotificationDelivery)
                    .where(NotificationDelivery.id == identifier)
                    .with_for_update()
                )
                if not delivery or delivery.state != "in_flight":
                    continue
                if result == "pending":
                    if delivery.attempts <= 3:
                        delivery.available_at = now() + timedelta(seconds=retry_after)
                    else:
                        result = "review"
                delivery.state, delivery.last_error = result, error
                if result == "delivered":
                    delivery.delivered_at = now()
                    completed += 1
                activity = session.get(Setting, "telegram-activity:" + str(account.id)) if account else None
                if (
                    error in TELEGRAM_RECIPIENT_BLOCKS
                    and account
                    and account.id == claimed_account_id
                    and str(account.telegram_id) == recipient
                    and (activity.value if activity else None) == activity_generation
                ):
                    account.telegram_notifications_blocked = True
        return completed

    def _media_self_in(self, session, item):
        from .db import Job

        if not item.event_key.startswith("broadcast:"):
            return False
        job = session.get(Job, item.event_key[len("broadcast:") :])
        return bool(job and job.account_id == item.account_id and job.payload.get("source") == "media_self")

    def _action_links(self, payload):
        from .providers import checkout_url as verified_checkout_url
        from .providers import sandbox_checkout_url

        checkout_url = sandbox_checkout_url(payload.get("checkout_url"))
        if self.runtime_policy and self.runtime_policy.production:
            checkout_url = verified_checkout_url(
                payload.get("checkout_url"), self.runtime_policy.scope.rollypay.checkout_hosts
            )
        links = [{"label": "Оплатить", "url": checkout_url}] if checkout_url else []
        base = getattr(self.settings, "dashboard_url", None)
        if not isinstance(base, str):
            return links
        base = base.rstrip("/")
        parsed = urlsplit(base)
        if (
            not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.scheme not in {"https", "http"}
            or parsed.scheme == "http"
            and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.query
            or parsed.fragment
        ):
            return links
        for action in (payload.get("actions") or [])[:5]:
            if not isinstance(action, dict):
                continue
            label = action.get("label")
            if not isinstance(label, str) or not 1 <= len(label) <= 100:
                continue
            try:
                target = safe_target(action.get("target"))
            except DomainError:
                continue
            if target:
                links.append({"label": label, "url": base + target})
        return links

    @staticmethod
    def _notice_presentation(theme, payload):
        from sunrise_core.content import validate_content

        icons = theme.get("icons", {}) if isinstance(theme, dict) else {}
        if not isinstance(icons, dict):
            icons = {}

        def emoji(key, fallback):
            alias = {
                "notifications_heading": "help_heading",
                "admin_finance_heading": "history_heading",
                "admin_warn": "error_heading",
                "admin_block": "revoke",
            }.get(key, key)
            entry = icons.get(key) or icons.get(alias) or {}
            if not isinstance(entry, dict):
                entry = {}
            result = {"type": "emoji", "fallback": entry.get("fallback") or fallback, "label": key}
            identifier = entry.get("custom_emoji_id")
            if (
                isinstance(identifier, str)
                and identifier.isascii()
                and identifier.isdigit()
                and len(identifier) <= 32
            ):
                result["id"] = identifier
            return result

        kind = payload.get("kind")
        title_key = {
            "payment_received": "success_heading",
            "sale": "admin_finance_heading",
            "new_device": "devices_heading",
            "device_unlink": "devices_heading",
            "connection_help": "help_heading",
            "subscription_deleted": "error_heading",
            "subscription_deleted_owner": "error_heading",
        }.get(kind, "notifications_heading")
        title_nodes = [
            {
                "type": "bold",
                "children": [emoji(title_key, "🔔"), {"type": "text", "text": " " + payload["title"]}],
            }
        ]
        nodes = payload.get("nodes") or []
        if (
            not nodes
            and kind in {"payment_received", "sale", "device_unlink"}
            and len(payload["body"]) <= 4000
            and len(payload["body"].split("\n")) <= 20
        ):
            fields = {
                "Покупатель": ("profile", "👤"),
                "Товар": ("products", "📦"),
                "Метод": (
                    "payment_"
                    + (
                        payload.get("payment_method")
                        if payload.get("payment_method") in {"sbp", "crypto", "xrocket", "bank_card"}
                        else "bank_card"
                    ),
                    "💳",
                ),
                "Сумма": ("quote_price", "💰"),
                "Бонусы": ("quote_bonus", "🎁"),
                "Промокод": ("promo", "🏷"),
                "Пользователь": ("profile", "👤"),
            }
            nodes = []
            for index, line in enumerate(payload["body"].split("\n")):
                if index:
                    nodes.append({"type": "br"})
                for label, (key, fallback) in fields.items():
                    if line.startswith(label):
                        nodes.extend([emoji(key, fallback), {"type": "text", "text": " "}])
                        break
                name = payload.get("product_name")
                content = payload.get("product_content_name")
                if (
                    line.startswith("Товар:")
                    and isinstance(name, str)
                    and name
                    and name in line
                    and isinstance(content, list)
                    and content
                ):
                    try:
                        validate_content(content)
                    except ValueError:
                        nodes.append({"type": "text", "text": line})
                        continue
                    parts = line.split(": ", 2)
                    if len(parts) != 3:
                        nodes.append({"type": "text", "text": line})
                        continue
                    operation_prefix, product_tail = ": ".join(parts[:2]), parts[2]
                    if not product_tail.startswith(name):
                        nodes.append({"type": "text", "text": line})
                        continue
                    prefix, suffix = operation_prefix + ": ", product_tail[len(name) :]
                    nodes.extend(
                        [
                            {"type": "text", "text": prefix},
                            {"type": "bold", "children": content},
                            {"type": "text", "text": suffix},
                        ]
                    )
                else:
                    nodes.append({"type": "text", "text": line})
        validate_content(title_nodes)
        if nodes:
            try:
                validate_content(nodes)
            except ValueError:
                # Presentation cannot strand a durable message; transport enforces its own length limit.
                nodes = payload.get("nodes") or []
        return {
            **payload,
            "title_nodes": title_nodes,
            "nodes": nodes,
            "notice_icons": {
                key: emoji(key, fallback)
                for key, fallback in [
                    ("subscriptions", "🗂"),
                    ("main", "🏠"),
                    ("profile", "👤"),
                    ("admin_warn", "⚠️"),
                    ("admin_block", "⛔️"),
                ]
            },
        }

    @staticmethod
    def _paid_payment_buttons(payload):
        if payload.get("kind") != "payment_received" or payload.get("audience") != "account":
            return []
        rows = [
            [{"text": "Мои подписки", "callback_data": "notice:subscriptions"}],
            [{"text": "Главное меню", "callback_data": "notice:main", "style": "danger"}],
        ]
        for row, key in zip(rows, ("subscriptions", "main"), strict=True):
            icon = (payload.get("notice_icons") or {}).get(key) or {}
            if icon.get("id"):
                row[0]["icon_custom_emoji_id"] = icon["id"]
            else:
                row[0]["text"] = (icon.get("fallback", "") + " " + row[0]["text"]).strip()
        return rows

    def _owner_device_buttons(self, payload):
        from sunrise_core.notice_callbacks import owner_notice_callback

        if payload.get("kind") != "device_unlink" or payload.get("audience") != "owner":
            return []
        operations = {
            "device_unlink_profile": ("p", "Профиль пользователя", "profile"),
            "device_unlink_warn": ("w", "Выдать предупреждение", "admin_warn"),
            "device_unlink_block": ("b", "Заблокировать удаления", "admin_block"),
        }
        rows = []
        for action in payload.get("actions") or []:
            spec = operations.get(action.get("operation"))
            if not spec:
                continue
            operation, label, key = spec
            try:
                callback = owner_notice_callback(
                    operation, action["metadata"]["unlink_event_id"], self.settings.secret_key
                )
            except (KeyError, ValueError, TypeError, AttributeError):
                continue
            button = {"text": label, "callback_data": callback}
            icon = (payload.get("notice_icons") or {}).get(key) or {}
            if icon.get("id"):
                button["icon_custom_emoji_id"] = icon["id"]
            else:
                button["text"] = (icon.get("fallback", "") + " " + label).strip()
            if operation == "b":
                button["style"] = "danger"
            rows.append([button])
        return rows

    @staticmethod
    def _paid_notice_content_in(session, item):
        from .sale_notifications import paid_staff_sale_content

        prefix = "payment:" if item.kind == "payment_received" else "sale:"
        if not item.event_key.startswith(prefix):
            return None
        order = session.get(Order, item.event_key[len(prefix) :])
        if not order or order.status != "paid" or order.fulfilled_at is None:
            return None
        if item.kind == "payment_received":
            from .db import Job

            job = session.scalar(select(Job).where(Job.key == "provision:" + order.id))
            subscription = session.get(Subscription, order.subscription_id)
            ready = bool(
                job
                and job.kind == "provision"
                and job.state == "done"
                and job.last_error is None
                and job.account_id == order.account_id
                and job.payload.get("order_id") == order.id
                and job.payload.get("subscription_id") == order.subscription_id
                and subscription
                and subscription.account_id == order.account_id
                and subscription.status == "active"
            )
            return (
                "Заказ оплачен",
                "Заказ оплачен. Доступ готов, подписка выдана."
                if ready
                else "Заказ оплачен. Готовим доступ. Статус подписки можно проверить в меню.",
            )
        quote = session.get(Quote, order.quote_id)
        buyer = session.get(Account, order.account_id)
        if not quote or not buyer or not isinstance(quote.snapshot, dict):
            return None
        return paid_staff_sale_content(buyer, order, quote.snapshot)

    def _validate_delivery(self, channel, recipient, payload, identifier):
        from .broadcast_media_registry import configured_media_store, notification_media
        from .legal import Legal

        media = payload.get("media")
        with self.db.transaction() as session:
            delivery = session.get(NotificationDelivery, identifier)
            if self.runtime_policy and self.runtime_policy.production:
                self.runtime_policy.require_current(
                    session,
                    {"email": "notice_email", "telegram": "notice_telegram", "push": "push_delivery"}.get(
                        channel, "invalid"
                    ),
                )
            item = session.get(Notification, payload["notification_id"])
            account = session.get(Account, item.account_id) if item else None
            if self.runtime_policy and self.runtime_policy.production and item and item.kind == "broadcast":
                self.runtime_policy.require_current(session, "broadcast_delivery")
                from .db import Alias, Job, StaffRole
                from .staff_permissions import StaffPermissions

                job = session.get(Job, item.event_key.removeprefix("broadcast:"), populate_existing=True)
                actor = session.get(Account, job.account_id, populate_existing=True) if job else None
                if actor:
                    session.get(StaffRole, actor.id, populate_existing=True)
                permissions = StaffPermissions(SimpleNamespace(db=self.db, settings=self.settings))
                if (
                    not item.event_key.startswith("broadcast:")
                    or not job
                    or job.kind != "broadcast"
                    or job.state != "done"
                    or job.payload.get("environment") != "live"
                    or job.payload.get("scope_id") != self.runtime_policy.scope.scope_id
                    or job.payload.get("source") != "unified"
                    or not actor
                    or session.get(Alias, actor.id, populate_existing=True)
                    or "broadcasts_write" not in permissions.capabilities_in(session, actor, bot=True)
                    or not account
                    or session.get(Alias, account.id, populate_existing=True)
                    or str(account.id) not in job.payload.get("account_ids", [])
                ):
                    raise missing()
            if (
                delivery is None
                or delivery.state != "in_flight"
                or delivery.notification_id != payload["notification_id"]
                or delivery.channel != channel
                or delivery.recipient != recipient
                or item is None
                or item.kind in SUPPRESSED_NOTIFICATION_KINDS
                or item.kind == "broadcast"
                and (
                    delivery.channel == "push"
                    or delivery.channel == "email"
                    and account.telegram_id is not None
                    and not self._media_self_in(session, item)
                )
                or item.audience != payload.get("audience", item.audience)
                or item.audience not in self._audiences_in(session, account)
                or not self._allowed(session, account, delivery)
                or self.runtime_policy
                and self.runtime_policy.scoped_payments
                and not self._scoped_notice_in(session, account, item)
                or media is not None
                and (
                    item.kind != "broadcast"
                    or not (self.runtime_policy and self.runtime_policy.production)
                    and not Legal(SimpleNamespace(db=self.db)).status_in(session, account)["accepted"]
                    or notification_media(self.db, self.settings, item, session) != media
                )
            ):
                raise missing()
        if media is None:
            return None, None
        store = configured_media_store(self.settings)
        return store, store.read(media)

    def _deliver(self, channel, recipient, payload, identifier):
        if payload.get("kind") in SUPPRESSED_NOTIFICATION_KINDS:
            raise DomainError("notification_suppressed", "Это уведомление отключено.", 409)
        media = payload.get("media")
        validate_authority = (
            media is not None
            or payload.get("audience") in {"owner", "staff"}
            or bool(self.runtime_policy and self.runtime_policy.production)
            or bool(self.runtime_policy and self.runtime_policy.scoped_payments)
        )
        media_store, media_bytes = None, None
        if validate_authority:
            media_store, media_bytes = self._validate_delivery(channel, recipient, payload, identifier)
        if channel == "telegram":
            if self.telegram_transport:
                return self.telegram_transport(recipient, payload, identifier)
            token = getattr(self.settings, "bot_test_token", None)
            approved = {x.strip() for x in getattr(self.settings, "test_bot_ids", "").split(",") if x.strip()}
            if self.runtime_policy and self.runtime_policy.production:
                token = self.settings.bot_token
                approved = {self.runtime_policy.scope.bot_id}
            if not token or token.split(":", 1)[0] not in approved:
                raise DomainError("telegram_unavailable", "Тестовый бот не настроен.", 503)
            with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
                identity_response = client.get("https://api.telegram.org/bot" + token + "/getMe")
                identity = identity_response.json()
                if (
                    isinstance(identity, dict)
                    and identity.get("ok") is False
                    and identity.get("error_code") in {401, 404}
                    and identity.get("error_code") == identity_response.status_code
                ):
                    raise DomainError(
                        "telegram_auth_failed"
                        if identity["error_code"] == 401
                        else "telegram_endpoint_invalid",
                        "Бот не подтверждён.",
                        503,
                    )
                if (
                    not isinstance(identity, dict)
                    or identity.get("ok") is not True
                    or not isinstance(identity.get("result"), dict)
                    or str(identity["result"].get("id")) not in approved
                ):
                    raise DomainError("telegram_unavailable", "Тестовый бот не подтверждён.", 503)
                body = validate_telegram_content(
                    payload["title"],
                    payload["body"],
                    payload["nodes"],
                    max_units=1024 if media else 4096,
                    title_nodes=payload.get("title_nodes"),
                )
                actions = self._action_links(payload)
                paid_buttons = self._paid_payment_buttons(payload) + self._owner_device_buttons(payload)
                if payload.get("kind") == "device_unlink":
                    actions = []
                if validate_authority:
                    _, media_bytes = self._validate_delivery(channel, recipient, payload, identifier)
                if media is not None:
                    field = "video" if media["mime"] == "video/mp4" else "photo"
                    data = {"chat_id": recipient, "caption": body, "parse_mode": "HTML"}
                    if actions or paid_buttons:
                        data["reply_markup"] = json.dumps(
                            {
                                "inline_keyboard": paid_buttons
                                + [[{"text": action["label"], "url": action["url"]}] for action in actions]
                            }
                        )
                    response = client.post(
                        "https://api.telegram.org/bot" + token + "/send" + field.title(),
                        data=data,
                        files={
                            field: (
                                "broadcast.mp4"
                                if field == "video"
                                else "broadcast.png"
                                if media["mime"] == "image/png"
                                else "broadcast.jpg",
                                media_bytes,
                                media["mime"],
                            )
                        },
                    )
                else:
                    response = client.post(
                        "https://api.telegram.org/bot" + token + "/sendMessage",
                        json={
                            "chat_id": recipient,
                            "text": body,
                            "parse_mode": "HTML",
                            "link_preview_options": {"is_disabled": True},
                            **(
                                {
                                    "reply_markup": {
                                        "inline_keyboard": paid_buttons
                                        + [
                                            [{"text": action["label"], "url": action["url"]}]
                                            for action in actions
                                        ]
                                    }
                                }
                                if actions or paid_buttons
                                else {}
                            ),
                        },
                    )
                error = _telegram_result(response)
                if error == "telegram_format_rejected":
                    plain = _telegram_plain(body)
                    if len(plain.encode("utf-16-le")) // 2 <= (1024 if media else 4096):
                        if validate_authority:
                            _, media_bytes = self._validate_delivery(channel, recipient, payload, identifier)
                        if media is not None:
                            data.pop("parse_mode", None)
                            data["caption"] = plain
                            response = client.post(
                                "https://api.telegram.org/bot" + token + "/send" + field.title(),
                                data=data,
                                files={
                                    field: (
                                        "broadcast.mp4" if field == "video" else "broadcast.png",
                                        media_bytes,
                                        media["mime"],
                                    )
                                },
                            )
                        else:
                            fallback = {
                                "chat_id": recipient,
                                "text": plain,
                                "link_preview_options": {"is_disabled": True},
                            }
                            if actions or paid_buttons:
                                fallback["reply_markup"] = {
                                    "inline_keyboard": paid_buttons
                                    + [
                                        [{"text": action["label"], "url": action["url"]}]
                                        for action in actions
                                    ]
                                }
                            response = client.post(
                                "https://api.telegram.org/bot" + token + "/sendMessage", json=fallback
                            )
                        error = _telegram_result(response)
                if error:
                    raise DomainError(error, "Уведомление не доставлено.", 503)
                return
        if channel == "email":
            transport = self.mail_transport
            if transport is None:
                from .mail import configured_mail

                transport = configured_mail(self.settings, media_store=media_store)
            if not transport:
                raise DomainError("mail_unavailable", "Почта не настроена.", 503)
            from .mail import SMTPTestMail, render_mail_text

            body = render_mail_text(payload["nodes"]) if payload["nodes"] else payload["body"]
            actions = self._action_links(payload)
            if isinstance(transport, SMTPTestMail):
                if media is not None and transport.media_store is None:
                    raise DomainError("media_unavailable", "Почтовое вложение не настроено.", 503)
                return transport.deliver(
                    recipient,
                    payload["title"],
                    body,
                    identifier,
                    actions=actions,
                    **({"media": media} if media is not None else {}),
                    **(
                        {
                            "before_send": lambda: self._validate_delivery(
                                channel, recipient, payload, identifier
                            )
                        }
                        if validate_authority
                        else {}
                    ),
                )
            if media is not None:
                return transport.deliver(recipient, payload["title"], body, identifier, media=media)
            if actions:
                body += "\n\n" + "\n".join(action["label"] + ": " + action["url"] for action in actions)
            return transport.deliver(recipient, payload["title"], body, identifier)
        if channel == "push":
            if self.push_transport:
                return self.push_transport(recipient, payload, identifier)
            from pywebpush import WebPushException, webpush

            with self.db.transaction() as session:
                row = session.get(PushSubscription, recipient)
                preference = session.get(NotificationPreference, row.account_id) if row else None
                if not row or row.disabled_at or not preference or not preference.browser_enabled:
                    raise DomainError("push_disabled", "Уведомления отключены.", 409)
                subscription = json.loads(self._cipher().decrypt(row.subscription_ciphertext.encode()))
                if subscription.pop("account_id") != str(row.account_id):
                    raise DomainError("push_owner_changed", "Привязка браузера изменилась.", 409)
                account_id = row.account_id
            try:
                return webpush(
                    subscription_info=subscription,
                    data=json.dumps(
                        {key: payload[key] for key in ("title", "body", "target", "notification_id", "tag")},
                        ensure_ascii=False,
                    ),
                    vapid_private_key=self.settings.push_private_key,
                    vapid_claims={"sub": self.settings.push_subject},
                    timeout=10,
                    ttl=300,
                    requests_session=_NoRedirectPushSession(),
                )
            except WebPushException as exc:
                if exc.response is not None and exc.response.status_code in {404, 410}:
                    self.remove_push(account_id, recipient)
                    raise DomainError("push_disabled", "Подписка браузера истекла.", 409) from None
                raise DomainError("push_delivery_unknown", "Не удалось доставить уведомление.", 503) from None
        raise DomainError("notification_channel_invalid", "Канал недоступен.", 400)


class _TelegramRetryAfter(DomainError):
    def __init__(self, seconds, *, code="telegram_rate_limited"):
        super().__init__(code, "Telegram попросил отложить отправку.", 429)
        self.seconds = seconds


class _NoRedirectPushSession:
    def post(self, url, **kwargs):
        import requests

        kwargs["allow_redirects"] = False
        with requests.Session() as session:
            session.trust_env = False
            return session.post(url, **kwargs)
