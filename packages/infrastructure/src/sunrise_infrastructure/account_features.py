"""Подтверждённые изменения личности без переноса денег или оплаченных прав."""

import hashlib
import hmac
import re
import secrets
from datetime import timedelta

from sqlalchemy import delete, func, select, update
from sunrise_core.errors import DomainError, missing

from .commerce import account_dto, iso
from .db import (
    Account,
    AccountIdentityOperation,
    AdminAuthenticator,
    Audit,
    Challenge,
    MergeProof,
    Session,
    now,
    uid,
)
from .security import digest


class AccountsFeatures:
    def __init__(self, accounts):
        self.accounts, self.db, self.settings = accounts, accounts.db, accounts.settings

    def _identity(self, tx, account_id, session_id):
        account = tx.scalar(
            select(Account)
            .where(Account.id == int(account_id))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        session = tx.scalar(
            select(Session)
            .where(Session.id == session_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if not account or not session or session.account_id != account.id or session.expires_at <= now():
            raise DomainError("auth_required", "Войдите в аккаунт.", 401)
        return account, session

    def _fresh_email(self, account, session, ip):
        return bool(
            account.email
            and account.email_verified
            and (
                session.email_confirmed_at
                and session.email_confirmed_at >= now() - timedelta(minutes=5)
                and session.ip_address == ip
                or self.accounts._link_granted(account.id, session.id, ip)
            )
        )

    def _require_email(self, account, session, ip):
        if not self._fresh_email(account, session, ip):
            raise DomainError("fresh_login_required", "Подтвердите текущую почту кодом из письма.", 403)

    def _public_session(self, row):
        return (
            row.public_id
            or hmac.new(
                self.settings.secret_key.encode(), ("session-public:" + row.id).encode(), hashlib.sha256
            ).hexdigest()
        )

    def list_sessions(self, account_id, current_session_id):
        with self.db.transaction() as tx:
            account, _ = self._identity(tx, account_id, current_session_id)
            return {
                "items": [
                    {
                        "id": self._public_session(row),
                        "current": row.id == current_session_id,
                        "auth_method": row.auth_method or "unknown",
                        "created_at": iso(row.created_at),
                        "last_seen_at": iso(row.last_seen_at),
                        "expires_at": iso(row.expires_at),
                        "ip_address": row.ip_address,
                        "browser_label": row.browser_label,
                    }
                    for row in tx.scalars(
                        select(Session)
                        .where(Session.account_id == account.id, Session.expires_at > now())
                        .order_by(Session.expires_at.desc(), Session.id)
                    )
                ]
            }

    def revoke_session(self, account_id, current_session_id, public_id):
        with self.db.transaction() as tx:
            account, _ = self._identity(tx, account_id, current_session_id)
            target = next(
                (
                    row
                    for row in tx.scalars(
                        select(Session).where(Session.account_id == account.id).with_for_update()
                    )
                    if hmac.compare_digest(self._public_session(row), public_id)
                ),
                None,
            )
            if not target:
                raise missing()
            current = target.id == current_session_id
            tx.delete(target)
            self._audit(tx, account.id, "session_revoke", public_id)
            return {"revoked": True, "current_session_revoked": current}

    def revoke_other_sessions(self, account_id, current_session_id):
        with self.db.transaction() as tx:
            account, _ = self._identity(tx, account_id, current_session_id)
            count = tx.execute(
                delete(Session).where(Session.account_id == account.id, Session.id != current_session_id)
            ).rowcount
            self._audit(tx, account.id, "sessions_revoke_others", str(account.id))
            return {"revoked": count}

    def _operation(self, tx, account, session, kind, ip, *, new_email=None, state="pending"):
        operation = AccountIdentityOperation(
            id=uid(),
            account_id=account.id,
            session_id=session.id,
            kind=kind,
            state=state,
            old_email=account.email,
            old_telegram_id=account.telegram_id,
            new_email=new_email,
            expires_at=now() + timedelta(minutes=10),
            payload={"ip_digest": digest(ip)},
        )
        tx.add(operation)
        return operation

    def _snapshot(self, account, operation):
        if account.email != operation.old_email or account.telegram_id != operation.old_telegram_id:
            raise DomainError(
                "identity_changed", "Способы входа изменились. Начните подтверждение заново.", 409
            )

    def _dto(self, operation, **extra):
        return {
            "operation_id": operation.id,
            "stage": operation.state,
            "expires_at": iso(operation.expires_at),
            **extra,
        }

    def identity_status(self, account_id, session_id):
        with self.db.transaction() as tx:
            account, _ = self._identity(tx, account_id, session_id)
            operations = tx.scalars(
                select(AccountIdentityOperation).where(
                    AccountIdentityOperation.account_id == account.id,
                    AccountIdentityOperation.session_id == session_id,
                    AccountIdentityOperation.expires_at > now(),
                    AccountIdentityOperation.state.not_in(["completed", "cancelled"]),
                )
            )
            return {"account": account_dto(account), "operations": [self._dto(row) for row in operations]}

    def request_unlink(self, account_id, session_id, ip):
        with self.db.transaction() as tx:
            account, session = self._identity(tx, account_id, session_id)
            self._require_email(account, session, ip)
            if not account.telegram_id:
                raise DomainError("telegram_not_linked", "Telegram не привязан.", 409)
            operation = self._operation(
                tx, account, session, "telegram_unlink", ip, state="awaiting_telegram"
            )
            operation.expires_at = now() + timedelta(minutes=5)
            secret = secrets.token_urlsafe(32)
            operation.secret_hash = digest(secret)
            tx.flush()
            self._audit(tx, account.id, "telegram_unlink_requested", operation.id)
            return self._dto(operation, proof=operation.id + "." + secret)

    def _proof(self, tx, proof, *, lock=False):
        if not isinstance(proof, str) or len(proof) > 200:
            raise DomainError("identity_proof_invalid", "Подтверждение недействительно.", 400)
        try:
            oid, secret = proof.split(".", 1)
        except ValueError:
            raise DomainError("identity_proof_invalid", "Подтверждение недействительно.", 400) from None
        query = select(AccountIdentityOperation).where(AccountIdentityOperation.id == oid)
        if lock:
            query = query.with_for_update().execution_options(populate_existing=True)
        operation = tx.scalar(query)
        if (
            not operation
            or not operation.secret_hash
            or not hmac.compare_digest(operation.secret_hash, digest(secret))
        ):
            raise DomainError("identity_proof_invalid", "Подтверждение недействительно.", 400)
        return operation

    def confirm_unlink(self, telegram_id, proof):
        if type(telegram_id) is not int or not 0 < telegram_id < 2**63:
            raise DomainError("identity_proof_invalid", "Подтверждение недействительно.", 400)
        with self.db.transaction() as tx:
            operation = self._proof(tx, proof)
            if operation.kind != "telegram_unlink" or telegram_id != operation.old_telegram_id:
                raise DomainError("identity_proof_invalid", "Подтверждение недействительно.", 400)
            if operation.state == "completed":
                return {"operation_id": operation.id, "stage": "completed"}
            if operation.state != "awaiting_telegram" or operation.expires_at <= now():
                raise DomainError("identity_proof_invalid", "Подтверждение истекло или отменено.", 400)
            account, session = self._identity(tx, operation.account_id, operation.session_id)
            operation = self._proof(tx, proof, lock=True)
            if operation.state == "completed":
                return {"operation_id": operation.id, "stage": "completed"}
            if operation.state != "awaiting_telegram" or operation.expires_at <= now():
                raise DomainError("identity_proof_invalid", "Подтверждение истекло или отменено.", 400)
            self._snapshot(account, operation)
            self._unlink_in(tx, account, session, operation)
            return self._dto(operation, account=account_dto(account))

    def request_owner_unlink(self, account_id, session_id, ip, reason):
        if not isinstance(reason, str) or not 5 <= len(reason.strip()) <= 500:
            raise DomainError("reason_required", "Опишите причину обращения владельцу.", 400)
        with self.db.transaction() as tx:
            account, session = self._identity(tx, account_id, session_id)
            self._require_email(account, session, ip)
            if not account.telegram_id:
                raise DomainError("telegram_not_linked", "Telegram не привязан.", 409)
            operation = self._operation(
                tx, account, session, "owner_unlink_request", ip, state="owner_review"
            )
            operation.expires_at = now() + timedelta(hours=24)
            operation.payload = {**operation.payload, "reason": reason.strip()}
            owner = tx.scalar(
                select(Account).where(Account.telegram_id == int(self.settings.owner_id or "0"))
            )
            if not owner and self.settings.owner_email:
                owner = tx.scalar(
                    select(Account).where(
                        Account.email == self.settings.owner_email, Account.email_verified.is_(True)
                    )
                )
            if not owner:
                raise DomainError("owner_unavailable", "Обращение владельцу временно недоступно.", 503)
            tx.flush()
            self._notify(
                tx,
                owner.id,
                "identity-review:" + operation.id,
                "identity_review",
                "Запрос отвязки Telegram",
                "Пользователь запросил проверку отвязки Telegram.",
                target="/admin#identity-requests",
                owner_only=True,
            )
            self._audit(tx, account.id, "owner_unlink_requested", operation.id)
            return self._dto(operation)

    def approve_owner_unlink(self, actor_id, operation_id, reason):
        if not isinstance(reason, str) or not 5 <= len(reason.strip()) <= 500:
            raise DomainError("reason_required", "Укажите причину решения.", 400)
        with self.db.transaction() as tx:
            from .staff_permissions import StaffPermissions

            StaffPermissions(self.accounts).require_in(tx, actor_id, "identity_review")
            operation = tx.get(AccountIdentityOperation, operation_id)
            if not operation or operation.kind != "owner_unlink_request":
                raise missing()
            if operation.state == "completed":
                return {"operation_id": operation.id, "stage": "completed"}
            if operation.state != "owner_review" or operation.expires_at <= now():
                raise DomainError("identity_proof_invalid", "Обращение истекло или отменено.", 409)
            account, session = self._identity(tx, operation.account_id, operation.session_id)
            operation = tx.scalar(
                select(AccountIdentityOperation)
                .where(AccountIdentityOperation.id == operation_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if operation.state == "completed":
                return {"operation_id": operation.id, "stage": "completed"}
            if operation.state != "owner_review" or operation.expires_at <= now():
                raise DomainError("identity_proof_invalid", "Обращение истекло или отменено.", 409)
            self._snapshot(account, operation)
            self._unlink_in(tx, account, session, operation)
            self._audit(tx, int(actor_id), "owner_unlink_approved", operation.id, reason.strip())
            return self._dto(operation)

    def _audit(self, tx, actor_id, action, target, reason="Подтверждённое действие аккаунта"):
        tx.add(Audit(actor_id=actor_id, action=action, target=target, reason=reason, details={}))

    def _notify(self, tx, account_id, key, kind, title, body, **kwargs):
        from .notifications import NotificationService

        NotificationService(self.db, self.settings).emit_in(tx, account_id, key, kind, title, body, **kwargs)

    def _invalidate_in(self, tx, account, session, operation):
        tx.execute(delete(Session).where(Session.account_id == account.id, Session.id != session.id))
        session.mfa_at = None
        tx.execute(
            update(AdminAuthenticator)
            .where(AdminAuthenticator.account_id == account.id)
            .values(
                trusted_ip_digest=None,
                trust_verified_at=None,
                pending_ciphertext=None,
                pending_session_id=None,
                pending_expires_at=None,
            )
        )
        tx.execute(
            delete(MergeProof).where(
                (MergeProof.source_id == account.id) | (MergeProof.target_id == account.id)
            )
        )
        for other in tx.scalars(
            select(AccountIdentityOperation).where(
                AccountIdentityOperation.account_id == account.id,
                AccountIdentityOperation.id != operation.id,
                AccountIdentityOperation.state.not_in(["completed", "cancelled"]),
            )
        ):
            other.state = "cancelled"
            other.completed_at = now()
            challenge_id = other.payload.get("challenge_id")
            if challenge_id:
                challenge = tx.get(Challenge, challenge_id)
                if challenge:
                    challenge.used = True
        # Обычные коды старой личности и scoped-подтверждения не переживают её изменение.
        if operation.old_email:
            tx.execute(
                update(Challenge)
                .where(Challenge.email == operation.old_email)
                .values(used=True, expires_at=now())
            )
        operation.state, operation.completed_at = "completed", now()

    def _unlink_in(self, tx, account, session, operation):
        if not account.email or not account.email_verified:
            raise DomainError("email_required", "Сначала добавьте и подтвердите email.", 409)
        from .staff_permissions import StaffPermissions

        StaffPermissions(self.accounts).ensure_owner_in(tx, account)
        account.telegram_id = account.telegram_username = None
        from .panel_contacts import enqueue_contacts_in
        from .settings import runtime_policy

        enqueue_contacts_in(tx, account, runtime_policy(self.settings), "telegram-unlinked:" + operation.id)
        self._invalidate_in(tx, account, session, operation)
        self._audit(tx, account.id, "telegram_unlinked", operation.id)
        self._notify(
            tx,
            account.id,
            "telegram-unlinked:" + operation.id,
            "identity_changed",
            "Telegram отвязан",
            "Telegram больше не используется для входа в ваш аккаунт.",
            target="/#account",
        )

    def _email(self, value):
        if (
            not isinstance(value, str)
            or len(value) > 320
            or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value.strip())
        ):
            raise DomainError("email_invalid", "Проверьте адрес email.", 400)
        return value.strip().lower()

    def _email_available(self, tx, account, email):
        tx.execute(select(func.pg_advisory_xact_lock(func.hashtext(email))))
        if tx.scalar(select(Account.id).where(func.lower(Account.email) == email, Account.id != account.id)):
            raise DomainError(
                "email_identity_conflict",
                "Адрес недоступен для изменения. Используйте подтверждённую привязку аккаунтов.",
                409,
            )

    def _challenge_in(self, tx, operation, email, ip):
        context = digest(ip + ":" + email)
        self.accounts.guard.check(context)
        self.accounts.guard.check("ip:" + digest(ip))
        self.accounts.guard.resend(digest(email))
        cid, code = uid(), f"{secrets.randbelow(1000000):06d}"
        binding = "identity:" + digest(
            operation.id + ":" + operation.kind + ":" + operation.session_id + ":" + email + ":" + ip
        )
        tx.add(
            Challenge(
                id=cid,
                email=email,
                digest=self.accounts._code_digest(cid, code),
                context=binding,
                expires_at=now() + timedelta(minutes=10),
            )
        )
        operation.payload = {**operation.payload, "challenge_id": cid}
        self.accounts._record_auth_mail_in(
            tx, cid, email, "identity", binding, context, ip, operation=operation
        )
        return cid, code, email

    def request_email_change(self, account_id, session_id, email, ip):
        email = self._email(email)
        with self.db.transaction() as tx:
            account, session = self._identity(tx, account_id, session_id)
            if email == account.email:
                raise DomainError("email_unchanged", "Этот email уже указан в аккаунте.", 409)
            self._email_available(tx, account, email)
            if not account.email:
                if (
                    not account.telegram_id
                    or session.auth_method not in {"telegram_oidc", "telegram_miniapp"}
                    or session.confirmed_at < now() - timedelta(minutes=5)
                    or session.ip_address != ip
                ):
                    raise DomainError(
                        "fresh_login_required", "Подтвердите свежий вход через связанный Telegram.", 403
                    )
                kind, stage = "email_add", "new_email"
            else:
                if not account.email_verified:
                    raise DomainError("email_required", "Текущая почта не подтверждена.", 403)
                kind, stage = (
                    "email_change",
                    "new_email" if self._fresh_email(account, session, ip) else "current_email",
                )
            operation = self._operation(tx, account, session, kind, ip, new_email=email, state=stage)
            delivery = self._challenge_in(tx, operation, email if stage == "new_email" else account.email, ip)
            tx.flush()
            result = self._dto(operation, challenge_id=delivery[0], retry_after=120)
        self.accounts._deliver_code(*delivery[:2], delivery[2], "identity")
        return result

    def verify_email_change(self, account_id, session_id, operation_id, challenge_id, code, ip):
        error, delivery, result = None, None, None
        with self.db.transaction() as tx:
            account, session = self._identity(tx, account_id, session_id)
            operation = tx.scalar(
                select(AccountIdentityOperation)
                .where(AccountIdentityOperation.id == operation_id)
                .with_for_update()
            )
            if (
                not operation
                or operation.account_id != account.id
                or operation.session_id != session.id
                or operation.kind not in {"email_add", "email_change"}
            ):
                raise DomainError("identity_proof_invalid", "Подтверждение недействительно.", 400)
            self._snapshot(account, operation)
            if (
                operation.expires_at <= now()
                or operation.state not in {"current_email", "new_email"}
                or operation.payload.get("ip_digest") != digest(ip)
                or operation.payload.get("challenge_id") != challenge_id
            ):
                raise DomainError("identity_proof_invalid", "Подтверждение истекло или изменилось.", 400)
            email = operation.old_email if operation.state == "current_email" else operation.new_email
            context = digest(ip + ":" + email)
            self.accounts.guard.check(context)
            self.accounts.guard.check("ip:" + digest(ip))
            challenge = tx.scalar(select(Challenge).where(Challenge.id == challenge_id).with_for_update())
            binding = "identity:" + digest(
                operation.id + ":" + operation.kind + ":" + operation.session_id + ":" + email + ":" + ip
            )
            if (
                not challenge
                or challenge.used
                or challenge.expires_at <= now()
                or challenge.context != binding
                or challenge.email != email
            ):
                raise DomainError("code_invalid", "Код неверен или истёк.", 400)
            if not isinstance(code, str) or not hmac.compare_digest(
                challenge.digest, self.accounts._code_digest(challenge.id, code)
            ):
                error = self.accounts._wrong_code(tx, challenge, context, ip)
            else:
                challenge.used = True
                if operation.state == "current_email":
                    operation.state = "new_email"
                    delivery = self._challenge_in(tx, operation, operation.new_email, ip)
                    result = self._dto(operation, challenge_id=delivery[0], retry_after=120)
                else:
                    self._email_available(tx, account, operation.new_email)
                    account.email, account.email_verified = operation.new_email, True
                    from .panel_contacts import enqueue_contacts_in
                    from .settings import runtime_policy

                    enqueue_contacts_in(
                        tx, account, runtime_policy(self.settings), "email-changed:" + operation.id
                    )
                    session.email_confirmed_at = None
                    self._invalidate_in(tx, account, session, operation)
                    self._audit(tx, account.id, "email_changed", operation.id)
                    self._notify(
                        tx,
                        account.id,
                        "email-changed:" + operation.id,
                        "identity_changed",
                        "Email изменён",
                        "Почта для входа в ваш аккаунт изменена.",
                        target="/#account",
                    )
                    result = self._dto(operation, account=account_dto(account))
        if error:
            raise error
        if delivery:
            self.accounts._deliver_code(*delivery[:2], delivery[2], "identity")
        return result
