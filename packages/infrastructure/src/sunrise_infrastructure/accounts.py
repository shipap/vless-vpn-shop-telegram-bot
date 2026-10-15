import hashlib
import hmac
import json
import re
import secrets
from datetime import timedelta
from pathlib import Path

from redis.exceptions import RedisError
from sqlalchemy import delete, func, select, text, update
from sunrise_core.errors import DomainError, missing

from .admin_mfa import AdminMFA
from .commerce import account_dto
from .db import (
    Account,
    AccountIdentityOperation,
    AdminAuthenticator,
    Agreement,
    Alias,
    Audit,
    Block,
    CampaignVisit,
    Challenge,
    CreditLedger,
    DeviceUnlink,
    Job,
    Ledger,
    LegacyPurchase,
    LegalConsent,
    MergeProof,
    Notification,
    NotificationDelivery,
    NotificationPreference,
    Order,
    Payment,
    PromoClaim,
    PromoUse,
    PushSubscription,
    Quote,
    Session,
    Setting,
    StaffRole,
    Subscription,
    now,
    uid,
)
from .legal import LEGAL_LOCK
from .mail import SMTPProductionMail, SMTPTestMail, synthetic_mail_address, valid_mail_address
from .referral_attribution import assign_new_referrer
from .security import digest


def require_account_access_in(session, account, policy):
    """Imported disabled identities cannot become new customer sessions or purchases."""
    if policy and getattr(policy, "scoped_payments", False):
        policy.require_current(session)
        policy.require_account_in(session, account)
        return
    if not policy or not policy.production:
        return
    policy.require_current(session)
    if account is None:
        raise missing()
    consent = account.legacy_consent or {}
    if not isinstance(consent, dict):
        raise missing()
    if "production_access" not in consent:
        # A new account has no historical evidence. Neither a missing import
        # flag nor a preserved panel binding is evidence of a new identity.
        if consent:
            raise missing()
        if session.scalar(text("SELECT to_regclass('users')")) is not None:
            if session.scalar(
                text("SELECT EXISTS (SELECT 1 FROM users WHERE user_id = :account_id)"),
                {"account_id": account.id},
            ):
                raise missing()
        imported = session.scalar(
            select(Setting.key)
            .where(
                Setting.key.like("panel_production:%"),
                Setting.value["origin"].as_string() == "imported_project_identity",
                Setting.value["account_id"].as_string() == str(account.id),
            )
            .limit(1)
        )
        if imported:
            raise missing()
        return
    value = consent["production_access"]
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "account_id", "state", "source", "source_account_id"}
        or type(value.get("version")) is not int
        or value["version"] != 1
        or value.get("account_id") != str(account.id)
        or value.get("source_account_id") != str(account.id)
        or value.get("source") != "legacy_user_is_active"
        or value.get("state") != "active"
    ):
        raise missing()


class Accounts:
    def __init__(self, db, guard, settings, mail_transport=None):
        self.db, self.guard, self.settings = db, guard, settings
        self.mail_transport = mail_transport
        self.admin_mfa = AdminMFA(self)

    def _code_digest(self, cid, code):
        return hmac.new(
            self.settings.secret_key.encode(), (cid + ":" + code).encode(), hashlib.sha256
        ).hexdigest()

    def _login_event(self, email, ip, cid, result, reason, account_id=None):
        from ipaddress import ip_address

        try:
            address = str(ip_address(ip))
        except ValueError:
            address = None
        with self.db.transaction() as s:
            s.add(
                Audit(
                    actor_id=account_id,
                    action="login_event",
                    target=cid or "email_request",
                    reason=reason,
                    details={
                        "email": email,
                        "challenge_id": cid,
                        "account_id": str(account_id) if account_id else None,
                        "result": result,
                        "ip": address,
                        "ip_provenance": "direct",
                    },
                )
            )

    def request_code(self, email, ip):
        try:
            return self._request_code(email, ip)
        except DomainError as exc:
            self._login_event(
                email.strip().lower(), ip, None, "blocked" if exc.status == 429 else "failure", exc.code
            )
            raise

    def _request_code(self, email, ip, *, challenge_context=None, purpose="login", link_binding=None):
        email = email.strip().lower()
        from .settings import runtime_policy

        policy = runtime_policy(self.settings)
        if policy and (policy.production or getattr(policy, "scoped_payments", False)):
            policy.require("auth_mail")
            if purpose not in {"login", "link", "identity"} or not valid_mail_address(email):
                raise DomainError("auth_mail_invalid", "Адрес или назначение кода не подтверждено.", 400)
            with self.db.transaction() as session:
                existing = session.scalar(
                    select(Account).where(Account.email == email).execution_options(populate_existing=True)
                )
                if existing:
                    require_account_access_in(session, existing, policy)
        context = digest(ip + ":" + email)
        stored_context = challenge_context or context
        self.guard.check(context)
        self.guard.check("ip:" + digest(ip))
        self.guard.resend(digest(email))
        code = f"{secrets.randbelow(1000000):06d}"
        cid = uid()
        with self.db.transaction() as s:
            s.execute(
                update(Challenge)
                .where(Challenge.email == email, Challenge.context == stored_context, ~Challenge.used)
                .values(used=True)
            )
            s.add(
                Challenge(
                    id=cid,
                    email=email,
                    digest=self._code_digest(cid, code),
                    context=stored_context,
                    expires_at=now() + timedelta(minutes=10),
                )
            )
            if policy and (policy.production or getattr(policy, "scoped_payments", False)):
                self._record_auth_mail_in(
                    s, cid, email, purpose, stored_context, context, ip, link_binding=link_binding
                )
        self._deliver_code(cid, code, email, purpose)
        return {"challenge_id": cid, "resend_after": 120, "expires_in": 600}

    def _record_auth_mail_in(
        self, session, cid, email, purpose, context, rate_context, ip, *, operation=None, link_binding=None
    ):
        from .settings import runtime_policy

        policy = runtime_policy(self.settings)
        if not policy or not (policy.production or getattr(policy, "scoped_payments", False)):
            return
        policy.require_current(session, "auth_mail")
        value = {
            "scope_id": policy.scope.scope_id,
            "purpose": purpose,
            "context": context,
            "email_hash": digest(email),
            "rate_context": rate_context,
            "ip_context": "ip:" + digest(ip),
        }
        if operation is not None:
            value.update(
                identity_operation=operation.id,
                identity_state=operation.state,
                identity_session=operation.session_id,
                identity_account=str(operation.account_id),
            )
        if purpose == "link":
            if not isinstance(link_binding, dict) or set(link_binding) != {"account_id", "session_id"}:
                raise DomainError(
                    "auth_mail_invalid", "Подтверждение привязки не связано с текущим сеансом.", 409
                )
            value.update(
                link_account=str(link_binding["account_id"]), link_session=link_binding["session_id"]
            )
        session.add(Setting(key="auth_mail:" + cid, value=value))

    def _deliver_code(self, cid, code, email, purpose="login"):
        from .instance_config import configured_instance

        subjects = {
            "login": "Код входа " + configured_instance(self.settings).app_name,
            "link": "Подтверждение привязки Telegram",
            "identity": "Подтверждение изменения аккаунта",
        }
        subject = subjects[purpose]
        body = (
            ("Код подтверждения привязки Telegram: " if purpose == "link" else "Код входа: ")
            + code
            + "\nСрок действия: 10 минут."
        )
        from .settings import runtime_policy

        policy = runtime_policy(self.settings)
        if policy and (policy.production or getattr(policy, "scoped_payments", False)):
            if not isinstance(self.mail_transport, SMTPProductionMail):
                raise DomainError("mail_unavailable", "Отправка почты временно недоступна.", 503)

            def authorize():
                with self.db.transaction() as session:
                    policy.require_current(session, "auth_mail")
                    challenge = session.get(Challenge, cid, populate_existing=True)
                    row = session.get(Setting, "auth_mail:" + cid, populate_existing=True)
                    evidence = row.value if row and isinstance(row.value, dict) else {}
                    if (
                        not challenge
                        or challenge.used
                        or challenge.expires_at <= now()
                        or challenge.email != email
                        or not valid_mail_address(email)
                        or not hmac.compare_digest(challenge.digest, self._code_digest(cid, code))
                        or evidence.get("scope_id") != policy.scope.scope_id
                        or evidence.get("purpose") != purpose
                        or evidence.get("email_hash") != digest(email)
                        or evidence.get("context") != challenge.context
                        or not isinstance(evidence.get("rate_context"), str)
                        or not isinstance(evidence.get("ip_context"), str)
                    ):
                        raise DomainError("auth_mail_invalid", "Код входа больше не действителен.", 409)
                    self.guard.check(evidence["rate_context"])
                    self.guard.check(evidence["ip_context"])
                    if purpose == "login":
                        existing = session.scalar(
                            select(Account)
                            .where(Account.email == email)
                            .execution_options(populate_existing=True)
                        )
                        if existing:
                            require_account_access_in(session, existing, policy)
                    if purpose == "link":
                        login = session.get(Session, evidence.get("link_session"), populate_existing=True)
                        actor = (
                            session.get(Account, login.account_id, populate_existing=True) if login else None
                        )
                        if (
                            not login
                            or login.expires_at <= now()
                            or not actor
                            or str(actor.id) != evidence.get("link_account")
                            or not actor.email_verified
                            or actor.email != email
                        ):
                            raise DomainError(
                                "auth_mail_invalid", "Подтверждение привязки больше не действительно.", 409
                            )
                        require_account_access_in(session, actor, policy)
                    if purpose == "identity":
                        operation = session.get(
                            AccountIdentityOperation,
                            evidence.get("identity_operation"),
                            populate_existing=True,
                        )
                        login = session.get(Session, evidence.get("identity_session"), populate_existing=True)
                        actor = (
                            session.get(Account, operation.account_id, populate_existing=True)
                            if operation
                            else None
                        )
                        if (
                            not operation
                            or not login
                            or not actor
                            or operation.kind not in {"email_add", "email_change"}
                            or operation.state not in {"current_email", "new_email"}
                            or operation.state != evidence.get("identity_state")
                            or operation.expires_at <= now()
                            or login.expires_at <= now()
                            or login.account_id != operation.account_id
                            or operation.session_id != login.id
                            or str(actor.id) != evidence.get("identity_account")
                            or operation.payload.get("challenge_id") != cid
                            or operation.payload.get("ip_digest") != digest(login.ip_address)
                            or email
                            != (
                                operation.old_email
                                if operation.state == "current_email"
                                else operation.new_email
                            )
                        ):
                            raise DomainError(
                                "auth_mail_invalid",
                                "Подтверждение изменения почты больше не действительно.",
                                409,
                            )
                        from .account_features import AccountsFeatures

                        require_account_access_in(session, actor, policy)
                        AccountsFeatures(self)._snapshot(actor, operation)

            authorize()
            return self.mail_transport.deliver(
                email, subject, body, cid, capability="auth_mail", before_send=authorize
            )
        if self.mail_transport and not (
            self.settings.local_test
            and synthetic_mail_address(email)
            and isinstance(self.mail_transport, SMTPTestMail)
        ):
            if email not in self.settings.email_allowlist or (
                not synthetic_mail_address(email)
                and not getattr(self.settings, "smtp_test_authorized", False)
            ):
                raise DomainError("mail_unavailable", "Отправка почты временно недоступна.", 503, True)
            self.mail_transport.deliver(email, subject, body, cid)
        elif (
            self.settings.local_test
            and email in self.settings.email_allowlist
            and synthetic_mail_address(email)
        ):
            folder = Path(self.settings.mail_dir).resolve()
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{cid}.json").write_text(
                json.dumps(
                    {"to": email, "subject": subject, "code": code, "expires_in": 600},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        elif not self.settings.local_test or not synthetic_mail_address(email):
            raise DomainError("mail_unavailable", "Отправка почты временно недоступна.", 503, True)

    def _link_identity(self, s, account_id, session_id):
        account = s.get(Account, int(account_id))
        session = s.get(Session, session_id)
        if not session or session.account_id != int(account_id) or session.expires_at <= now():
            raise DomainError("auth_required", "Войдите в аккаунт.", 401)
        if not account or not account.email_verified or not account.email:
            raise DomainError(
                "link_email_required", "Для привязки Telegram нужна подтверждённая почта аккаунта.", 403
            )
        from .settings import runtime_policy

        require_account_access_in(s, account, runtime_policy(self.settings))
        return account

    def _link_context(self, account_id, session_id, email, ip, purpose):
        binding = digest(str(account_id) + ":" + session_id + ":" + email + ":" + ip)
        return "link_email_" + purpose + ":" + binding

    def request_link_email(self, account_id, session_id, ip):
        with self.db.transaction() as s:
            account = self._link_identity(s, account_id, session_id)
            email = account.email
        context = self._link_context(account_id, session_id, email, ip, "pending")
        return self._request_code(
            email,
            ip,
            challenge_context=context,
            purpose="link",
            link_binding={"account_id": account.id, "session_id": session_id},
        )

    def _wrong_code(self, s, challenge, context, ip):
        blocked = self.guard.fail(context)
        ip_context = "ip:" + digest(ip)
        if self.guard.fail(ip_context, threshold=30, window=600):
            s.add(
                Block(
                    context=ip_context,
                    rule="ip_login_failures",
                    count=30,
                    threshold=30,
                    ends_at=now() + timedelta(hours=1),
                )
            )
        if blocked:
            challenge.used = True
            s.add(
                Block(
                    context=context,
                    rule="wrong_code",
                    count=10,
                    threshold=10,
                    ends_at=now() + timedelta(hours=1),
                )
            )
        return DomainError("code_invalid", "Код неверен или истёк.", 429 if blocked else 400)

    def verify_link_email(self, account_id, session_id, cid, code, ip):
        error = None
        with self.db.transaction() as s:
            account = self._link_identity(s, account_id, session_id)
            context = digest(ip + ":" + account.email)
            self.guard.check(context)
            self.guard.check("ip:" + digest(ip))
            challenge = s.scalar(select(Challenge).where(Challenge.id == cid).with_for_update())
            pending = self._link_context(account_id, session_id, account.email, ip, "pending")
            if (
                not challenge
                or challenge.used
                or challenge.expires_at <= now()
                or challenge.context != pending
                or challenge.email != account.email
            ):
                raise DomainError("code_invalid", "Код неверен или истёк.", 400)
            if not hmac.compare_digest(challenge.digest, self._code_digest(cid, code)):
                error = self._wrong_code(s, challenge, context, ip)
            else:
                verified = self._link_context(account_id, session_id, account.email, ip, "verified")
                challenge.used = True
                challenge.context = verified
                challenge.expires_at = now() + timedelta(minutes=5)
                s.flush()
                try:
                    if not self.guard.redis.set("sunrise:link-confirmed:" + verified, cid, ex=300):
                        raise DomainError(
                            "protection_unavailable", "Защита входа временно недоступна.", 503, True
                        )
                except RedisError:
                    raise DomainError(
                        "protection_unavailable", "Защита входа временно недоступна.", 503, True
                    ) from None
        if error:
            raise error
        return {"confirmed": True, "expires_in": 300}

    def _link_granted(self, account_id, session_id, ip):
        if not session_id or not ip:
            return False
        with self.db.transaction() as s:
            account = self._link_identity(s, account_id, session_id)
            return self._link_granted_in(s, account, session_id, ip)

    def _link_granted_in(self, s, account, session_id, ip):
        if not session_id or not ip or not account.email_verified or not account.email:
            return False
        verified = self._link_context(account.id, session_id, account.email, ip, "verified")
        try:
            cid = self.guard.redis.get("sunrise:link-confirmed:" + verified)
        except RedisError:
            raise DomainError(
                "protection_unavailable", "Защита входа временно недоступна.", 503, True
            ) from None
        if not cid:
            return False
        challenge = s.scalar(
            select(Challenge)
            .where(Challenge.id == (cid.decode() if isinstance(cid, bytes) else cid))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return bool(
            challenge
            and challenge.used
            and challenge.context == verified
            and challenge.email == account.email
            and challenge.expires_at > now()
        )

    def verify_code(
        self, cid, code, ip, *, legal_accepted=False, documents=None, referral_code=None, browser_label=None
    ):
        with self.db.transaction() as s:
            challenge = s.get(Challenge, cid)
            email = challenge.email if challenge else None
        try:
            result = self._verify_code(
                cid,
                code,
                ip,
                legal_accepted=legal_accepted,
                documents=documents,
                referral_code=referral_code,
                browser_label=browser_label,
            )
        except DomainError as exc:
            self._login_event(email, ip, cid, "blocked" if exc.status == 429 else "failure", exc.code)
            raise
        self._login_event(email, ip, cid, "success", "verified_code", int(result[1]["account"]["id"]))
        return result

    def _verify_code(
        self, cid, code, ip, *, legal_accepted=False, documents=None, referral_code=None, browser_label=None
    ):
        error = None
        result = None
        with self.db.transaction() as s:
            c = s.scalar(select(Challenge).where(Challenge.id == cid).with_for_update())
            if not c:
                raise DomainError("code_invalid", "Код неверен или истёк.", 400)
            context = digest(ip + ":" + c.email)
            self.guard.check(context)
            self.guard.check("ip:" + digest(ip))
            if c.used or c.expires_at <= now() or c.context != context:
                raise DomainError("code_invalid", "Код неверен или истёк.", 400)
            if not hmac.compare_digest(c.digest, self._code_digest(cid, code)):
                error = self._wrong_code(s, c, context, ip)
            else:
                if legal_accepted is not True or not documents:
                    raise DomainError(
                        "legal_consent_required",
                        "Ознакомьтесь с текущими документами и подтвердите согласие перед входом.",
                        409,
                        next_action="legal",
                    )
                c.used = True
                from sqlalchemy import text

                s.execute(text("SELECT pg_advisory_xact_lock(hashtext(:email))"), {"email": c.email})
                a = s.scalar(select(Account).where(Account.email == c.email).with_for_update())
                from .settings import runtime_policy

                if a:
                    require_account_access_in(s, a, runtime_policy(self.settings))
                if not a:
                    a = Account(email=c.email, email_verified=True)
                    s.add(a)
                    assign_new_referrer(s, a, referral_code)
                    s.flush()
                    policy = runtime_policy(self.settings)
                    if policy and getattr(policy, "scoped_payments", False):
                        policy.enroll_verified_email_in(s, a)
                a.email_verified = True
                from .panel_contacts import enqueue_contacts_in
                from .settings import runtime_policy

                enqueue_contacts_in(s, a, runtime_policy(self.settings), "email-verified:" + c.id)
                from .commerce import Commerce

                Commerce(self.db).legal.accept_in(s, a, documents, "website", "registration")
                token = secrets.token_urlsafe(32)
                csrf = secrets.token_urlsafe(32)
                session = Session(
                    id=digest(token),
                    account_id=a.id,
                    csrf=csrf,
                    expires_at=now() + timedelta(days=7),
                    email_confirmed_at=now(),
                )
                s.add(session)
                s.flush()
                self._session_started_in(s, a, session, "email_otp", ip, browser_label)
                result = (
                    token,
                    {
                        "account": account_dto(a),
                        "csrf_token": csrf,
                        "is_admin": self.is_admin(a),
                        "mfa_verified": False,
                    },
                )
        if error:
            raise error
        return result

    def is_admin(self, a):
        from .staff_permissions import telegram_admin_allowed

        return telegram_admin_allowed(a, self.settings)

    def _session_started_in(self, s, account, session, auth_method, ip, browser_label):
        from ipaddress import ip_address

        from .notifications import NotificationService

        if auth_method not in {"email_otp", "telegram_miniapp", "telegram_oidc", "unknown"}:
            raise DomainError("auth_origin_invalid", "Способ входа не подтверждён.", 400)
        try:
            address = str(ip_address(ip))
        except ValueError:
            address = None
        label = (
            browser_label
            if isinstance(browser_label, str)
            and 0 < len(browser_label) <= 200
            and not re.search(r"[\x00-\x1f\x7f]", browser_label)
            else None
        )
        context = digest((address or "unknown") + ":" + (label or "unknown"))
        known = s.scalar(
            select(Audit.id)
            .where(
                Audit.actor_id == account.id,
                Audit.action == "session_login",
                Audit.details["context"].as_string() == context,
                Audit.details["auth_method"].as_string() != "telegram_miniapp",
            )
            .limit(1)
        )
        session.auth_method, session.ip_address, session.browser_label = auth_method, address, label
        session.confirmed_at = session.created_at = session.last_seen_at = now()
        s.add(
            Audit(
                actor_id=account.id,
                action="session_login",
                target=session.public_id,
                reason="Подтверждённый вход",
                details={"context": context, "auth_method": auth_method},
            )
        )
        if not known and auth_method != "telegram_miniapp":
            NotificationService(self.db, self.settings).emit_in(
                s,
                account.id,
                "login:" + session.public_id,
                "login",
                "Новый вход в аккаунт",
                "Выполнен вход с нового устройства или адреса. Проверьте активные сеансы.",
                target="/#account",
            )

    def telegram_login(
        self,
        identity,
        ip,
        *,
        legal_accepted=False,
        documents=None,
        allow_current=False,
        claim=None,
        referral_code=None,
        auth_method="unknown",
        browser_label=None,
    ):
        from .commerce import Commerce

        with self.db.transaction() as s:
            s.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": identity.telegram_id})
            account = s.scalar(
                select(Account).where(Account.telegram_id == identity.telegram_id).with_for_update()
            )
            if not account:
                account = Account(telegram_id=identity.telegram_id)
                s.add(account)
                assign_new_referrer(s, account, referral_code)
                s.flush()
            from .settings import runtime_policy

            require_account_access_in(s, account, runtime_policy(self.settings))
            legal = Commerce(self.db).legal
            s.execute(select(func.pg_advisory_xact_lock(LEGAL_LOCK)))
            state = legal.status_in(s, account)
            if not (allow_current and state["accepted"]):
                if legal_accepted is not True or not documents:
                    raise DomainError(
                        "legal_consent_required",
                        "Прочитайте документы и подтвердите согласие перед входом.",
                        409,
                        next_action="legal",
                    )
                legal.accept_in(s, account, documents, "website", "registration")
            account.telegram_username = identity.username
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            session = Session(
                id=digest(token), account_id=account.id, csrf=csrf, expires_at=now() + timedelta(days=7)
            )
            s.add(session)
            s.flush()
            self._session_started_in(s, account, session, auth_method, ip, browser_label)
            if claim:
                claim()
            dto = {
                "account": account_dto(account),
                "csrf_token": csrf,
                "is_admin": self.is_admin(account),
                "mfa_verified": False,
            }
        return token, dto

    def is_bot_admin(self, account):
        return self.is_admin(account)

    def session(self, token):
        if not token:
            raise DomainError("auth_required", "Войдите в аккаунт.", 401, next_action="login")
        with self.db.transaction() as s:
            session = s.get(Session, digest(token))
            if not session or session.expires_at <= now():
                raise DomainError("auth_required", "Войдите в аккаунт.", 401, next_action="login")
            a = s.get(Account, session.account_id)
            from .settings import runtime_policy

            require_account_access_in(s, a, runtime_policy(self.settings))
            return a, session

    def logout(self, session_id):
        with self.db.transaction() as s:
            s.execute(delete(Session).where(Session.id == session_id))

    def mfa(self, account, session_id, code, ip):
        return self.admin_mfa.verify(account.id, session_id, code, ip)

    def request_link(
        self,
        account_id,
        target_telegram_id,
        confirmed_at,
        *,
        target_telegram_username=None,
        session_id=None,
        ip=None,
    ):
        # Legacy trusted service callers without a website session retain their explicit proof contract.
        # Every HTTP path supplies a session and rechecks genuine email proof under its mutation locks below.
        if session_id is None and confirmed_at < now() - timedelta(minutes=5):
            raise DomainError(
                "fresh_login_required",
                "Для привязки Telegram подтвердите почту кодом из письма прямо в этом окне.",
                403,
            )
        if (target_telegram_id is None) == (target_telegram_username is None):
            raise DomainError("identity_invalid", "Укажите ID или имя пользователя Telegram.", 400)
        if target_telegram_username is not None:
            if not isinstance(target_telegram_username, str) or not re.fullmatch(
                r"@?[A-Za-z0-9_]{5,32}", target_telegram_username
            ):
                raise DomainError("identity_invalid", "Проверьте имя Telegram.", 400)
            username = target_telegram_username.removeprefix("@").lower()
            target_condition = func.lower(Account.telegram_username) == username
        else:
            if (
                not isinstance(target_telegram_id, str)
                or not re.fullmatch(r"[1-9]\d{0,18}", target_telegram_id)
                or int(target_telegram_id) > 2**63 - 1
            ):
                raise DomainError("identity_invalid", "Проверьте ID Telegram.", 400)
            target_condition = Account.telegram_id == int(target_telegram_id)
        secret = secrets.token_urlsafe(32)
        with self.db.transaction() as s:
            matches = list(
                s.scalars(
                    select(Account.id).where(target_condition, Account.telegram_id.is_not(None)).limit(2)
                )
            )
            if len(matches) != 1:
                raise DomainError(
                    "link_target_unavailable",
                    "Не удалось определить Telegram. Откройте бота в нужном аккаунте и проверьте ID или имя.",
                    400,
                )
            if matches[0] == int(account_id):
                raise DomainError("already_linked", "Telegram уже привязан.")
            locked = {
                a.id: a
                for a in s.scalars(
                    select(Account)
                    .where(Account.id.in_([int(account_id), matches[0]]))
                    .order_by(Account.id)
                    .with_for_update()
                )
            }
            a, b = locked.get(int(account_id)), locked.get(matches[0])
            if (
                not b
                or b.telegram_id is None
                or (target_telegram_username is not None and (b.telegram_username or "").lower() != username)
                or (target_telegram_id is not None and b.telegram_id != int(target_telegram_id))
            ):
                raise DomainError("link_target_unavailable", "Telegram изменился. Повторите привязку.", 400)
            if not a:
                raise missing()
            if not a.email_verified or not a.email or a.telegram_id is not None or b.email is not None:
                raise DomainError(
                    "identity_conflict", "Привязка требует проверки существующих способов входа."
                )
            initiating_session = None
            if session_id is not None:
                initiating_session = s.scalar(
                    select(Session)
                    .where(Session.id == session_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if (
                    not initiating_session
                    or initiating_session.account_id != a.id
                    or initiating_session.expires_at <= now()
                ):
                    raise DomainError("auth_required", "Войдите в аккаунт.", 401)
                fresh_email = bool(
                    ip
                    and initiating_session.ip_address == ip
                    and initiating_session.email_confirmed_at
                    and now() - timedelta(minutes=5) <= initiating_session.email_confirmed_at <= now()
                )
                if not fresh_email and not self._link_granted_in(s, a, session_id, ip):
                    raise DomainError(
                        "fresh_login_required",
                        "Для привязки Telegram подтвердите почту кодом из письма прямо в этом окне.",
                        403,
                    )
            proof = MergeProof(
                source_id=a.id,
                target_id=b.id,
                secret_hash=digest(secret),
                expires_at=now() + timedelta(minutes=5),
            )
            s.add(proof)
            s.flush()
            if initiating_session:
                binding = {
                    "session_id": initiating_session.id,
                    "source_id": str(a.id),
                    "target_id": str(b.id),
                    "email_hash": digest(a.email),
                }
                try:
                    if not self.guard.redis.set(
                        "sunrise:link-session:" + proof.id, json.dumps(binding), nx=True, ex=300
                    ):
                        raise DomainError(
                            "protection_unavailable", "Защита входа временно недоступна.", 503, True
                        )
                except RedisError:
                    raise DomainError(
                        "protection_unavailable", "Защита входа временно недоступна.", 503, True
                    ) from None
            return {"proof": proof.id + "." + secret, "expires_in": 300, "requires_merge": True}

    def _merge_initiating_session(self, s, proof, source):
        try:
            raw = self.guard.redis.get("sunrise:link-session:" + proof.id)
        except RedisError:
            raise DomainError(
                "protection_unavailable", "Защита входа временно недоступна.", 503, True
            ) from None
        if not raw or len(raw) > 1024:
            return None
        try:
            binding = json.loads(raw)
            if (
                not isinstance(binding, dict)
                or binding.get("source_id") != str(source.id)
                or binding.get("target_id") != str(proof.target_id)
                or binding.get("email_hash") != digest(source.email or "")
            ):
                return None
            session_id = binding.get("session_id")
            if not isinstance(session_id, str) or not re.fullmatch(r"[0-9a-f]{64}", session_id):
                return None
            session = s.scalar(select(Session).where(Session.id == session_id).with_for_update())
            return (
                session
                if session and session.account_id == source.id and session.expires_at > now()
                else None
            )
        except (ValueError, TypeError, UnicodeError):
            return None

    def confirm_telegram_link(self, account_id, proof):
        try:
            pid, secret = proof.split(".", 1)
        except ValueError:
            raise DomainError("link_invalid", "Подтверждение привязки недействительно.", 400) from None
        with self.db.transaction() as s:
            p = s.scalar(select(MergeProof).where(MergeProof.id == pid).with_for_update())
            if (
                not p
                or p.used
                or p.expires_at <= now()
                or p.target_id != int(account_id)
                or not hmac.compare_digest(p.secret_hash, digest(secret))
            ):
                raise DomainError("link_invalid", "Подтверждение привязки недействительно.", 400)
            accounts = list(
                s.scalars(
                    select(Account)
                    .where(Account.id.in_([p.source_id, p.target_id]))
                    .order_by(Account.id)
                    .with_for_update()
                )
            )
            if len(accounts) != 2:
                raise DomainError("link_invalid", "Аккаунты уже изменились.")
            a = next(x for x in accounts if x.id == p.source_id)
            b = next(x for x in accounts if x.id == p.target_id)
            from .settings import runtime_policy

            scoped_policy = runtime_policy(self.settings)
            scoped_merge = (
                scoped_policy.prepare_merge_in(s, a, b, pid)
                if scoped_policy and getattr(scoped_policy, "scoped_payments", False)
                else None
            )
            from .retention import prepare_merge_in, transfer_merge_in

            retention_states = prepare_merge_in(s, [a.id, b.id])
            from .subscription_engagement import merge_states_in

            engagement_states = merge_states_in(
                s,
                list(
                    s.scalars(
                        select(Subscription)
                        .where(Subscription.account_id.in_([a.id, b.id]))
                        .order_by(Subscription.id)
                        .with_for_update()
                    )
                ),
            )
            from .subscription_archive import prepare_merge_in as prepare_archive_merge_in

            archive_states = prepare_archive_merge_in(
                s,
                list(
                    s.scalars(
                        select(Subscription)
                        .where(Subscription.account_id.in_([a.id, b.id]))
                        .order_by(Subscription.id)
                        .with_for_update()
                    )
                ),
            )
            if a.telegram_id or b.email or not b.telegram_id:
                raise DomainError("identity_conflict", "Способы входа изменились.")
            initiating_session = self._merge_initiating_session(s, p, a)
            if a.pending_promo and b.pending_promo:
                raise DomainError(
                    "promo_merge_conflict",
                    "Перед объединением используйте промокод одного из аккаунтов.",
                    409,
                )
            claimed_a = set(s.scalars(select(PromoClaim.code).where(PromoClaim.account_id == a.id)))
            claimed_b = set(s.scalars(select(PromoClaim.code).where(PromoClaim.account_id == b.id)))
            if claimed_a & claimed_b:
                raise DomainError(
                    "promo_merge_conflict",
                    "Выдачи подарочных промокодов требуют проверки перед объединением.",
                    409,
                )
            if b.pending_promo:
                a.pending_promo = b.pending_promo
            if a.credit_balance != 0 and b.credit_balance != 0 and a.credit_currency != b.credit_currency:
                raise DomainError(
                    "credit_currency_review",
                    "Валюты денежных остатков требуют сверки перед объединением.",
                    409,
                )
            if a.credit_balance == 0 and a.credit_reserved == 0:
                a.credit_currency, a.credit_provenance = b.credit_currency, b.credit_provenance
            from .staff_permissions import StaffPermissions

            list(
                s.scalars(
                    select(StaffRole)
                    .where(StaffRole.account_id.in_([a.id, b.id]))
                    .order_by(StaffRole.account_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            target_owner_qualified = StaffPermissions(self).role_in(s, b) == "owner"
            authenticators = {
                record.account_id: record
                for record in s.scalars(
                    select(AdminAuthenticator)
                    .where(AdminAuthenticator.account_id.in_([a.id, b.id]))
                    .order_by(AdminAuthenticator.account_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            }
            source_authenticator = authenticators.get(a.id)
            target_authenticator = authenticators.get(b.id)
            if target_authenticator is not None and target_authenticator.secret_ciphertext is not None:
                from .admin_mfa import AdminMFA

                mfa = AdminMFA(self)
                if (
                    target_owner_qualified is not True
                    or (source_authenticator is not None and source_authenticator.secret_ciphertext is not None)
                    or mfa._configured_secret(a)
                ):
                    raise DomainError(
                        "mfa_merge_review_required",
                        "Объединение настроек дополнительной защиты требует отдельной проверки.",
                        409,
                    )
                ciphertext = mfa._encrypt(a.id, mfa._decrypt(b.id, target_authenticator.secret_ciphertext))
                if source_authenticator is None:
                    source_authenticator = AdminAuthenticator(account_id=a.id)
                    s.add(source_authenticator)
                source_authenticator.secret_ciphertext = ciphertext
                source_authenticator.activated_at = target_authenticator.activated_at
                source_authenticator.last_totp_step = target_authenticator.last_totp_step
                source_authenticator.pending_ciphertext = None
                source_authenticator.pending_session_id = None
                source_authenticator.pending_expires_at = None
            telegram = b.telegram_id
            b.telegram_id = None
            s.flush()
            a.telegram_id = telegram
            a.telegram_username = b.telegram_username
            a.telegram_notifications_blocked = (
                a.telegram_notifications_blocked or b.telegram_notifications_blocked
            )
            self._merge_account_features_in(s, a, b, target_owner_qualified=target_owner_qualified)
            a.unlink_warnings_total += b.unlink_warnings_total
            combined_warnings = a.unlink_warnings + b.unlink_warnings
            a.unlink_blocked = a.unlink_blocked or b.unlink_blocked or combined_warnings >= 2
            a.unlink_warnings = combined_warnings % 2
            a.bonus_balance += b.bonus_balance
            a.bonus_reserved += b.bonus_reserved
            a.credit_balance += b.credit_balance
            a.credit_reserved += b.credit_reserved
            if b.legacy_consent:
                a.legacy_consent = {**a.legacy_consent, "merged:" + str(b.id): b.legacy_consent}
            # Caller keys were scoped to two independent accounts. Preserve both namespaces.
            for old_order in s.scalars(select(Order).where(Order.account_id == b.id)):
                original = old_order.idempotency_key
                old_order.idempotency_key = "merged:" + str(b.id) + ":" + digest(original)
            for model in [
                Subscription,
                Quote,
                Order,
                Payment,
                Ledger,
                CreditLedger,
                Job,
                Agreement,
                PromoUse,
                PromoClaim,
                LegacyPurchase,
                LegalConsent,
                DeviceUnlink,
            ]:
                s.execute(update(model).where(model.account_id == b.id).values(account_id=a.id))
            transfer_merge_in(s, retention_states, a.id)
            from .subscription_engagement import transfer_merge_in as transfer_engagement_in

            transfer_engagement_in(s, engagement_states, a.id)
            from .subscription_archive import transfer_merge_in as transfer_archive_merge_in

            transfer_archive_merge_in(s, archive_states, a.id)
            s.execute(update(Account).where(Account.referrer_id == b.id).values(referrer_id=a.id))
            if a.referrer_id in {a.id, b.id}:
                a.referrer_id = None
            s.execute(update(Alias).where(Alias.canonical_id == b.id).values(canonical_id=a.id))
            s.add(Alias(old_id=b.id, canonical_id=a.id))
            revoked = delete(Session).where(Session.account_id.in_([a.id, b.id]))
            if initiating_session:
                revoked = revoked.where(Session.id != initiating_session.id)
                initiating_session.mfa_at = None
            s.execute(revoked)
            s.execute(
                update(AdminAuthenticator)
                .where(AdminAuthenticator.account_id == a.id)
                .values(
                    trusted_ip_digest=None,
                    trust_verified_at=None,
                    pending_ciphertext=None,
                    pending_session_id=None,
                    pending_expires_at=None,
                )
            )
            s.execute(delete(AdminAuthenticator).where(AdminAuthenticator.account_id == b.id))
            s.execute(
                delete(MergeProof).where(
                    (MergeProof.source_id.in_([a.id, b.id])) | (MergeProof.target_id.in_([a.id, b.id]))
                )
            )
            merge_audit = Audit(
                actor_id=a.id,
                action="account_merge",
                target=str(b.id),
                reason="Подтверждены email и Telegram",
                details={"canonical_id": str(a.id)},
            )
            s.add(merge_audit)
            s.delete(b)
            if scoped_merge is not None:
                scoped_policy.complete_merge_in(s, a, b.id, pid, scoped_merge, merge_audit)
            from .panel_contacts import enqueue_contacts_in
            from .settings import runtime_policy

            enqueue_contacts_in(s, a, runtime_policy(self.settings), "telegram-linked:" + pid)
            from .notifications import NotificationService

            NotificationService(self.db, self.settings).emit_in(
                s,
                a.id,
                "telegram-linked:" + pid,
                "identity_linked",
                "Аккаунты связаны",
                "Telegram успешно привязан к вашему аккаунту. Теперь способы входа используют один профиль. "
                "Если это сделали не вы, откройте раздел «Аккаунт» и подтвердите отвязку Telegram. "
                "В разделе «Сессии» можно завершить неизвестные входы.",
                channel="all",
                target="/#account",
                actions=[{"label": "Отвязать Telegram", "target": "/#account"}],
            )
            return account_dto(a)

    def _merge_account_features_in(self, s, source, target, *, target_owner_qualified):
        from .staff_permissions import StaffPermissions

        target_role = s.get(StaffRole, target.id)
        source_role = s.get(StaffRole, source.id)
        if (
            (target_role and target_role.role == "owner" or source_role and source_role.role == "owner")
            and target_owner_qualified is not True
        ):
            raise DomainError(
                "owner_merge_review_required",
                "Объединение аккаунта владельца требует отдельной проверки.",
                409,
            )
        if target_role and target_role.role == "owner":
            if source_role is None:
                s.add(
                    StaffRole(
                        account_id=source.id,
                        role=target_role.role,
                        granted_at=target_role.granted_at,
                        granted_by=target_role.granted_by,
                    )
                )
            else:
                source_role.role = target_role.role
                source_role.granted_at = target_role.granted_at
                source_role.granted_by = target_role.granted_by
        s.execute(delete(StaffRole).where(StaffRole.account_id == target.id))
        StaffPermissions(self).ensure_owner_in(s, source)
        notification_ids = select(Notification.id).where(Notification.account_id.in_([source.id, target.id]))
        s.execute(
            update(NotificationDelivery)
            .where(
                NotificationDelivery.notification_id.in_(notification_ids),
                NotificationDelivery.state.in_(
                    ["pending", "processing", "sending", "leased", "retry", "retrying"]
                ),
            )
            .values(state="cancelled", last_error="identity_merged")
        )
        for notification in s.scalars(
            select(Notification).where(Notification.account_id == target.id).with_for_update()
        ):
            notification.event_key = "merged:" + str(target.id) + ":" + digest(notification.event_key)
            notification.account_id = source.id
        # Согласие на browser delivery не включается вследствие объединения.
        s.execute(delete(NotificationPreference).where(NotificationPreference.account_id == target.id))
        s.execute(
            update(PushSubscription)
            .where(PushSubscription.account_id == target.id)
            .values(account_id=source.id, disabled_at=now())
        )
        for operation in s.scalars(
            select(AccountIdentityOperation)
            .where(AccountIdentityOperation.account_id.in_([source.id, target.id]))
            .with_for_update()
        ):
            if operation.state not in {"completed", "cancelled"}:
                operation.state, operation.completed_at = "cancelled", now()
                cid = operation.payload.get("challenge_id")
                if cid:
                    s.execute(update(Challenge).where(Challenge.id == cid).values(used=True))
            if operation.account_id == target.id:
                operation.payload = {**operation.payload, "original_account_id": str(target.id)}
                operation.account_id = source.id
        s.execute(
            update(CampaignVisit).where(CampaignVisit.account_id == target.id).values(account_id=source.id)
        )
