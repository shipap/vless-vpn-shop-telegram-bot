"""Регистрация TOTP и фиксированное доверие к прямому IP администратора."""

import base64
import hashlib
import hmac
import json
from datetime import timedelta
from ipaddress import ip_address

import pyotp
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import select
from sunrise_core.errors import DomainError, missing

from .db import Account, AdminAuthenticator, Audit, Block, Session, now


class AdminMFA:
    def __init__(self, accounts):
        self.accounts, self.db, self.settings, self.guard = (
            accounts,
            accounts.db,
            accounts.settings,
            accounts.guard,
        )

    def _cipher(self):
        key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b"sunrise-shop-v10",
            info=b"admin-totp-fernet",
        ).derive(self.settings.secret_key.encode())
        return Fernet(base64.urlsafe_b64encode(key))

    def _encrypt(self, account_id, secret):
        payload = json.dumps({"account_id": str(account_id), "secret": secret}).encode()
        return self._cipher().encrypt(payload).decode()

    def _decrypt(self, account_id, ciphertext):
        try:
            payload = json.loads(self._cipher().decrypt(ciphertext.encode()))
            if payload["account_id"] != str(account_id) or not isinstance(payload["secret"], str):
                raise ValueError("invalid context")
            return payload["secret"]
        except (InvalidToken, ValueError, KeyError, TypeError):
            raise DomainError("mfa_unavailable", "Дополнительная защита временно недоступна.", 503) from None

    def _ip_digest(self, ip):
        try:
            address = str(ip_address(ip))
        except ValueError:
            raise DomainError(
                "mfa_context_invalid", "Не удалось подтвердить адрес подключения.", 403
            ) from None
        return hmac.new(
            self.settings.secret_key.encode(), ("admin-trust-ip:" + address).encode(), hashlib.sha256
        ).hexdigest()

    def _configured_secret(self, account):
        return self.settings.admin_totp_secrets.get(account.email)

    def _account(self, s, account_id, *, lock=False):
        query = select(Account).where(Account.id == int(account_id))
        account = s.scalar(query.with_for_update() if lock else query)
        if not account or not self.accounts.is_admin(account):
            raise missing()
        return account

    def _session(self, s, account_id, session_id, *, fresh=False, ip=None):
        session = s.get(Session, session_id)
        if not session or session.account_id != account_id or session.expires_at <= now():
            raise DomainError("auth_required", "Войдите в аккаунт.", 401, next_action="login")
        moment = now()
        fresh_email = bool(
            session.email_confirmed_at
            and moment - timedelta(minutes=5) <= session.email_confirmed_at <= moment
            and moment - timedelta(minutes=5) <= session.confirmed_at <= moment
        )
        fresh_telegram = bool(
            ip and session.ip_address == ip
            and session.auth_method in {"telegram_oidc", "telegram_miniapp"}
            and session.created_at
            and moment - timedelta(minutes=5) <= session.created_at <= moment
            and moment - timedelta(minutes=5) <= session.confirmed_at <= moment
        )
        if fresh and not (fresh_email or fresh_telegram):
            raise DomainError(
                "fresh_login_required",
                "Войдите заново перед настройкой дополнительной защиты.",
                403,
                next_action="login",
            )
        return session

    def _record(self, s, account_id):
        record = s.get(AdminAuthenticator, account_id)
        if not record:
            record = AdminAuthenticator(account_id=account_id)
            s.add(record)
            s.flush()
        return record

    def _status(self, account, record, ip):
        enrolled = bool(record and record.secret_ciphertext or self._configured_secret(account))
        expires = (
            record.trust_verified_at + timedelta(hours=48) if record and record.trust_verified_at else None
        )
        trusted = bool(
            enrolled
            and expires
            and record.trust_verified_at <= now() < expires
            and hmac.compare_digest(record.trusted_ip_digest, self._ip_digest(ip))
        )
        return {
            "enrolled": enrolled,
            "enrollment_required": not enrolled,
            "trusted": trusted,
            "trust_expires_at": expires.isoformat() if expires else None,
        }

    def status(self, account_id, ip):
        with self.db.transaction() as s:
            account = self._account(s, account_id)
            return self._status(account, s.get(AdminAuthenticator, account.id), ip)

    def trusted(self, account, ip):
        if not self.accounts.is_admin(account):
            return False
        return self.status(account.id, ip)["trusted"]

    def begin(self, account_id, session_id, *, ip=None):
        key = "mfa:" + str(account_id)
        self.guard.check(key)
        with self.db.transaction() as s:
            account = self._account(s, account_id, lock=True)
            self._session(s, account.id, session_id, fresh=True, ip=ip)
            record = self._record(s, account.id)
            if record.secret_ciphertext or self._configured_secret(account):
                raise DomainError("mfa_already_enrolled", "Аутентификатор уже настроен.", 409)
            if (
                record.pending_session_id == session_id
                and record.pending_expires_at
                and record.pending_expires_at > now()
            ):
                secret = self._decrypt(account.id, record.pending_ciphertext)
            else:
                secret = pyotp.random_base32()
                record.pending_ciphertext = self._encrypt(account.id, secret)
                record.pending_session_id = session_id
                record.pending_expires_at = now() + timedelta(minutes=10)
            return {
                "secret": secret,
                "provisioning_uri": pyotp.TOTP(secret).provisioning_uri(
                    name=account.email or str(account.telegram_id), issuer_name=self.settings.instance.app_name
                ),
                "expires_at": record.pending_expires_at.isoformat(),
            }

    def _failure(self, s, key):
        blocked = self.guard.fail(key)
        if blocked:
            s.add(
                Block(
                    context=key,
                    rule="mfa_failures",
                    count=10,
                    threshold=10,
                    ends_at=now() + timedelta(hours=1),
                )
            )
        return DomainError("mfa_invalid", "Код дополнительной защиты неверен.", 429 if blocked else 400)

    def verify(self, account_id, session_id, code, ip, *, enrollment=False):
        key = "mfa:" + str(account_id)
        self.guard.check(key)
        ip_digest = self._ip_digest(ip)
        error = None
        with self.db.transaction() as s:
            account = self._account(s, account_id, lock=True)
            session = self._session(s, account.id, session_id)
            record = self._record(s, account.id)
            if enrollment:
                if (
                    record.secret_ciphertext
                    or self._configured_secret(account)
                    or record.pending_session_id != session_id
                    or not record.pending_expires_at
                    or record.pending_expires_at <= now()
                ):
                    raise DomainError("mfa_enrollment_invalid", "Настройка истекла. Начните заново.", 400)
                secret = self._decrypt(account.id, record.pending_ciphertext)
            else:
                secret = (
                    self._decrypt(account.id, record.secret_ciphertext)
                    if record.secret_ciphertext
                    else self._configured_secret(account)
                )
                if not secret:
                    raise DomainError("mfa_enrollment_required", "Настройте приложение-аутентификатор.", 403)
            moment = now()
            totp = pyotp.TOTP(secret)
            if not totp.verify(code, for_time=moment, valid_window=0):
                error = self._failure(s, key)
            else:
                step = int(moment.timestamp()) // totp.interval
                if record.last_totp_step is not None and step <= record.last_totp_step:
                    raise DomainError("mfa_replay", "Этот код уже использован. Дождитесь следующего.", 400)
                if enrollment:
                    record.secret_ciphertext = record.pending_ciphertext
                    record.activated_at = moment
                    record.pending_ciphertext = record.pending_session_id = record.pending_expires_at = None
                record.last_totp_step = step
                record.trusted_ip_digest = ip_digest
                record.trust_verified_at = moment
                session.mfa_at = moment
                s.add(
                    Audit(
                        actor_id=account.id,
                        action="admin_mfa_enrolled" if enrollment else "admin_mfa_verified",
                        target=str(account.id),
                        reason="Подтверждён код аутентификатора",
                        details={"trust_expires_at": (moment + timedelta(hours=48)).isoformat()},
                    )
                )
                result = self._status(account, record, ip)
        if error:
            raise error
        return result
