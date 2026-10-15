"""Общий авторизованный фасад Telegram-администрирования; без сетевых отправок."""

import re
import secrets
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sunrise_core.content import validate_content
from sunrise_core.errors import DomainError, missing

from .commerce import iso, successful_order_history_clause, successful_payment_history_clause
from .db import (
    Account,
    Agreement,
    Alias,
    Audit,
    Block,
    CreditLedger,
    Device,
    Job,
    Ledger,
    Order,
    Payment,
    Product,
    ProviderOperation,
    Quote,
    Refund,
    Session,
    Setting,
    Settlement,
    Subscription,
    now,
    uid,
)
from .legal import Legal


class BotAdmin:
    def __init__(self, admin, settings, telegram_recipient_allowlist=None):
        self.admin, self.db, self.settings = admin, admin.db, settings
        configured = (getattr(settings, "test_telegram_recipients", "") or "").split(",")
        self.telegram_recipient_allowlist = frozenset(
            telegram_recipient_allowlist if telegram_recipient_allowlist is not None else configured
        )
        self.admin.commerce.telegram_notification_allowlist = self.telegram_recipient_allowlist
        self.admin.commerce.owner_id = getattr(settings, "owner_id", None)
        self.admin.commerce.public_settings = settings

    def authorize_admin(self, actor, capability="users_read"):
        from .staff_permissions import StaffPermissions

        with self.db.transaction() as session:
            StaffPermissions(self.admin.accounts).require_in(session, actor, capability, bot=True)
        return True

    def capabilities(self, actor):
        from .staff_permissions import StaffPermissions

        self.authorize_admin(actor)
        return StaffPermissions(self.admin.accounts).describe(actor, bot=True)

    def _mutation(self, actor, reason, confirmed, capability="settings_write", *, dangerous=True):
        self.authorize_admin(actor, capability)
        # Telegram admin actions use allowlisted identity and capabilities.
        # Fresh TOTP is enforced at the website request boundary only.
        if confirmed is not True:
            raise DomainError("confirmation_required", "Подтвердите изменение.", 400)
        if not isinstance(reason, str) or not 5 <= len(reason.strip()) <= 500:
            raise DomainError("reason_required", "Укажите причину изменения.", 400)

    @staticmethod
    def _page(page, limit):
        if type(page) is not int or type(limit) is not int or page < 1 or not 1 <= limit <= 50:
            raise DomainError("page_invalid", "Проверьте номер страницы.", 400)

    def _paged(self, session, query, page, limit, serializer):
        self._page(page, limit)
        total = session.scalar(select(func.count()).select_from(query.subquery()))
        rows = session.scalars(query.offset((page - 1) * limit).limit(limit))
        return {"items": [serializer(row) for row in rows], "total": total, "page": page, "page_size": limit}

    def bot_products(self, actor):
        self.authorize_admin(actor, "catalog_read")
        result = self.admin.list_items("catalog", page_size=200)
        return result | {"catalog_description": self.admin.public_content().get("catalog_description", "")}

    def bot_product_save(self, actor, product_id, patch, reason, confirmed=False):
        self._mutation(actor, reason, confirmed, "catalog_write")
        if not isinstance(patch, dict):
            raise DomainError("catalog_invalid", "Проверьте поля товара.", 400)
        if "name" in patch and (not isinstance(patch["name"], str) or not 1 <= len(patch["name"]) <= 200):
            raise DomainError("catalog_invalid", "Название: от 1 до 200 символов.", 400)
        if "description" in patch and (
            not isinstance(patch["description"], str) or len(patch["description"]) > 4000
        ):
            raise DomainError("catalog_invalid", "Описание: до 4000 символов.", 400)
        for field in ("price", "extra_device_price"):
            if patch.get(field) is not None:
                try:
                    number = Decimal(patch[field])
                    if (
                        not number.is_finite()
                        or not 0 < number <= 10000000
                        or number != number.quantize(Decimal(".01"))
                    ):
                        raise ValueError()
                except (ValueError, TypeError, InvalidOperation):
                    raise DomainError(
                        "price_invalid", "Нужна положительная цена с точностью до копеек.", 400
                    ) from None
        return (
            self.admin.catalog_update(actor, product_id, patch, reason)
            if product_id
            else self.admin.catalog_create(actor, patch, reason)
        )

    def bot_catalog_intro_save(self, actor, content, reason, confirmed=False):
        self._mutation(actor, reason, confirmed, "content_write")
        nodes = content if isinstance(content, list) else [{"type": "text", "text": content}]
        try:
            validate_content(nodes)
        except ValueError as exc:
            raise DomainError("content_invalid", str(exc), 400) from None
        if isinstance(content, list):
            from .mail import render_mail_text

            content = render_mail_text(content)
        if not isinstance(content, str) or len(content) > 4000:
            raise DomainError("content_invalid", "Описание каталога: до 4000 символов.", 400)
        value = self.admin.public_content() | {
            "catalog_description": content,
            "catalog_description_content": nodes,
        }
        return self.admin.update_public_content(actor, value, reason)

    def bot_settings(self, actor):
        self.authorize_admin(actor, "settings_write")
        with self.db.transaction() as s:
            return {
                "panel_mapping": self.admin.panel_mapping(),
                "legal": Legal(self.admin.commerce).current(s),
                "recurring": self.admin.commerce._setting(s, "recurring", {"enabled": False}),
                "fee_estimates": self.admin.commerce._setting(s, "accounting_fee_estimates", {}),
                "catalog_description": self.admin.public_content().get("catalog_description", ""),
            }

    def bot_settings_update(self, actor, section, values, reason, confirmed=False):
        self._mutation(actor, reason, confirmed)
        if not isinstance(values, dict):
            raise DomainError("setting_invalid", "Проверьте настройку.", 400)
        if section == "panel_mapping" and set(values) == {"external_squad_uuid", "project_prefix"}:
            return self.admin.update_panel_mapping(actor, **values, reason=reason)
        if (
            section == "legal"
            and "documents" in values
            and not set(values) - {"documents", "partial", "expected_revision"}
        ):
            if "partial" in values and type(values["partial"]) is not bool:
                raise DomainError("setting_invalid", "Проверьте настройку документа.", 400)
            return Legal(self.admin.commerce).configure(
                actor,
                values["documents"],
                reason,
                partial=values.get("partial", False),
                expected_revision=values.get("expected_revision"),
            )
        if section == "recurring" and set(values) == {"enabled"} and type(values["enabled"]) is bool:
            return self.admin.toggle(actor, values["enabled"], reason)
        if section == "catalog_description" and set(values) == {"text"}:
            return self.bot_catalog_intro_save(actor, values["text"], reason, confirmed=True)
        if section == "fee_estimates":
            clean = {}
            try:
                if len(values) > 20:
                    raise ValueError()
                for method, rule in values.items():
                    if not re.fullmatch(r"[a-z0-9_]{1,40}", method) or set(rule) != {
                        "percent",
                        "fixed",
                        "currency",
                    }:
                        raise ValueError()
                    percent, fixed = Decimal(rule["percent"]), Decimal(rule["fixed"])
                    if (
                        not percent.is_finite()
                        or not fixed.is_finite()
                        or not 0 <= percent <= 100
                        or not 0 <= fixed <= 1000000
                        or not re.fullmatch(r"[A-Z]{3}", rule["currency"])
                    ):
                        raise ValueError()
                    clean[method] = {
                        "percent": str(percent),
                        "fixed": str(fixed),
                        "currency": rule["currency"],
                    }
            except (ValueError, TypeError, KeyError, InvalidOperation):
                raise DomainError(
                    "fee_estimate_invalid", "Проверьте метод, валюту, процент и фиксированную комиссию.", 400
                ) from None
            return self.admin._save_setting(actor, "accounting_fee_estimates", clean, reason)
        raise DomainError("setting_invalid", "Неизвестная настройка.", 400)

    def bot_users_search(self, actor, query, page=1, limit=10):
        self.authorize_admin(actor)
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 320:
            raise DomainError("search_invalid", "Введите точный ID или email.", 400)
        query = query.strip()
        with self.db.transaction() as s:
            conditions = [
                func.lower(Account.email) == query.lower(),
                func.lower(Account.telegram_username) == query.lstrip("@").lower(),
            ]
            if query.isascii() and query.isdigit() and len(query) <= 19 and int(query) <= 9223372036854775807:
                number = int(query)
                conditions += [
                    Account.id == number,
                    Account.telegram_id == number,
                    Account.id.in_(select(Alias.canonical_id).where(Alias.old_id == number)),
                ]
            return self._paged(
                s, select(Account).where(or_(*conditions)).order_by(Account.id), page, limit, self.admin._row
            )

    def bot_user_detail(self, actor, target_id, page=1, limit=10):
        self.authorize_admin(actor)
        with self.db.transaction() as s:
            account = self.admin.commerce._account(s, target_id, customer=False)
            from .moderation import DeviceModeration
            from .subscription_archive import unarchived_subscription_clause

            result = {
                "account": self.admin._row(account),
                **DeviceModeration(self.admin.commerce).summary(s, account),
                "can_moderate": DeviceModeration.can_moderate(s.get(Account, int(actor)), self.settings),
            }
            for key, model in [("subscriptions", Subscription), ("payments", Payment), ("ledger", Ledger)]:
                serialize = (
                    self.admin._row
                    if model is not Ledger
                    else lambda x: {
                        "id": x.id,
                        "operation": x.operation,
                        "amount": str(x.amount),
                        "created_at": iso(x.created_at),
                    }
                )
                query = select(model).where(model.account_id == account.id).order_by(model.id)
                if model is Subscription:
                    query = query.where(unarchived_subscription_clause())
                elif model is Payment:
                    query = query.where(successful_payment_history_clause())
                result[key] = self._paged(
                    s,
                    query,
                    page,
                    limit,
                    serialize,
                )
            result["identities"] = {
                "email": account.email,
                "email_verified": account.email_verified,
                "telegram_id": str(account.telegram_id) if account.telegram_id else None,
                "telegram_username": account.telegram_username,
                "aliases": [
                    str(a) for a in s.scalars(select(Alias.old_id).where(Alias.canonical_id == account.id))
                ],
            }
            subscriptions = select(Subscription.id).where(Subscription.account_id == account.id)
            sections = [
                (
                    "orders",
                    select(Order)
                    .where(Order.account_id == account.id, successful_order_history_clause())
                    .order_by(Order.created_at.desc()),
                    self.admin._row,
                ),
                (
                    "devices",
                    select(Device)
                    .where(Device.subscription_id.in_(subscriptions.where(unarchived_subscription_clause())))
                    .order_by(Device.id),
                    lambda d: {
                        "id": d.id,
                        "subscription_id": d.subscription_id,
                        "name": d.name,
                        "revoked": d.revoked,
                        "last_seen_at": iso(d.last_seen_at),
                    },
                ),
                (
                    "sessions",
                    select(Session)
                    .where(Session.account_id == account.id)
                    .order_by(Session.expires_at.desc()),
                    lambda x: {
                        "expires_at": iso(x.expires_at),
                        "confirmed_at": iso(x.confirmed_at),
                        "mfa_verified": x.mfa_at is not None,
                    },
                ),
                (
                    "blocks",
                    select(Block)
                    .where(Block.context == "account:" + str(account.id))
                    .order_by(Block.started_at.desc()),
                    self.admin._row,
                ),
                (
                    "agreements",
                    select(Agreement)
                    .where(Agreement.account_id == account.id)
                    .order_by(Agreement.created_at.desc()),
                    lambda x: {
                        "id": x.id,
                        "subscription_id": x.subscription_id,
                        "state": x.state,
                        "amount": str(x.consent_amount),
                        "currency": x.currency,
                        "next_charge_at": iso(x.next_charge_at),
                    },
                ),
                (
                    "credit_ledger",
                    select(CreditLedger)
                    .where(CreditLedger.account_id == account.id)
                    .order_by(CreditLedger.created_at.desc()),
                    lambda x: {
                        "id": x.id,
                        "amount": str(x.amount),
                        "currency": x.currency,
                        "provenance": x.provenance,
                        "created_at": iso(x.created_at),
                    },
                ),
                (
                    "audit",
                    select(Audit)
                    .where(or_(Audit.target == str(account.id), Audit.target.in_(subscriptions)))
                    .order_by(Audit.created_at.desc()),
                    lambda x: {
                        "id": x.id,
                        "action": x.action,
                        "target": x.target,
                        "reason": x.reason,
                        "created_at": iso(x.created_at),
                    },
                ),
            ]
            for key, query, serializer in sections:
                result[key] = self._paged(s, query, page, limit, serializer)
            return result

    def bot_user_invitees(self, actor, target_id, page=1, limit=10):
        self.authorize_admin(actor)
        return self.admin.user_invitees(target_id, page, limit)

    def bot_user_moderate(self, actor, target_id, action, reason, idempotency_key, confirmed=False):
        self._mutation(actor, reason, confirmed, "device_unlinks_manage")
        from .moderation import DeviceModeration

        return DeviceModeration(self.admin.commerce).moderate(
            actor,
            target_id,
            action,
            reason,
            idempotency_key,
            self.settings,
            confirmed,
            require_fresh_mfa=False,
        )

    def bot_owner_device_notice(self, actor, event_id):
        from uuid import UUID

        from .db import DeviceUnlink
        from .staff_permissions import StaffPermissions

        if not isinstance(event_id, str) or not re.fullmatch(r"[a-f0-9]{32}|[a-f0-9-]{36}", event_id):
            raise missing()
        try:
            event_id = str(UUID(event_id))
        except ValueError:
            raise missing() from None
        with self.db.transaction() as session:
            staff = StaffPermissions(self.admin.accounts)
            owner = staff.require_in(session, actor, "device_unlinks_manage", bot=True)
            if staff.role_in(session, owner, bot=True) != "owner":
                raise missing()
            event = session.get(DeviceUnlink, event_id)
            from .moderation import device_unlink_event_in

            policy = getattr(self.admin.commerce, "runtime_policy", None)
            target, _ = device_unlink_event_in(session, event, policy)
            return {"account_id": str(target.id), "event_id": event.id}

    def bot_grant(
        self,
        actor,
        target,
        product_id,
        days,
        key,
        reason,
        confirmed=False,
        *,
        subscription_id=None,
        devices=None,
    ):
        from .admin_operations import AdminOperations

        self._mutation(actor, reason, confirmed, "subscriptions_manage")
        return AdminOperations(self.admin.commerce, self.settings).grant(
            actor,
            target,
            product_id,
            days,
            key,
            reason,
            confirmed=confirmed,
            subscription_id=subscription_id,
            devices=devices,
            require_fresh_mfa=False,
        )

    def bot_message(self, actor, target, nodes, key, reason, confirmed=False):
        from .admin_operations import AdminOperations

        self._mutation(actor, reason, confirmed, "messages_write", dangerous=False)
        return AdminOperations(self.admin.commerce, self.settings).message(
            actor, target, nodes, key, reason, confirmed=confirmed
        )

    def bot_backups(self, actor):
        from .admin_operations import AdminOperations

        return AdminOperations(self.admin.commerce, self.settings).backups(actor)

    def bot_request_backup(self, actor, key, reason, confirmed=False):
        from .admin_operations import AdminOperations

        self._mutation(actor, reason, confirmed, "backups_manage")
        return AdminOperations(self.admin.commerce, self.settings).request_backup(
            actor, key, reason, confirmed=confirmed, require_fresh_mfa=False
        )

    def pending_owner_alerts(self, actor, limit=10):
        self._page(1, limit)
        from .moderation import DeviceModeration

        with self.db.transaction() as s:
            DeviceModeration(self.admin.commerce).authorize_notification_owner(s, actor, self.settings)
            owner = str(self.settings.owner_id)
            if owner not in self.telegram_recipient_allowlist:
                raise DomainError("recipient_denied", "Получатель не разрешен настройками стенда.", 403)
            for job in s.scalars(
                select(Job)
                .where(
                    Job.kind == "owner_device_unlink",
                    Job.state == "in_flight",
                    Job.available_at <= now(),
                    Job.payload["target_id"].as_string() == owner,
                )
                .with_for_update(skip_locked=True)
                .limit(100)
            ):
                job.state, job.last_error = "review", "delivery_outcome_unknown"
            items = list(
                s.scalars(
                    select(Job)
                    .where(
                        Job.kind == "owner_device_unlink",
                        Job.state == "pending",
                        Job.available_at <= now(),
                        Job.payload["target_id"].as_string() == owner,
                    )
                    .order_by(Job.available_at, Job.id)
                    .limit(limit)
                )
            )
            return {"items": [{"id": j.id, "target_id": owner, "payload": j.payload} for j in items]}

    def claim_owner_alert(self, actor, job_id):
        from .moderation import DeviceModeration

        with self.db.transaction() as s:
            DeviceModeration(self.admin.commerce).authorize_notification_owner(s, actor, self.settings)
            owner = str(self.settings.owner_id)
            if owner not in self.telegram_recipient_allowlist:
                raise DomainError("recipient_denied", "Получатель не разрешен настройками стенда.", 403)
            job = s.scalar(select(Job).where(Job.id == job_id).with_for_update())
            if not job or job.kind != "owner_device_unlink" or job.payload.get("target_id") != owner:
                raise missing()
            if job.state == "in_flight" and job.available_at <= now():
                job.state, job.last_error = "review", "delivery_outcome_unknown"
            if job.state != "pending" or job.available_at > now():
                return None
            job.state, job.attempts = "in_flight", job.attempts + 1
            job.available_at = now() + timedelta(minutes=5)
            return {"id": job.id, "target_id": owner, "payload": job.payload}

    def fail_owner_alert(self, actor, job_id, *, state="review", retry_after=None):
        from .moderation import DeviceModeration

        if state not in {"review", "blocked", "pending"} or (
            state == "pending" and (type(retry_after) is not int or not 1 <= retry_after <= 3600)
        ):
            raise DomainError("delivery_state_invalid", "Некорректный результат доставки.", 400)
        with self.db.transaction() as s:
            DeviceModeration(self.admin.commerce).authorize_notification_owner(s, actor, self.settings)
            job = s.scalar(select(Job).where(Job.id == job_id).with_for_update())
            if (
                not job
                or job.kind != "owner_device_unlink"
                or job.payload.get("target_id") != str(self.settings.owner_id)
            ):
                raise missing()
            if job.state != "in_flight":
                return {"id": job.id, "state": job.state}
            if state == "pending":
                job.available_at = now() + timedelta(seconds=retry_after)
                state = "pending" if job.attempts <= 3 else "review"
                job.last_error = "telegram_rate_limited"
            else:
                job.last_error = (
                    "recipient_not_authorized" if state == "blocked" else "delivery_outcome_unknown"
                )
            job.state = state
            return {"id": job.id, "state": job.state}

    def acknowledge_owner_alert(self, actor, job_id):
        from .moderation import DeviceModeration

        with self.db.transaction() as s:
            DeviceModeration(self.admin.commerce).authorize_notification_owner(s, actor, self.settings)
            if str(self.settings.owner_id) not in self.telegram_recipient_allowlist:
                raise DomainError("recipient_denied", "Получатель не разрешен настройками стенда.", 403)
            j = s.scalar(select(Job).where(Job.id == job_id).with_for_update())
            if (
                not j
                or j.kind != "owner_device_unlink"
                or j.payload.get("target_id") != str(self.settings.owner_id)
            ):
                raise missing()
            if j.state not in {"in_flight", "done"}:
                raise DomainError("delivery_claim_required", "Доставка не была подтверждена.", 409)
            j.state = "done"
            return {"id": j.id, "state": j.state}

    def bot_referrals(self, actor, metric="rewards", page=1, limit=20):
        self.authorize_admin(actor, "referrals_read")
        self._page(page, limit)
        if metric not in {"rewards", "invited"}:
            raise DomainError("metric_invalid", "Выберите рейтинг.", 400)
        with self.db.transaction() as s:
            aliases = dict(s.execute(select(Alias.old_id, Alias.canonical_id)).all())
            invited, rewards = Counter(), defaultdict(Decimal)
            from .referrals import referral_operation_condition

            for referrer in s.scalars(select(Account.referrer_id).where(Account.referrer_id.is_not(None))):
                invited[aliases.get(referrer, referrer)] += 1
            for row in s.scalars(select(Ledger).where(referral_operation_condition())):
                rewards[aliases.get(row.account_id, row.account_id)] += row.amount
            rows = []
            for a in s.scalars(select(Account).where(Account.id.in_(set(invited) | set(rewards)))):
                rows.append(
                    {
                        "account_id": str(a.id),
                        "telegram_id": str(a.telegram_id) if a.telegram_id else None,
                        "email": a.email,
                        "invited_users": invited[a.id],
                        "rewards": str(rewards[a.id]),
                    }
                )
            rows.sort(
                key=lambda x: (
                    Decimal(x["rewards"]) if metric == "rewards" else x["invited_users"],
                    int(x["account_id"]),
                ),
                reverse=True,
            )
            return {
                "items": rows[(page - 1) * limit : page * limit],
                "total": len(rows),
                "page": page,
                "page_size": limit,
                "metric": metric,
            }

    @staticmethod
    def _method(operation):
        value = (operation.request or {}).get("payment_method") if operation else None
        return value if isinstance(value, str) and re.fullmatch(r"[a-z0-9_]{1,40}", value) else None

    def _money(self, rows, settlements, rules, refunds):
        totals = {}
        for p, _o, _q, operation in rows:
            result = totals.setdefault(
                p.currency,
                {
                    "currency": p.currency,
                    "gross": Decimal(0),
                    "actual_fee": Decimal(0),
                    "actual_net": Decimal(0),
                    "actual_net_count": 0,
                    "unknown_fee_count": 0,
                    "estimated_fee": Decimal(0),
                    "estimated_net": Decimal(0),
                    "estimated_count": 0,
                    "refunds": Decimal(0),
                    "settlement_net_usdt": Decimal(0),
                    "settlement_net_count": 0,
                    "actual_fees_by_currency": {},
                    "cross_currency_fee_count": 0,
                },
            )
            result["gross"] += p.amount
            events = settlements.get(p.id, [])
            fee_events = [e for e in events if e.fee is not None and e.fee_currency]
            actual = defaultdict(Decimal)
            if fee_events:
                for event in fee_events:
                    actual[event.fee_currency] += event.fee
            elif p.fee is not None and p.fee_currency:
                actual[p.fee_currency] += p.fee
            for currency, value in actual.items():
                result["actual_fees_by_currency"][currency] = (
                    result["actual_fees_by_currency"].get(currency, Decimal(0)) + value
                )
            fee = actual[p.currency] if actual and set(actual) == {p.currency} else None
            if fee is not None:
                result["actual_fee"] += fee
                result["actual_net"] += p.amount - fee
                result["actual_net_count"] += 1
            elif actual:
                result["cross_currency_fee_count"] += 1
            else:
                result["unknown_fee_count"] += 1
                rule = rules.get(self._method(operation))
                if rule and rule["currency"] == p.currency:
                    estimate = (p.amount * Decimal(rule["percent"]) / 100 + Decimal(rule["fixed"])).quantize(
                        Decimal(".01")
                    )
                    result["estimated_fee"] += estimate
                    result["estimated_net"] += p.amount - estimate
                    result["estimated_count"] += 1
            net_events = [e for e in events if e.net_usdt is not None]
            net = sum((e.net_usdt for e in net_events), Decimal(0)) if net_events else p.net_usdt
            if net is not None:
                result["settlement_net_usdt"] += net
                result["settlement_net_count"] += 1
        for currency, amount in refunds.items():
            result = totals.setdefault(
                currency,
                {
                    "currency": currency,
                    "gross": Decimal(0),
                    "actual_fee": Decimal(0),
                    "actual_net": Decimal(0),
                    "actual_net_count": 0,
                    "unknown_fee_count": 0,
                    "estimated_fee": Decimal(0),
                    "estimated_net": Decimal(0),
                    "estimated_count": 0,
                    "refunds": Decimal(0),
                    "settlement_net_usdt": Decimal(0),
                    "settlement_net_count": 0,
                    "actual_fees_by_currency": {},
                    "cross_currency_fee_count": 0,
                },
            )
            result["refunds"] += amount
        for result in totals.values():
            result["actual_fees_by_currency"] = {
                c: str(v) for c, v in result["actual_fees_by_currency"].items()
            }
            result["actual_net"] = result["actual_net"] if result["actual_net_count"] else None
            result["settlement_net_usdt"] = (
                result["settlement_net_usdt"] if result["settlement_net_count"] else None
            )
            result["fee_time_basis"] = "paid_cohort_latest_confirmed"
            result["actual_net_provenance"] = "calculated_from_confirmed_same_currency_fee"
        return [
            {k: str(v) if isinstance(v, Decimal) else v for k, v in x.items()}
            for _, x in sorted(totals.items())
        ]

    def bot_statistics(self, actor, start_date=None, end_date=None):
        self.authorize_admin(actor, "statistics_read")
        zone = ZoneInfo("Europe/Moscow")
        today = now().astimezone(zone).date()
        try:
            first = date.fromisoformat(start_date) if start_date else today
            last = date.fromisoformat(end_date) if end_date else today
            if first > last:
                raise ValueError()
            start = datetime.combine(first, time.min, zone).astimezone(UTC)
            end = datetime.combine(last + timedelta(days=1), time.min, zone).astimezone(UTC)
        except (ValueError, TypeError, OverflowError):
            raise DomainError("range_invalid", "Период: ГГГГ-ММ-ДД, начало не позже конца.", 400) from None
        with self.db.transaction() as s:
            records = list(
                s.execute(
                    select(Payment, Order, Quote, ProviderOperation)
                    .join(Order, Order.id == Payment.order_id)
                    .join(Quote, Quote.id == Order.quote_id)
                    .outerjoin(ProviderOperation, ProviderOperation.order_id == Order.id)
                    .where(
                        Payment.environment == "live",
                        Payment.status.in_(["paid", "refunded", "partially_refunded", "chargeback"]),
                    )
                )
            )
            selected = [r for r in records if start <= r[0].paid_at < end]
            today_start = datetime.combine(today, time.min, zone).astimezone(UTC)
            todays = [r for r in records if today_start <= r[0].paid_at < today_start + timedelta(days=1)]
            settlements = defaultdict(list)
            for event in s.scalars(
                select(Settlement).where(
                    Settlement.payment_id.in_([r[0].id for r in records]),
                    Settlement.source == "provider_confirmed",
                )
            ):
                settlements[event.payment_id].append(event)
            refund_rows = list(
                s.execute(
                    select(Refund, Payment.currency)
                    .join(Payment, Payment.id == Refund.payment_id)
                    .where(
                        Payment.environment == "live",
                        Payment.status.in_(["paid", "refunded", "partially_refunded", "chargeback"]),
                    )
                )
            )

            def refunds_between(begin, finish):
                total = defaultdict(Decimal)
                for refund, currency in refund_rows:
                    if begin <= refund.occurred_at < finish:
                        total[currency] += refund.amount
                return total

            rules = self.admin.commerce._setting(s, "accounting_fee_estimates", {})
            methods = Counter(self._method(r[3]) for r in records)
            denominator = sum(count for method, count in methods.items() if method)
            popular = {}
            for p, o, q, _ in records:
                key = (
                    q.snapshot.get("product_id"),
                    "devices" if q.snapshot.get("action") == "devices" else "tariff",
                    p.currency,
                )
                row = popular.setdefault(
                    key,
                    {
                        "product_id": key[0],
                        "action": key[1],
                        "currency": key[2],
                        "purchases": set(),
                        "gross": Decimal(0),
                    },
                )
                row["purchases"].add(o.id)
                row["gross"] += p.amount
            products = {p.id: p.name for p in s.scalars(select(Product))}
            popular_rows = [
                row
                | {
                    "purchases": len(row["purchases"]),
                    "gross": str(row["gross"]),
                    "name": products.get(row["product_id"], "Исторический товар"),
                }
                for row in popular.values()
            ]
            popular_rows.sort(key=lambda r: r["purchases"], reverse=True)
            total_purchases = len({r[1].id for r in records})
            for row in popular_rows:
                row["count"] = row["purchases"]
                row["percent"] = (
                    str((Decimal(row["count"]) * 100 / total_purchases).quantize(Decimal(".01")))
                    if total_purchases
                    else "0.00"
                )
            recurring = s.scalar(
                select(func.count(func.distinct(Agreement.account_id)))
                .join(Subscription, Subscription.id == Agreement.subscription_id)
                .where(Agreement.state.in_(["active", "enabled"]), Subscription.environment == "live")
            )
            return {
                "period": {
                    "start_date": first.isoformat(),
                    "end_date": last.isoformat(),
                    "timezone": "Europe/Moscow",
                },
                "period_money": self._money(selected, settlements, rules, refunds_between(start, end)),
                "today_money": self._money(
                    todays, settlements, rules, refunds_between(today_start, today_start + timedelta(days=1))
                ),
                "all_time_money": self._money(
                    records,
                    settlements,
                    rules,
                    refunds_between(datetime.min.replace(tzinfo=UTC), datetime.max.replace(tzinfo=UTC)),
                ),
                "payment_methods": [
                    {
                        "method": method,
                        "count": count,
                        "percent": str((Decimal(count) * 100 / denominator).quantize(Decimal(".01"))),
                        "denominator": denominator,
                    }
                    for method, count in sorted(methods.items(), key=lambda x: x[0] or "")
                    if method
                ],
                "unknown_method_count": methods[None],
                "purchase_count": total_purchases,
                "sales_period": "all_time",
                "payment_methods_period": "all_time",
                "popular_products_period": "all_time",
                "purchase_count_period": "all_time",
                "telegram_users": s.scalar(
                    select(func.count()).select_from(Account).where(Account.telegram_id.is_not(None))
                ),
                "recurring_users": recurring,
                "popular_products": popular_rows,
                "provenance": "provider_confirmed_live_only",
                "estimated_provenance": "admin_accounting_assumption",
                "checked_at": iso(now()),
            }

    def _utm_row(self, row):
        from .campaigns import Campaigns

        return Campaigns(self.admin.commerce, self.settings)._row(row) | {
            "start_code": "utm_" + row.key.removeprefix("utm:"),
        }

    def bot_utm_create(self, actor, values, reason, confirmed=False):
        self._mutation(actor, reason, confirmed, "campaigns_write", dangerous=False)
        if (
            not isinstance(values, dict)
            or set(values) != {"name", "source", "medium", "campaign"}
            or any(not isinstance(v, str) or not 1 <= len(v.strip()) <= 100 for v in values.values())
        ):
            raise DomainError("utm_invalid", "Заполните название, источник, канал и кампанию.", 400)
        bot_url = urlsplit(getattr(self.settings, "bot_url", None) or "")
        if (
            bot_url.scheme != "https"
            or bot_url.hostname != "t.me"
            or not re.fullmatch(r"/[A-Za-z0-9_]{5,32}", bot_url.path)
        ):
            raise DomainError("bot_url_required", "Настройте публичную ссылку тестового бота.", 400)
        token = secrets.token_urlsafe(18)
        value = values | {
            "url": "https://t.me" + bot_url.path + "?start=utm_" + token,
            "created_at": iso(now()),
        }
        with self.db.transaction() as s:
            row = Setting(key="utm:" + token, value=value)
            s.add(row)
            self.admin.audit(s, actor, "utm_create", token, reason, {}, value)
            return self._utm_row(row)

    def bot_utm_list(self, actor, page=1, limit=10):
        from .campaigns import Campaigns

        self._page(page, limit)
        rows = Campaigns(self.admin.commerce, self.settings).list(actor)["items"]
        return {
            "items": [
                {**row, "start_code": "utm_" + row["id"]} for row in rows[(page - 1) * limit : page * limit]
            ],
            "total": len(rows),
            "page": page,
            "page_size": limit,
        }

    def record_utm_start(self, account_id, start_code):
        if not isinstance(start_code, str) or not re.fullmatch(r"utm_[A-Za-z0-9_-]{24}", start_code):
            return False
        token = start_code[4:]
        with self.db.transaction() as s:
            link = s.get(Setting, "utm:" + token)
            if not link:
                return False
            account = self.admin.commerce._account(s, account_id, True)
            s.add(
                Job(
                    key="utm_start:" + uid(),
                    kind="utm_start_event",
                    account_id=account.id,
                    state="done",
                    payload={"link_id": token, "occurred_at": iso(now()), "source": "telegram"},
                )
            )
            key = "utm_account:" + str(account.id)
            if not s.get(Setting, key):
                s.add(
                    Setting(
                        key=key,
                        value={
                            "link_id": token,
                            "account_id": str(account.id),
                            "attributed_at": iso(now()),
                            "model": "first_touch",
                        },
                    )
                )
            return True

    def bot_utm_stats(self, actor, link_id):
        from .campaigns import Campaigns

        self.authorize_admin(actor, "campaigns_read")
        web = Campaigns(self.admin.commerce, self.settings).stats(actor, link_id)
        with self.db.transaction() as s:
            if not s.get(Setting, "utm:" + link_id):
                raise missing()
            events = list(
                s.scalars(
                    select(Job).where(
                        Job.kind == "utm_start_event", Job.payload["link_id"].as_string() == link_id
                    )
                )
            )
            aliases = dict(s.execute(select(Alias.old_id, Alias.canonical_id)).all())
            unique = {aliases.get(j.account_id, j.account_id) for j in events}
            attribution = [r for r in s.scalars(select(Setting).where(Setting.key.like("utm_account:%")))]
            first_touch = {}
            for row in attribution:
                canonical = aliases.get(int(row.value["account_id"]), int(row.value["account_id"]))
                prior = first_touch.get(canonical)
                if prior is None or (row.value["attributed_at"], row.key) < (
                    prior.value["attributed_at"],
                    prior.key,
                ):
                    first_touch[canonical] = row
            owned = {
                canonical for canonical, row in first_touch.items() if row.value.get("link_id") == link_id
            }
            # Shared account first-touch applies to both Telegram and website orders.
            gross = defaultdict(Decimal)
            orders = set()
            for p in s.scalars(
                select(Payment).where(
                    Payment.account_id.in_(owned),
                    Payment.environment == "live",
                    Payment.status.in_(["paid", "refunded", "partially_refunded", "chargeback"]),
                )
            ):
                stamp = datetime.fromisoformat(first_touch[p.account_id].value["attributed_at"])
                if p.paid_at >= stamp:
                    gross[p.currency] += p.amount
                    orders.add(p.order_id)
            return {
                "id": link_id,
                "starts": len(events),
                "unique_users": len(unique),
                "paid_orders": len(orders),
                "gross": [{"currency": c, "amount": str(v)} for c, v in sorted(gross.items())],
                "attribution_model": "canonical_account_first_touch",
                "website_visits": web["web_visits"],
                "website_unique_visitors": web["web_unique_visitors"],
                "website_paid_orders": web["paid_orders"],
                "website_turnover": web["turnover"],
                "website_currency": web["currency"],
                "website_attribution_model": web["attribution"],
            }

    def _broadcast_recipients(self):
        allowed = self.telegram_recipient_allowlist
        if (
            not self.admin.commerce.local_test
            or not allowed
            or any(not re.fullmatch(r"[1-9][0-9]{0,18}", e) for e in allowed)
        ):
            raise DomainError(
                "recipient_denied", "Нужны разрешённые синтетические получатели локального стенда.", 403
            )
        with self.db.transaction() as s:
            existing = {
                str(tg)
                for tg in s.scalars(select(Account.telegram_id).where(Account.telegram_id.is_not(None)))
            }
        return sorted(existing & allowed)

    def register_media(self, actor, chunks, mime):
        from .broadcast_media_registry import BroadcastMediaRegistry

        return BroadcastMediaRegistry(self.admin.commerce, self.settings).register(actor, chunks, mime)

    def resolve_media(self, actor, media):
        from .broadcast_media_registry import BroadcastMediaRegistry

        return BroadcastMediaRegistry(self.admin.commerce, self.settings).resolve(actor, media)

    def bot_broadcast_preview(self, actor, nodes, *, media=None):
        from .broadcasts import BroadcastService

        return BroadcastService(self.admin.commerce, self.settings).preview(actor, nodes, media=media)

    def _broadcast_row(self, job):
        if not job or job.kind != "broadcast" or job.payload.get("source") != "telegram_admin":
            raise missing()
        delivered = job.payload.get("delivered_recipients", [])
        return {
            "id": job.id,
            "state": job.state,
            "recipient_count": len(job.payload.get("recipients", [])),
            "delivered_count": len(delivered),
            "failed_count": len(job.payload.get("failed_recipients", [])),
            "attempts": job.attempts,
            "last_error": job.last_error,
            "nodes": job.payload.get("nodes", []),
            "delivery": "local_artifact_only",
        }

    def bot_broadcast_create(self, actor, nodes, reason, confirmed=False, *, media=None):
        from .broadcasts import BroadcastService

        self._mutation(actor, reason, confirmed, "broadcasts_write", dangerous=False)
        return BroadcastService(self.admin.commerce, self.settings).create(
            actor, nodes, reason, draft=True, media=media
        )

    def bot_broadcast_start(self, actor, jid, reason, confirmed=False):
        from .broadcasts import BroadcastService

        self._mutation(actor, reason, confirmed, "broadcasts_write", dangerous=False)
        return BroadcastService(self.admin.commerce, self.settings).start(actor, jid, reason)

    def bot_broadcast_stop(self, actor, jid, reason, confirmed=False):
        from .broadcasts import BroadcastService

        self._mutation(actor, reason, confirmed, "broadcasts_write", dangerous=False)
        return BroadcastService(self.admin.commerce, self.settings).stop(actor, jid, reason)

    def bot_broadcast_detail(self, actor, jid):
        from .broadcasts import BroadcastService

        return BroadcastService(self.admin.commerce, self.settings).detail(actor, jid)

    def bot_broadcast_list(self, actor, page=1, limit=10):
        from .broadcasts import BroadcastService

        return BroadcastService(self.admin.commerce, self.settings).list(actor, page=page, limit=limit)
