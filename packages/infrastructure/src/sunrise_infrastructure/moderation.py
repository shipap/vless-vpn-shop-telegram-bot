"""Учет завершенных отвязок и ограничения только этой операции."""

import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sunrise_core.errors import DomainError, missing

from .db import Account, Audit, Device, DeviceUnlink, Job, Subscription, now


def device_unlink_event_in(session, event, policy=None):
    account = session.get(Account, event.account_id) if event else None
    subscription = session.get(Subscription, event.subscription_id) if event else None
    if not event or not account or not subscription or subscription.account_id != account.id:
        raise missing()
    if policy:
        policy.require_current(session)
        if getattr(policy, "scoped_payments", False):
            policy.require_subscription_in(session, subscription)
            if not policy.owns_original_in(session, account, str(event.original_account_id)):
                raise missing()
    return account, subscription


class DeviceModeration:
    def __init__(self, commerce):
        self.commerce, self.db = commerce, commerce.db

    def state(self, session, account, moment=None):
        moment = moment or now()
        zone = ZoneInfo("Europe/Moscow")
        local = moment.astimezone(zone)
        start = datetime(local.year, local.month, 1, tzinfo=zone).astimezone(UTC)
        end = datetime(local.year + (local.month == 12), local.month % 12 + 1, 1, tzinfo=zone).astimezone(UTC)
        base = select(func.count()).select_from(DeviceUnlink).where(DeviceUnlink.account_id == account.id)
        operations = (
            select(
                DeviceUnlink.unlink_mode,
                DeviceUnlink.unlink_operation,
                func.min(DeviceUnlink.completed_at).label("completed_at"),
            )
            .where(
                DeviceUnlink.account_id == account.id,
                DeviceUnlink.unlink_mode.in_(["single", "bulk"]),
                DeviceUnlink.unlink_operation.is_not(None),
            )
            .group_by(DeviceUnlink.unlink_mode, DeviceUnlink.unlink_operation)
            .subquery()
        )
        result = {
            "current_warnings": account.unlink_warnings,
            "total_warnings": account.unlink_warnings_total,
            "unlink_blocked": account.unlink_blocked,
            "unlink_total": session.scalar(base),
            "unlink_month": session.scalar(
                base.where(DeviceUnlink.completed_at >= start, DeviceUnlink.completed_at < end)
            ),
            "month": local.strftime("%Y-%m"),
            "timezone": "Europe/Moscow",
        }
        for mode, label in (("single", "individual"), ("bulk", "bulk")):
            count = select(func.count()).select_from(operations).where(operations.c.unlink_mode == mode)
            result[f"unlink_{label}_total"] = session.scalar(count)
            result[f"unlink_{label}_month"] = session.scalar(
                count.where(operations.c.completed_at >= start, operations.c.completed_at < end)
            )
        unknown = base.where(or_(DeviceUnlink.unlink_mode.is_(None), DeviceUnlink.unlink_operation.is_(None)))
        result["unlink_unknown_total"] = session.scalar(unknown)
        result["unlink_unknown_month"] = session.scalar(
            unknown.where(DeviceUnlink.completed_at >= start, DeviceUnlink.completed_at < end)
        )
        return result

    def assert_unlink_allowed(self, account):
        if account.unlink_blocked:
            raise DomainError(
                "device_unlink_blocked",
                "Отвязка устройств ограничена владельцем. Обратитесь в поддержку.",
                403,
            )

    def complete(
        self,
        session,
        account_id,
        subscription_id,
        device_id,
        moment=None,
        *,
        operation_mode=None,
        operation_key=None,
    ):
        account = (
            self.commerce._account(session, account_id, True, customer=False)
            if getattr(self.commerce, "live", False) or getattr(self.commerce, "scoped_payments", False)
            else self.commerce._account(session, account_id, True)
        )
        device = session.scalar(
            select(Device)
            .where(Device.id == device_id, Device.subscription_id == subscription_id)
            .with_for_update()
        )
        if not device:
            raise missing()
        # This is called only after confirmed panel success, never for a requested/pending operation.
        if device.revoked or session.scalar(
            select(DeviceUnlink.id).where(
                DeviceUnlink.device_id == device_id, DeviceUnlink.generation == device.generation
            )
        ):
            return False
        operation = session.scalar(
            select(Job).where(
                Job.kind == "device_revoke",
                Job.account_id == account.id,
                Job.payload["subscription_id"].as_string() == subscription_id,
                Job.payload["device_id"].as_string() == device.id,
                Job.payload["generation"].as_integer() == device.generation,
            )
        )
        if operation:
            operation_mode = operation.payload.get("unlink_mode")
            operation_key = operation.payload.get("unlink_operation")
        if not isinstance(operation_key, str) or not 1 <= len(operation_key) <= 200:
            operation_key = None
        if operation_mode not in {"single", "bulk"} or operation_key is None:
            operation_mode = None
        event = DeviceUnlink(
            account_id=account.id,
            original_account_id=account.id,
            subscription_id=subscription_id,
            device_id=device.id,
            generation=device.generation,
            completed_at=moment or now(),
            unlink_mode=operation_mode,
            unlink_operation=operation_key,
            device_name=(operation.payload.get("device_name") if operation else None) or device.name,
        )
        device.revoked = True
        session.add(event)
        session.flush()
        if getattr(self.commerce, "live", False) or getattr(self.commerce, "scoped_payments", False):
            from .notifications import NotificationService
            from .panel import PanelUnavailable
            from .panel_news import owner_account_in

            self.commerce.runtime_policy.require_current(session)
            if getattr(self.commerce, "scoped_payments", False):
                device_unlink_event_in(session, event, self.commerce.runtime_policy)
            owner = owner_account_in(session, self.commerce.public_settings)
            if owner is None:
                raise PanelUnavailable("owner_notice_unavailable")
            state = self.state(session, account, event.completed_at)
            from .sale_notifications import staff_account_identity

            identity = staff_account_identity(account)
            body = (
                f"Пользователь: {identity}\n"
                f"Удалено устройство: {event.device_name or 'название неизвестно'}\n"
                f"Всего удалено поштучно {state['unlink_individual_total']}\n"
                f"Массовых удалений {state['unlink_bulk_total']}\n"
                f"Поштучно за месяц {state['unlink_individual_month']}\n"
                f"Массовых за месяц {state['unlink_bulk_month']}\n"
                f"Текущих предупреждений {state['current_warnings']}/2"
            )
            if state["unlink_unknown_total"]:
                body += (
                    f"\nУдалений с неизвестным типом {state['unlink_unknown_total']}"
                    f"\nС неизвестным типом за месяц {state['unlink_unknown_month']}"
                )
            NotificationService(self.db, self.commerce.public_settings).emit_in(
                session,
                owner.id,
                "owner-device-unlink:" + event.id,
                "device_unlink",
                "Удаление HWID",
                body,
                target="/admin#users",
                owner_only=True,
                allow_push=False,
                actions=[
                    {
                        "label": label,
                        "target": "/admin#users",
                        "operation": action,
                        "metadata": {"account_id": str(account.id), "unlink_event_id": event.id},
                    }
                    for label, action in (
                        ("Профиль пользователя", "device_unlink_profile"),
                        ("Предупредить", "device_unlink_warn"),
                        ("Блокировать отвязки", "device_unlink_block"),
                    )
                ],
            )
            return True
        owner = self.commerce.owner_id
        session.add(
            Job(
                key="owner-device-unlink:" + event.id,
                kind="owner_device_unlink",
                account_id=account.id,
                payload={
                    "target_id": owner,
                    "account_id": str(account.id),
                    "telegram_id": str(account.telegram_id) if account.telegram_id else None,
                    "telegram_username": account.telegram_username,
                    "email": account.email,
                    "subscription_id": subscription_id,
                    "device_id": device.id,
                    "device_name": event.device_name,
                    "hwid": device.panel_hwid,
                    "generation": device.generation,
                    "unlink_mode": event.unlink_mode,
                    "unlink_operation": event.unlink_operation,
                    "unlink_event_id": event.id,
                    "completed_at": event.completed_at.isoformat(),
                    **self.state(session, account, event.completed_at),
                    "actions": ["warn", "block"],
                },
            )
        )
        return True

    def authorize_owner(self, session, actor, settings):
        from types import SimpleNamespace

        from .staff_permissions import StaffPermissions

        return StaffPermissions(
            self.commerce.accounts or SimpleNamespace(db=self.db, settings=settings)
        ).require_in(session, actor, "device_unlinks_manage", bot=True)

    def authorize_notification_owner(self, session, actor, settings):
        # Internal dispatcher only: recipient authorization is independent of
        # customer/admin sign-in. No HTTP/admin read or mutation uses this gate.
        account = self.commerce._account(session, actor, True)
        configured = getattr(settings, "owner_id", None)
        if not configured or str(account.telegram_id) != str(configured):
            raise missing()
        return account

    @staticmethod
    def can_moderate(account, settings):
        from .staff_permissions import telegram_admin_allowed

        return telegram_admin_allowed(account, settings)

    def moderate(
        self, actor, target_id, action, reason, idempotency_key, settings, confirmed=False, *, require_fresh_mfa=True
    ):
        if confirmed is not True:
            raise DomainError("confirmation_required", "Подтвердите изменение.", 400)
        if not isinstance(reason, str) or not 5 <= len(reason.strip()) <= 500:
            raise DomainError("reason_required", "Укажите причину изменения.", 400)
        if (
            action not in {"warn", "block", "unblock", "remove_warning"}
            or not isinstance(idempotency_key, str)
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", idempotency_key)
        ):
            raise DomainError("moderation_invalid", "Проверьте действие и ключ операции.", 400)
        with self.db.transaction() as session:
            policy = getattr(self.commerce, "runtime_policy", None)
            if policy:
                policy.require_current(session)
            if hasattr(self.commerce, "_lock_accounts_in"):
                self.commerce._lock_accounts_in(session, [actor, target_id])
            authorized = self.authorize_owner(session, actor, settings)
            if require_fresh_mfa:
                from datetime import timedelta

                from .db import AdminAuthenticator

                authenticator = session.scalar(
                    select(AdminAuthenticator)
                    .where(AdminAuthenticator.account_id == authorized.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if (
                    not authenticator
                    or not authenticator.trust_verified_at
                    or not now() - timedelta(minutes=5) <= authenticator.trust_verified_at <= now()
                ):
                    raise DomainError(
                        "mfa_required",
                        "Подтвердите действие свежим кодом аутентификатора.",
                        403,
                        next_action="mfa",
                    )
            account = self.commerce._account(session, target_id, True)
            key = "device-moderation:" + str(actor) + ":" + idempotency_key
            # Target account lock serializes independent warn callbacks; the unique job key scopes replays.
            from sqlalchemy import text

            session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key})
            prior = session.scalar(select(Job).where(Job.key == key))
            if prior:
                if prior.account_id != account.id or prior.payload["action"] != action:
                    raise DomainError(
                        "idempotency_conflict", "Ключ уже использован для другого действия.", 409
                    )
                return prior.payload["result"]
            before = self.state(session, account)
            if action == "warn":
                account.unlink_warnings += 1
                account.unlink_warnings_total += 1
                if account.unlink_warnings >= 2:
                    account.unlink_blocked, account.unlink_warnings = True, 0
            elif action == "remove_warning":
                if account.unlink_warnings <= 0:
                    raise DomainError("warning_missing", "Действующих предупреждений нет.", 409)
                account.unlink_warnings -= 1
            else:
                account.unlink_blocked = action == "block"
                if action == "unblock":
                    for job in session.scalars(
                        select(Job).where(
                            Job.account_id == account.id,
                            Job.kind == "device_revoke",
                            Job.state == "blocked",
                            Job.last_error == "device_unlink_blocked",
                        )
                    ):
                        job.state, job.last_error, job.available_at = "pending", None, now()
            after = self.state(session, account)
            if policy:
                policy.require_current(session)
            session.add(
                Job(
                    key=key,
                    kind="device_moderation",
                    account_id=account.id,
                    state="done",
                    payload={"action": action, "result": after},
                )
            )
            session.add(
                Audit(
                    actor_id=int(actor),
                    action="device_moderation",
                    target=str(account.id),
                    reason=reason,
                    details={
                        "action": action,
                        "before": before,
                        "after": after,
                        "scope": "device_unlink_only",
                    },
                )
            )
            return after

    def summary(self, session, account):
        inviter = None
        if account.referrer_id:
            try:
                a = self.commerce._account(session, account.referrer_id)
                inviter = {
                    "id": str(a.id),
                    "email": a.email,
                    "telegram_id": str(a.telegram_id) if a.telegram_id else None,
                    "telegram_username": a.telegram_username,
                }
            except DomainError:
                pass
        from .db import Agreement, Subscription
        from .referrals import earned_total
        from .subscription_archive import unarchived_subscription_clause

        earnings = earned_total(session, account.id)
        return {
            "inviter": inviter,
            "referral": {
                "balance": str(account.bonus_balance),
                "earned_total": str(earnings),
                "invited_total": session.scalar(
                    select(func.count()).select_from(Account).where(Account.referrer_id == account.id)
                ),
            },
            "active_subscriptions_total": session.scalar(
                select(func.count())
                .select_from(Subscription)
                .where(
                    Subscription.account_id == account.id,
                    Subscription.status == "active",
                    Subscription.expires_at > now(),
                    unarchived_subscription_clause(),
                )
            ),
            "active_agreements_total": session.scalar(
                select(func.count())
                .select_from(Agreement)
                .where(Agreement.account_id == account.id, Agreement.state.in_(["active", "enabled"]))
            ),
            "moderation": self.state(session, account),
        }
