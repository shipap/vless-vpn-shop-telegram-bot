"""Напоминания и закрытые резервные копии разрешённой тестовой базы."""

from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, select, text
from sunrise_core.errors import DomainError

from .db import Account, Audit, Job, Notification, Order, Subscription, now
from .instance_config import configured_instance
from .notifications import NotificationService


class Scheduler:
    def __init__(self, commerce, settings, *, dump_database=None, send_document=None, verify_database=None):
        self.commerce, self.db, self.settings = commerce, commerce.db, settings
        self._dump = dump_database or self._dump_local_database
        self._send = send_document or self._send_owner_document
        self._verify = verify_database or self._verify_local_database

    def sync_panel_routes(self):
        from .panel_event_routes import sync_accessible_nodes

        panel = self.commerce.panel_provider
        if panel is None or getattr(self.settings, "runtime_mode", "isolated") not in {
            "production",
            "scoped_test",
        }:
            return 0
        result = sync_accessible_nodes(self.db, panel, self.settings)
        return int(result["status"] == "ok" and result.get("changed", False))

    def _owner_in(self, session, *, lock=False):
        from types import SimpleNamespace

        from .staff_permissions import StaffPermissions

        owner_id = getattr(self.settings, "owner_id", None)
        if not owner_id or owner_id not in {
            x.strip() for x in self.settings.test_telegram_recipients.split(",")
        }:
            return None
        query = select(Account).where(Account.telegram_id == int(owner_id))
        owner = session.scalar(query.with_for_update() if lock else query)
        permissions = StaffPermissions(
            self.commerce.accounts or SimpleNamespace(db=self.db, settings=self.settings)
        )
        return owner if owner and permissions.role_in(session, owner) == "owner" else None

    def schedule(self, moment=None):
        local = (moment or now()).astimezone(ZoneInfo("Europe/Moscow"))
        if (local.hour, local.minute) < (16, 0):
            return 0
        with self.db.transaction() as session:
            key = "daily-backup:" + self.db.engine.url.database + ":" + local.strftime("%Y-%m-%d")
            session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"), {"key": key})
            if session.scalar(select(Job.id).where(Job.key == key)) or not self._owner_in(session):
                return 0
            owner = self._owner_in(session)
            session.add(
                Job(
                    key=key,
                    kind="daily_backup",
                    account_id=owner.id,
                    payload={
                        "recipient": str(owner.telegram_id),
                        "database": self.db.engine.url.database,
                        "scheduled_date": local.strftime("%Y-%m-%d"),
                        "timezone": "Europe/Moscow",
                    },
                )
            )
            return 1

    EXPIRY_REFRESH_INTERVAL = timedelta(minutes=30)
    # Each refresh is one synchronous panel read inside the worker leader lock (30 s).
    EXPIRY_REFRESH_BATCH = 5
    EXPIRY_REFRESH_SECONDS = 12

    def _expiry_candidates(self, moment, policy):
        """Local expiry is only a hint for which reminder could be due and not yet sent."""
        from . import panel_entitlements
        from .db import Agreement
        from .renewal_reminders import reminder_bucket, reminder_window
        from .retention import fenced_in

        window = reminder_window(policy)
        result = []
        with self.db.transaction() as session:
            for sub in session.scalars(
                select(Subscription)
                .where(
                    Subscription.expires_at.is_not(None),
                    Subscription.expires_at >= moment - timedelta(hours=24),
                    Subscription.expires_at <= moment + window,
                )
                .order_by(Subscription.expires_at, Subscription.id)
            ):
                if fenced_in(session, sub):
                    continue
                # Automatic renewal handles these; a manual-renewal prompt would only push a cancellation.
                if session.scalar(
                    select(Agreement.id)
                    .where(Agreement.subscription_id == sub.id, Agreement.state != "stopped")
                    .limit(1)
                ):
                    continue
                hint = panel_entitlements.verified_service_horizon(session, sub, moment)
                # Only a panel-bound subscription has panel data that may need a refresh.
                fresh = hint is not None or not panel_entitlements.bound(sub)
                hint = hint or sub.expires_at
                bucket = reminder_bucket(policy, (hint - moment).total_seconds())
                if bucket is None:
                    continue
                if fresh:
                    # Exactly the key remind() would write for this term and bucket.
                    sent = select(Notification.id).where(
                        Notification.account_id == sub.account_id,
                        Notification.event_key == f"expiry:{sub.id}:{hint.isoformat()}:{bucket}",
                    )
                else:
                    # The verified horizon is unknown until the refresh; a reminder of this bucket for
                    # the current term can only have been created inside the current reminder window.
                    sent = select(Notification.id).where(
                        Notification.account_id == sub.account_id,
                        Notification.event_key.startswith(f"expiry:{sub.id}:"),
                        Notification.event_key.endswith(":" + bucket),
                        Notification.created_at >= moment - window - timedelta(hours=24),
                    )
                if session.scalar(sent.limit(1)):
                    continue
                result.append((sub.account_id, sub.id, fresh))
        return result

    def _refresh_expiry(self, owner, sid, moment):
        """One targeted read of this project's own panel user, at most every 30 minutes.

        Returns None when throttled (no panel request), else whether the refresh succeeded.
        """
        from .db import Setting

        key = "expiry-refresh:" + sid
        with self.db.transaction() as session:
            session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"), {"key": key})
            marker = session.get(Setting, key, with_for_update=True)
            if marker:
                try:
                    last = datetime.fromisoformat(marker.value["attempted_at"])
                except (KeyError, TypeError, ValueError):
                    last = None
                if last is not None and moment - last < self.EXPIRY_REFRESH_INTERVAL:
                    return None
                marker.value = {"attempted_at": moment.isoformat()}
            else:
                session.add(Setting(key=key, value={"attempted_at": moment.isoformat()}))
        try:
            results = self.commerce._refresh_panel(owner, sid)
        except DomainError:
            return False
        return bool(results) and all(item.get("status") == "ok" for item in results.values())

    def remind(self, moment=None):
        import logging
        import time

        moment = moment or now()
        with self.db.transaction() as session:
            policy = self.commerce._setting(session, "discounts", {})
        attempts = 0
        started = time.monotonic()
        count = 0
        notifier = NotificationService(self.db, self.settings)
        for owner, sid, fresh in self._expiry_candidates(moment, policy):
            if not fresh:
                # Panel data is refreshed only for a subscription whose reminder is due now.
                if self.commerce.panel_provider is None:
                    continue
                if attempts >= self.EXPIRY_REFRESH_BATCH or time.monotonic() - started > self.EXPIRY_REFRESH_SECONDS:
                    continue
                try:
                    refreshed = self._refresh_expiry(owner, sid, moment)
                except Exception:  # noqa: BLE001 -- one subscription must never stop the worker tick
                    logging.getLogger(__name__).warning("renewal reminder refresh skipped", exc_info=True)
                    attempts += 1
                    continue
                if refreshed is None:
                    continue
                attempts += 1
                if not refreshed:
                    continue
            try:
                count += self._remind_one(notifier, policy, owner, sid, moment)
            except Exception:  # noqa: BLE001 -- one subscription must never stop the worker tick
                logging.getLogger(__name__).warning("renewal reminder skipped", exc_info=True)
        return count

    def _remind_one(self, notifier, policy, owner, sid, moment):
        from decimal import Decimal

        from . import panel_entitlements
        from .db import Quote
        from .renewal_reminders import reminder_bucket, reminder_content
        from .retention import fenced_in

        # The price is optional: the reminder must not depend on a successful quote.
        try:
            quote = self.commerce._renewal_offer(owner, sid)
        except DomainError:
            quote = None
        with self.db.transaction() as session:
            account = self.commerce._account(session, owner, True)
            sub = self.commerce._sub(session, account.id, sid, True)
            if fenced_in(session, sub):
                return 0
            expiry = panel_entitlements.verified_service_horizon(session, sub, moment)
            if expiry is None and (panel_entitlements.bound(sub) or sub.expires_at is None):
                return 0
            expiry = expiry or sub.expires_at
            bucket = reminder_bucket(policy, (expiry - moment).total_seconds())
            if bucket is None:
                return 0
            key = f"expiry:{sub.id}:{expiry.isoformat()}:{bucket}"
            if session.scalar(
                select(Notification.id).where(Notification.account_id == account.id, Notification.event_key == key)
            ):
                return 0
            if quote and (
                quote.get("subscription_id") not in {None, sub.id} or quote.get("action") not in {None, "renew"}
            ):
                quote = None
            # The text names only the near-expiry discount the frozen quote actually applied.
            applied = None
            saved = session.get(Quote, quote["id"]) if quote else None
            selected = (saved.snapshot.get("selected_secondary") or {}) if saved else {}
            if selected.get("source") == "near_expiry_renewal":
                applied = Decimal(str(selected["percent"]))
            from .db import Product

            product = session.get(Product, sub.product_id) if sub.product_id else None
            title, body = reminder_content(
                policy,
                product_name=(product.name if product else None) or "VPN-подписка",
                expiry=expiry,
                moment=moment,
                quote=quote,
                applied_percent=applied,
            )
            retention = self.commerce.retention.projection_in(session, sub)
            if retention["cleanup_at"]:
                body += (
                    "\n\nЧерез 7 дней после окончания срока VPN-подписка будет удалена из панели. "
                    "Аккаунт и история платежей сохранятся."
                )
            from .subscription_engagement import SubscriptionEngagement

            notifier.emit_in(
                session,
                account.id,
                key,
                "subscription_expiry",
                title,
                body,
                target="/#subscriptions",
                actions=[SubscriptionEngagement.action(sid, "renew", "Продлить", quote["id"] if quote else None)],
            )
            return 1

    def notify_ready(self, *, scoped_only=False):
        from .db import Quote

        with self.db.transaction() as session:
            policy = getattr(self.commerce, "runtime_policy", None)
            if scoped_only:
                if not policy or not policy.scoped_payments:
                    raise DomainError(
                        "scoped_test_stage_denied", "Операция недоступна в тестовом контуре.", 403
                    )
                policy.require_current(session)
            issued = (
                select(Order.id, Order.account_id)
                .join(Quote, Quote.id == Order.quote_id)
                .join(Subscription, Subscription.id == Order.subscription_id)
                .join(Job, Order.id == func.substr(Job.key, len("provision:") + 1))
                .where(
                    Order.status == "paid",
                    Quote.snapshot["action"].as_string() == "renew",
                    *([Order.environment == "scoped_live"] if scoped_only else []),
                    Subscription.status == "active",
                    Subscription.account_id == Order.account_id,
                    Job.kind == "provision",
                    Job.key.startswith("provision:"),
                    Job.account_id == Order.account_id,
                    Job.payload["subscription_id"].as_string() == Order.subscription_id,
                    Job.state == "done",
                    Job.last_error.is_(None),
                )
                .cte("issued_orders")
                .prefix_with("MATERIALIZED")
            )
            orders = session.scalars(
                select(Order)
                .join(issued, issued.c.id == Order.id)
                .where(
                    select(Notification.id)
                    .where(
                        Notification.account_id == issued.c.account_id,
                        Notification.event_key == "payment:" + issued.c.id,
                    )
                    .exists(),
                    ~select(Notification.id)
                    .where(
                        Notification.account_id == issued.c.account_id,
                        Notification.event_key == "access-ready:" + issued.c.id,
                    )
                    .exists(),
                )
                .order_by(Order.created_at, Order.id)
                .limit(1000)
            )
            notifier = NotificationService(self.db, self.settings)
            for order in orders:
                from .db import Quote, Setting
                from .panel_v3 import instant
                from .retention import fenced_in

                if scoped_only:
                    policy.require_paid_service_in(session, order)

                sub = session.get(Subscription, order.subscription_id)
                if sub is None or fenced_in(session, sub):
                    continue
                paid_quote = session.get(Quote, order.quote_id)
                renewal = paid_quote and paid_quote.snapshot.get("action") == "renew"
                if not renewal:
                    continue
                expiry = sub.expires_at
                if renewal and sub.panel_user_id is not None:
                    job = session.scalar(select(Job).where(Job.key == "provision:" + order.id))
                    record = session.get(Setting, "panel_test:" + sub.id)
                    proof = job.payload.get("applied_target") if job else None
                    try:
                        if (
                            not isinstance(proof, dict)
                            or record is None
                            or job.payload.get("applied_binding") != record.value.get("binding")
                            or job.payload.get("version") != sub.version
                            or proof.get("status") != "ACTIVE"
                            or not now() - timedelta(minutes=10)
                            <= instant(job.payload.get("applied_at"))
                            <= now()
                        ):
                            continue
                        expiry = instant(proof["expires_at"])
                        if expiry <= now():
                            continue
                        from . import panel_entitlements

                        expiry = max(expiry, panel_entitlements.paid_horizon(session, sub, now()) or expiry)
                    except (ValueError, TypeError, KeyError):
                        continue
                if session.scalar(
                    select(Notification.id).where(
                        Notification.account_id == order.account_id,
                        Notification.event_key == "payment:" + order.id,
                    )
                ):
                    notifier.emit_in(
                        session,
                        order.account_id,
                        "access-ready:" + order.id,
                        "subscription_renewed",
                        "Подписка продлена",
                        f"Новый оплаченный срок подписки: {expiry.isoformat()} (UTC). Предыдущее напоминание об окончании больше не актуально."
                        if renewal and expiry
                        else "Подписка продлена. Актуальный срок доступен в кабинете.",
                        target="/#subscriptions",
                        channel="auto",
                    )

    def deliver_backups(self, limit=1):
        self._recover_backup_claims()
        completed = 0
        for _ in range(max(0, min(limit, 10))):
            with self.db.transaction() as session:
                owner = self._owner_in(session, lock=True)
                job = session.scalar(
                    select(Job)
                    .where(Job.kind == "daily_backup", Job.state == "pending")
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
                if not job:
                    break
                if (
                    not self.commerce.local_test
                    or not owner
                    or job.account_id != owner.id
                    or job.payload.get("recipient") != str(owner.telegram_id)
                    or job.payload.get("database") != self.db.engine.url.database
                ):
                    job.state, job.last_error = "blocked", "backup_scope_denied"
                    continue
                job.state, job.attempts = "in_flight", job.attempts + 1
                job.payload = {**job.payload, "claim_expires_at": (now() + timedelta(minutes=15)).isoformat()}
                identifier, recipient = job.id, str(owner.telegram_id)
            state, error = "done", None
            verification = {"state": "unknown"}
            try:
                from .backup_verification import BackupIncomplete

                for copy_attempt in range(1, 4):
                    copy_id = identifier if copy_attempt == 1 else f"{identifier}-attempt-{copy_attempt}"
                    if not self._renew_backup_claim(identifier):
                        raise DomainError("backup_claim_lost", "Задание требует проверки.", 409)
                    try:
                        result = self._dump(copy_id)
                        verification = self._verify(result, copy_id)
                        if (
                            not isinstance(verification, dict)
                            or verification.get("state") != "verified"
                            or verification.get("snapshot_equal") is not True
                            or verification.get("relations_equal") is not True
                        ):
                            raise DomainError("backup_verify_unknown", "Проверка копии не подтверждена.", 503)
                        verification = {**verification, "copy_attempts": copy_attempt}
                        break
                    except BackupIncomplete:
                        verification = {"state": "incomplete", "copy_attempts": copy_attempt}
                        if copy_attempt == 3:
                            raise
                if result.stat().st_size > 49_000_000:
                    raise DomainError("backup_too_large", "Копия превышает лимит Telegram.", 409)
                if not self._renew_backup_claim(identifier, verification=verification, dump_file=result.name):
                    raise DomainError("backup_claim_lost", "Задание требует проверки.", 409)
                self._send(
                    recipient,
                    result,
                    "Резервная копия всей тестовой базы "
                    + configured_instance(self.settings).app_name
                    + ". Храните файл конфиденциально.",
                )
            except DomainError as exc:
                state, error = "review", exc.code
            except Exception:  # noqa: BLE001 -- no blind retry of a possibly delivered confidential database copy
                state, error = "review", "backup_outcome_unknown"
            with self.db.transaction() as session:
                self._owner_in(session, lock=True)
                job = session.scalar(select(Job).where(Job.id == identifier).with_for_update())
                if not job or job.state != "in_flight":
                    continue
                job.state, job.last_error = state, error
                job.payload = {
                    **job.payload,
                    "completed_at": now().isoformat() if state == "done" else None,
                    "verification": verification,
                    "dump_file": result.name if state == "done" else None,
                }
                self._backup_report_in(session, job)
                session.add(
                    Audit(
                        actor_id=job.account_id,
                        action="backup_delivery",
                        target=identifier,
                        reason="Ежедневная копия владельцу",
                        details={"state": state, "database": self.db.engine.url.database},
                    )
                )
            completed += state == "done"
        self.cleanup_backups()
        self._report_finished_backups()
        return completed

    def _renew_backup_claim(self, identifier, *, verification=None, dump_file=None):
        with self.db.transaction() as session:
            owner = self._owner_in(session, lock=True)
            job = session.scalar(select(Job).where(Job.id == identifier).with_for_update())
            if not job or job.state != "in_flight":
                return False
            if (
                not owner
                or job.account_id != owner.id
                or job.payload.get("recipient") != str(owner.telegram_id)
                or job.payload.get("database") != self.db.engine.url.database
                or not self.commerce.local_test
            ):
                return False
            job.payload = {**job.payload, "claim_expires_at": (now() + timedelta(minutes=15)).isoformat()}
            if verification is not None:
                job.payload = {**job.payload, "verification": verification, "dump_file": dump_file}
            return True

    def _report_finished_backups(self, limit=10):
        with self.db.transaction() as session:
            self._owner_in(session, lock=True)
            jobs = session.scalars(
                select(Job)
                .where(
                    Job.kind == "daily_backup",
                    Job.state.in_(["done", "review"]),
                    Job.payload["verification"].as_string().is_not(None),
                    Job.payload["report_state"].as_string().is_distinct_from("queued"),
                )
                .order_by(Job.available_at, Job.id)
                .with_for_update(skip_locked=True)
                .limit(limit)
            )
            for job in jobs:
                self._backup_report_in(session, job)

    def _backup_report_in(self, session, job):
        from types import SimpleNamespace

        from .staff_permissions import StaffPermissions

        permissions = StaffPermissions(
            self.commerce.accounts or SimpleNamespace(db=self.db, settings=self.settings)
        )
        owner = self._owner_in(session)
        if not owner or permissions.role_in(session, owner) != "owner":
            job.payload = {**job.payload, "report_state": "owner_unavailable"}
            return
        verification = job.payload.get("verification", {})
        body = (
            "Восстановление закрытой копии проверено. Таблиц: "
            + str(verification.get("table_count", 0))
            + ".\n"
            if verification.get("state") == "verified"
            else "Резервная копия осталась неполной после трёх попыток. Требуется решение владельца.\n"
            if verification.get("state") == "incomplete"
            else "Результат проверки резервной копии неизвестен. Требуется решение владельца.\n"
        )
        body += (
            "Документ доставлен владельцу."
            if job.state == "done"
            else "Доставка документа не подтверждена. Автоматическая повторная отправка не выполняется."
        )
        NotificationService(self.db, self.settings).emit_in(
            session,
            owner.id,
            "backup-report:" + job.id,
            "backup_report",
            "Проверка резервной копии",
            body,
            target="/admin#operations",
            owner_only=True,
        )
        job.payload = {**job.payload, "report_state": "queued"}

    def _recover_backup_claims(self):
        with self.db.transaction() as session:
            self._owner_in(session, lock=True)
            rows = session.scalars(
                select(Job)
                .where(Job.kind == "daily_backup", Job.state == "in_flight")
                .order_by(Job.available_at)
                .with_for_update(skip_locked=True)
                .limit(100)
            )
            for job in rows:
                try:
                    deadline = datetime.fromisoformat(job.payload["claim_expires_at"])
                    expired = deadline.tzinfo is None or deadline <= now()
                except (KeyError, TypeError, ValueError):
                    expired = True
                if expired:
                    job.state, job.last_error = "review", "backup_outcome_unknown"
                    job.payload = {
                        **job.payload,
                        "verification": job.payload.get("verification", {"state": "unknown"}),
                    }
                    self._backup_report_in(session, job)

    def cleanup_backups(self):
        """Удаляет только известные успешные копии старше семи дней в закрытой папке."""
        url = self.db.engine.url
        if (
            not self.settings.local_test
            or not self.commerce.local_test
            or url.host not in {"localhost", "127.0.0.1"}
            or url.port != 55439
            or url.database not in {"sunrise_test", "sunrise_test_backend", "sunrise_test_bot"}
        ):
            return 0
        root = Path(__file__).resolve().parents[4]
        folder = root / ".private/backups/owner-daily"
        if not folder.is_dir() or folder.resolve() != folder.absolute():
            return 0
        removed = 0
        with self.db.transaction() as session:
            jobs = session.scalars(
                select(Job)
                .where(
                    Job.kind == "daily_backup",
                    Job.state == "done",
                    Job.payload["dump_removed_at"].as_string().is_(None),
                )
                .order_by(Job.available_at)
                .with_for_update(skip_locked=True)
                .limit(100)
            )
            for job in jobs:
                try:
                    completed = datetime.fromisoformat(job.payload["completed_at"])
                    if (
                        completed.tzinfo is None
                        or completed >= now() - timedelta(days=7)
                        or str(UUID(job.id)) != job.id
                    ):
                        continue
                    filename = job.payload.get("dump_file") or (job.id + ".dump")
                    if filename not in {
                        job.id + ".dump",
                        job.id + "-attempt-2.dump",
                        job.id + "-attempt-3.dump",
                    }:
                        continue
                    path = folder / filename
                    if path.is_symlink() or path.resolve().parent != folder.resolve():
                        continue
                    if path.exists():
                        if not path.is_file():
                            continue
                        path.unlink()
                        removed += 1
                    job.payload = {**job.payload, "dump_removed_at": now().isoformat()}
                except (KeyError, ValueError, TypeError, OSError):
                    continue
        return removed

    def _dump_local_database(self, identifier):
        from .backup_verification import BackupVerification

        return BackupVerification(self.db, self.settings).capture(identifier)

    def _verify_local_database(self, path, identifier):
        from .backup_verification import BackupVerification

        return BackupVerification(self.db, self.settings).verify(path, identifier)

    def _send_owner_document(self, recipient, path, caption):
        token = self.settings.bot_test_token
        if (
            recipient != str(self.settings.owner_id)
            or recipient not in {x.strip() for x in self.settings.test_telegram_recipients.split(",")}
            or not token
            or token.split(":", 1)[0] not in {x.strip() for x in self.settings.test_bot_ids.split(",")}
        ):
            raise DomainError("backup_recipient_denied", "Получатель копии не разрешён.", 403)
        with httpx.Client(timeout=60, follow_redirects=False, trust_env=False) as client:
            identity = client.get("https://api.telegram.org/bot" + token + "/getMe").json()
            if not identity.get("ok") or str(identity.get("result", {}).get("id")) not in {
                x.strip() for x in self.settings.test_bot_ids.split(",")
            }:
                raise DomainError("backup_bot_denied", "Тестовый бот не подтверждён.", 403)
            with path.open("rb") as content:
                response = client.post(
                    "https://api.telegram.org/bot" + token + "/sendDocument",
                    data={"chat_id": recipient, "caption": caption},
                    files={"document": (path.name, content, "application/octet-stream")},
                )
            if not response.json().get("ok"):
                raise DomainError("backup_delivery_unknown", "Копия не подтверждена Telegram.", 503)
