"""Выдача подарка, адресные сообщения и копии с серверными правами и аудитом."""

import hashlib
import json
import re
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import select
from sunrise_core.content import validate_content
from sunrise_core.errors import DomainError, missing

from . import panel_entitlements
from .db import AdminAuthenticator, Audit, Job, Product, Segment, Setting, Subscription, now
from .mail import render_mail_text
from .notifications import NotificationService
from .staff_permissions import StaffPermissions


class AdminOperations:
    def __init__(self, commerce, settings):
        self.commerce, self.db, self.settings = commerce, commerce.db, settings
        self.staff = StaffPermissions(commerce.accounts or SimpleNamespace(db=self.db, settings=settings))

    def _guard(self, session, actor, capability, reason, key, confirmed, *, fresh=True):
        account = self.staff.require_in(session, actor, capability, bot=True)
        if (
            confirmed is not True
            or not isinstance(reason, str)
            or not 5 <= len(reason.strip()) <= 500
            or not isinstance(key, str)
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", key)
        ):
            raise DomainError(
                "operation_invalid", "Подтвердите действие, укажите причину и ключ операции.", 400
            )
        if fresh:
            authenticator = session.scalar(
                select(AdminAuthenticator)
                .where(AdminAuthenticator.account_id == account.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if (
                not authenticator
                or not authenticator.trust_verified_at
                or authenticator.trust_verified_at < now() - timedelta(minutes=5)
            ):
                raise DomainError(
                    "mfa_required",
                    "Подтвердите действие свежим кодом аутентификатора.",
                    403,
                    next_action="mfa",
                )
        return account

    def _prior(self, session, actor, key, request):
        marker = "admin_op:" + str(actor) + ":" + hashlib.sha256(key.encode()).hexdigest()[:40]
        row = session.get(Setting, marker)
        if row and row.value.get("request") != request:
            raise DomainError("idempotency_conflict", "Ключ уже использован для другого действия.", 409)
        return marker, row.value.get("result") if row else None

    def grant(
        self,
        actor,
        target,
        product_id,
        days,
        idempotency_key,
        reason,
        confirmed=False,
        subscription_id=None,
        devices=None,
        require_fresh_mfa=True,
    ):
        if (
            isinstance(days, bool)
            or not isinstance(days, int)
            or not 1 <= days <= 3660
            or devices is not None
            and (isinstance(devices, bool) or not isinstance(devices, int) or not 1 <= devices <= 100)
        ):
            raise DomainError("grant_invalid", "Проверьте срок и число устройств.", 400)
        request = {
            "kind": "grant",
            "target": str(target),
            "product_id": product_id,
            "days": days,
            "subscription_id": subscription_id,
            "devices": devices,
        }
        with self.db.transaction() as session:
            self._guard(
                session,
                actor,
                "subscriptions_manage",
                reason,
                idempotency_key,
                confirmed,
                fresh=require_fresh_mfa,
            )
            account = self.commerce._account(session, target, True)
            marker, prior = self._prior(session, actor, idempotency_key, request)
            if prior:
                return prior
            self.commerce._assert_new_operations(session)
            product = session.get(Product, product_id)
            if not product or not product.available:
                raise missing()
            sub = self.commerce._sub(session, account.id, subscription_id, True) if subscription_id else None
            prior_status = panel_entitlements.local_status(session, sub) if sub else "active"
            if sub:
                self.commerce._assert_subscription_operations(session, sub)
                if (
                    sub.product_id != product.id
                    or sub.expires_at is None
                    or devices is not None
                    and devices != sub.devices_limit
                ):
                    raise DomainError(
                        "grant_rights_review",
                        "Подарок не должен менять существующий тариф или лимит устройств.",
                        409,
                    )
                start = max(now(), sub.expires_at)
            else:
                start = now()
                sub = Subscription(
                    account_id=account.id,
                    product_id=product.id,
                    devices_limit=devices or product.included_devices,
                    environment="local_test" if self.commerce.local_test else "sandbox",
                )
                session.add(sub)
                session.flush()
            end = start + timedelta(days=days)
            basis = {
                "currency": product.currency,
                "panel_mappings": {p.id: p.internal_squad_uuid for p in session.scalars(select(Product))},
                "panel_configuration": self.commerce._setting(session, "panel_mapping", {}),
            }
            session.add(
                Segment(
                    subscription_id=sub.id,
                    product_id=product.id,
                    starts_at=start,
                    ends_at=end,
                    devices=sub.devices_limit,
                    paid_value=Decimal("0.00"),
                    price_basis=basis,
                    kind="owner_gift",
                )
            )
            sub.expires_at, sub.status, sub.version = end, "provisioning_pending", sub.version + 1
            session.flush()
            session.add(
                Job(
                    key="owner-grant:" + marker,
                    kind="provision",
                    account_id=account.id,
                    payload=self.commerce._provision_payload(
                        session, sub, prior_status=prior_status, origin="owner"
                    ),
                )
            )
            result = {
                "subscription_id": sub.id,
                "expires_at": end.isoformat(),
                "provisioning_state": "pending",
            }
            session.add(Setting(key=marker, value={"request": request, "result": result}))
            session.add(
                Audit(
                    actor_id=int(actor),
                    action="owner_grant",
                    target=sub.id,
                    reason=reason.strip(),
                    details={
                        "days": days,
                        "paid_value": "0.00",
                        "idempotency_key_digest": hashlib.sha256(idempotency_key.encode()).hexdigest(),
                    },
                )
            )
            NotificationService(self.db, self.settings).emit_in(
                session,
                account.id,
                "grant:" + marker,
                "gift",
                "Вам добавлены дни подписки",
                f"Добавлено {days} дн. Готовим доступ.",
                target="/#subscriptions",
            )
            return result

    def message(self, actor, target, nodes, idempotency_key, reason, confirmed=False):
        try:
            validate_content(nodes)
        except ValueError:
            raise DomainError("content_invalid", "Проверьте текст сообщения.", 400) from None
        body = render_mail_text(nodes)
        from .notifications import validate_telegram_content

        validate_telegram_content("Сообщение поддержки", body, nodes)
        request = {
            "kind": "message",
            "target": str(target),
            "content_digest": hashlib.sha256(json.dumps(nodes, sort_keys=True).encode()).hexdigest(),
        }
        with self.db.transaction() as session:
            self._guard(session, actor, "messages_write", reason, idempotency_key, confirmed, fresh=False)
            account = self.commerce._account(session, target, True)
            marker, prior = self._prior(session, actor, idempotency_key, request)
            if prior:
                return prior
            item = NotificationService(self.db, self.settings).emit_in(
                session,
                account.id,
                "message:" + marker,
                "support",
                "Сообщение поддержки",
                body,
                nodes=nodes,
                target="/#notifications",
            )
            result = {"notification_id": item.id, "state": "queued"}
            session.add(Setting(key=marker, value={"request": request, "result": result}))
            session.add(
                Audit(
                    actor_id=int(actor),
                    action="message_create",
                    target=str(account.id),
                    reason=reason.strip(),
                    details={"notification_id": item.id},
                )
            )
            return result

    def backups(self, actor):
        with self.db.transaction() as session:
            self.staff.require_in(session, actor, "backups_manage", bot=True)
            production = getattr(self.settings, "runtime_mode", "isolated") == "production"
            verification = None
            if production:
                from .settings import configured_runtime

                configured_runtime(self.settings, self.db).require_current(session)
                verified = session.get(Setting, "production_backup_last_verified")
                verification = verified.value if verified else {"state": "not_verified"}
            rows = session.scalars(
                select(Job).where(Job.kind == "daily_backup").order_by(Job.id.desc()).limit(30)
            )
            return {
                "items": [
                    {
                        "id": row.id,
                        "state": row.state,
                        "created_at": row.available_at.isoformat(),
                        "completed_at": row.payload.get("completed_at"),
                        "last_error": row.last_error,
                    }
                    for row in rows
                ],
                "schedule": "16:00",
                "timezone": "Europe/Moscow",
                "recipient": "owner",
                "scope": "production_server_sql" if production else "whole_test_database",
                **({"last_verification": verification} if production else {}),
            }

    def request_backup(self, actor, key, reason, confirmed=False, *, require_fresh_mfa=True):
        from .scheduler import Scheduler

        with self.db.transaction() as session:
            self._guard(
                session,
                actor,
                "backups_manage",
                reason,
                key,
                confirmed,
                fresh=require_fresh_mfa,
            )
            production = getattr(self.settings, "runtime_mode", "isolated") == "production"
            if production:
                from .panel_news import owner_account_in
                from .settings import configured_runtime

                policy = configured_runtime(self.settings, self.db)
                policy.require_current(session)
                owner = owner_account_in(session, self.settings)
            else:
                owner = Scheduler(self.commerce, self.settings)._owner_in(session)
            if not owner:
                raise DomainError("backup_recipient_denied", "Получатель копии не настроен.", 403)
            marker, prior = self._prior(session, actor, key, {"kind": "backup"})
            if prior:
                return prior
            job = Job(
                key=marker,
                kind="daily_backup",
                account_id=owner.id,
                payload=(
                    {
                        "backup_scope": "production_server_sql",
                        "source": "owner_manual",
                        "scope_id": policy.scope.scope_id,
                        "database": self.db.engine.url.database,
                    }
                    if production
                    else {"recipient": str(owner.telegram_id), "database": self.db.engine.url.database}
                ),
            )
            session.add(job)
            session.flush()
            result = {"id": job.id, "state": "pending"}
            session.add(Setting(key=marker, value={"request": {"kind": "backup"}, "result": result}))
            session.add(
                Audit(
                    actor_id=int(actor),
                    action="backup_requested",
                    target=job.id,
                    reason=reason.strip(),
                    details={},
                )
            )
            return result
