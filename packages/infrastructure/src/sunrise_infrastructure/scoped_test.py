"""Отдельный допуск реальных счетов для изолированного синтетического стенда."""

import hashlib
import json
import re
from decimal import Decimal
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, model_validator
from sqlalchemy import or_, select
from sqlalchemy.engine import make_url
from sunrise_core.errors import DomainError

from .db import (
    Account,
    Alias,
    Audit,
    CreditLedger,
    Job,
    Ledger,
    LegacyIssue,
    Order,
    ProviderOperation,
    Quote,
    Segment,
    Setting,
    Subscription,
)
from .settings import (
    BankScope,
    DatabaseScope,
    MailScope,
    PanelScope,
    RedisScope,
    RuntimePolicy,
    StrictScope,
    _owned_file,
    _unique_json,
)


class ScopedTestAdmission(StrictScope):
    order_prefix: str = Field(default="qa_", pattern=r"^[g-z][a-z0-9]{1,10}_$")
    version: Literal[1]
    scope_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    database: DatabaseScope
    redis: RedisScope
    instance_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    bot_id: str = Field(pattern=r"^[1-9][0-9]{0,18}$")
    telegram_id: str = Field(pattern=r"^[1-9][0-9]{0,18}$")
    landing_origin: str
    dashboard_origin: str
    callback_origin: str
    rollypay: BankScope
    panel: PanelScope
    allow_email_signup: bool = False
    smtp: MailScope | None = None

    @model_validator(mode="after")
    def isolated_resources(self):
        if (
            self.database.host not in {"127.0.0.1", "localhost"}
            or self.database.port != 55439
            or self.database.name not in {"sunrise_test", "sunrise_test_backend", "sunrise_test_bot"}
            or self.redis.host not in {"127.0.0.1", "localhost"}
            or int(self.bot_id) >= 2**63
            or int(self.telegram_id) >= 2**63
            or not self.rollypay.checkout_hosts
        ):
            raise ValueError("scoped_test_resource_denied")
        for origin in (
            self.landing_origin,
            self.dashboard_origin,
            self.callback_origin,
            self.rollypay.origin,
            self.panel.origin,
        ):
            url = urlparse(origin)
            if not url.hostname or origin != "https://" + url.hostname or url.hostname.endswith(".test"):
                raise ValueError("scoped_test_origin_denied")
        for host in self.rollypay.checkout_hosts:
            if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", host) or ".." in host:
                raise ValueError("scoped_test_origin_denied")
        if self.landing_origin == self.dashboard_origin:
            raise ValueError("scoped_test_origin_denied")
        if not self.panel.internal_squads:
            raise ValueError("scoped_test_panel_denied")
        if self.allow_email_signup and self.smtp is None:
            raise ValueError("scoped_test_mail_denied")
        from uuid import UUID

        for value in [*self.panel.internal_squads, *self.panel.external_squads, *self.panel.node_uuids]:
            if str(UUID(value)) != value:
                raise ValueError("scoped_test_panel_denied")
        return self


def load_scoped_test_scope(config):
    from .instance_config import PROJECT_ROOT

    relative, pin = config.scoped_test_admission_file or "", config.scoped_test_admission_sha256 or ""
    if not relative.startswith(".private/scoped-test/") or not re.fullmatch(r"[a-f0-9]{64}", pin):
        raise ValueError("scoped_test_admission_missing")
    path = _owned_file(PROJECT_ROOT.resolve(), relative)
    raw = path.read_bytes()
    if len(raw) > 65536 or hashlib.sha256(raw).hexdigest() != pin:
        raise ValueError("scoped_test_admission_changed")
    return ScopedTestAdmission.model_validate(json.loads(raw, object_pairs_hook=_unique_json)), path


def validate_scoped_test_settings(config, scope):
    db, target, redis = make_url(config.database_url), scope.database, urlparse(config.redis_url)
    if (db.get_backend_name(), db.host, db.port, db.database, db.username) != (
        "postgresql",
        target.host,
        target.port,
        target.name,
        target.role,
    ) or db.query:
        raise ValueError("scoped_test_database_denied")
    if (redis.scheme, redis.hostname, redis.port, redis.path, redis.query, redis.fragment) != (
        "redis",
        scope.redis.host,
        scope.redis.port,
        "/" + str(scope.redis.database),
        "",
        "",
    ):
        raise ValueError("scoped_test_redis_denied")
    if (
        not config.local_test
        or not config.cookie_secure
        or config.runtime_mode != "scoped_test"
        or config.production_admission_file
        or config.production_admission_sha256
        or config.rollypay_test_authorized
        or config.recurring_test_authorized
        or config.smtp_test_authorized != scope.allow_email_signup
        or config.push_test_authorized
        or config.email_allowlist
        or config.bot_token
        or not config.bot_test_token
        or config.bot_test_token.split(":", 1)[0] != scope.bot_id
        or {x.strip() for x in config.test_bot_ids.split(",") if x.strip()} != {scope.bot_id}
        or {x.strip() for x in config.test_telegram_recipients.split(",") if x.strip()} != {scope.telegram_id}
    ):
        raise ValueError("scoped_test_transport_denied")
    if (
        config.landing_url.rstrip("/") != scope.landing_origin
        or config.dashboard_url.rstrip("/") != scope.dashboard_origin
        or config.webhook_base_url.rstrip("/") != scope.callback_origin
        or config.rollypay_base_url.rstrip("/") != scope.rollypay.origin
        or config.rollypay_terminal != scope.rollypay.terminal
        or set(config.payment_allowed_hosts) != {urlparse(scope.rollypay.origin).hostname}
        or not set(config.allowed_hosts)
        <= {
            "localhost",
            "127.0.0.1",
            urlparse(scope.landing_origin).hostname,
            urlparse(scope.dashboard_origin).hostname,
            urlparse(scope.callback_origin).hostname,
        }
        or not set(config.allowed_origins) <= {scope.landing_origin, scope.dashboard_origin}
    ):
        raise ValueError("scoped_test_origin_denied")
    if (
        not config.panel_test_authorized
        or config.panel_contract != "3.4_id"
        or config.panel_base_url != scope.panel.origin
        or set(config.panel_allowed_hosts) != {urlparse(scope.panel.origin).hostname}
        or set(config.panel_test_squads) != set(scope.panel.internal_squads)
        or set(config.panel_test_external_squads) != set(scope.panel.external_squads)
        or set(config.panel_test_node_uuids) != set(scope.panel.node_uuids)
        or set(config.panel_subscription_allowed_hosts) != set(scope.panel.subscription_hosts)
        or config.panel_test_bindings
        or config.panel_test_user_ids
        or config.panel_test_uuids
        or config.panel_test_short_uuids
        or config.panel_test_usernames
        or config.panel_test_hwids
        or config.panel_test_device_bindings
    ):
        raise ValueError("scoped_test_panel_denied")
    if scope.allow_email_signup and (
        config.smtp_host,
        config.smtp_port,
        config.smtp_mode,
        config.smtp_sender,
    ) != (scope.smtp.host, scope.smtp.port, scope.smtp.mode, scope.smtp.sender):
        raise ValueError("scoped_test_mail_denied")
    if scope.allow_email_signup and set(config.smtp_allowed_hosts) != {scope.smtp.host}:
        raise ValueError("scoped_test_mail_denied")


class ScopedTestPolicy(RuntimePolicy):
    def __init__(self, config, scope, path):
        # Production admission and capabilities remain a separate contract.
        self.config, self.scope, self.path = config, scope, path
        self._production, self._database, self._attested = False, None, False
        self._admission_sha256 = config.scoped_test_admission_sha256

    @property
    def scoped_payments(self):
        return True

    @property
    def payment_environment(self):
        return "scoped_live"

    def _marker(self):
        return {
            "version": 1,
            "scope_id": self.scope.scope_id,
            "admission_sha256": self._admission_sha256,
            "instance_sha256": self.scope.instance_sha256,
            "synthetic_only": True,
        }

    def admit(self, database):
        self._database = database
        with database.transaction() as session:
            self._check_database(session)
            self._check_current(session)
            accounts = list(session.scalars(select(Account)))
            for account in accounts:
                if (
                    account.legacy_consent
                    or account.email
                    and not account.email.endswith("@example.test")
                    or session.scalar(select(Alias.old_id).where(Alias.canonical_id == account.id).limit(1))
                ):
                    self.require_account_in(session, account)
            for alias in session.scalars(select(Alias)):
                self.require_account_in(session, session.get(Account, alias.canonical_id))
            if session.scalar(select(LegacyIssue.id).limit(1)):
                raise ValueError("scoped_test_imported_data_denied")
            if session.scalar(select(Subscription.id).where(Subscription.environment == "live").limit(1)):
                raise ValueError("scoped_test_imported_data_denied")
        self._attested = True
        return self

    def _check_current(self, session):
        from .instance_config import PROJECT_ROOT

        instance = _owned_file(PROJECT_ROOT.resolve(), self.config.instance_config_path)
        # Reparse/path containment is rechecked for both mutable local files.
        _, path = load_scoped_test_scope(self.config)
        if (
            path != self.path
            or hashlib.sha256(instance.read_bytes()).hexdigest() != self.scope.instance_sha256
        ):
            raise ValueError("scoped_test_admission_changed")
        marker = session.get(Setting, "scoped_test_admission", populate_existing=True)
        if not marker or marker.value != self._marker():
            raise ValueError("scoped_test_admission_changed")

    def require_current(self, session, capability=None):
        self._capability(capability)
        try:
            if not self._attested:
                raise ValueError("scoped_test_admission_missing")
            self._check_database(session)
            self._check_current(session)
        except (OSError, ValueError, TypeError, AttributeError):
            raise DomainError(
                "scoped_test_admission_denied", "Допуск тестового контура не подтверждён.", 503
            ) from None

    def _capability(self, capability):
        if capability == "auth_mail" and getattr(self.scope, "allow_email_signup", False):
            return
        if capability not in {
            None,
            "invoice_create",
            "payment_reconcile",
            "financial_apply",
            "panel_mutation",
        }:
            raise DomainError("scoped_test_stage_denied", "Операция недоступна в тестовом контуре.", 403)

    def require(self, capability=None):
        self._capability(capability)
        if self._database is None:
            raise DomainError("scoped_test_admission_denied", "Допуск тестового контура не подтверждён.", 503)
        with self._database.transaction() as session:
            self.require_current(session, capability)

    def require_account_in(self, session, account):
        marker = (
            session.get(Setting, "scoped_test_account:" + str(account.id), populate_existing=True)
            if account
            else None
        )
        if (
            marker
            and isinstance(marker.value, dict)
            and marker.value.get("origin") == "verified_scoped_merge"
        ):
            if self._merged_account_valid(session, account, marker.value):
                return
            raise DomainError(
                "scoped_test_owner_denied", "Подтверждение объединённого аккаунта требует проверки.", 403
            )
        email_marker = bool(
            account
            and getattr(self.scope, "allow_email_signup", False)
            and account.email_verified
            and marker
            and marker.value
            == {
                "scope_id": self.scope.scope_id,
                "account_id": str(account.id),
                "email": account.email,
                "origin": "new_verified_email_account",
            }
        )
        telegram_marker = bool(
            account
            and str(account.telegram_id) == self.scope.telegram_id
            and marker
            and marker.value
            == {
                "scope_id": self.scope.scope_id,
                "account_id": str(account.id),
                "telegram_id": self.scope.telegram_id,
                "origin": "new_synthetic_account",
            }
        )
        if (
            not account
            or not (email_marker or telegram_marker)
            or account.legacy_consent
            or account.telegram_id is not None
            and str(account.telegram_id) != self.scope.telegram_id
            or account.email
            and not account.email.endswith("@example.test")
            and not getattr(self.scope, "allow_email_signup", False)
            or session.get(Alias, account.id)
            or session.scalar(select(Alias.old_id).where(Alias.canonical_id == account.id).limit(1))
        ):
            raise DomainError("scoped_test_owner_denied", "Тестовый владелец не подтверждён.", 403)

    @staticmethod
    def _json_hash(value):
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        ).hexdigest()

    def prepare_merge_in(self, session, source, target, proof_id):
        self.require_current(session)
        sources = []
        for account in (source, target):
            self.require_account_in(session, account)
            marker = session.get(Setting, "scoped_test_account:" + str(account.id), populate_existing=True)
            if marker.value.get("origin") not in {"new_synthetic_account", "new_verified_email_account"}:
                raise DomainError(
                    "scoped_test_merge_denied", "Это объединение требует отдельного подтверждения.", 403
                )
            sources.append(
                {
                    "account_id": str(account.id),
                    "marker": dict(marker.value),
                    "telegram_id": str(account.telegram_id) if account.telegram_id is not None else None,
                    "email": account.email,
                    "legacy_consent_sha256": self._json_hash(account.legacy_consent),
                }
            )
        return {
            "scope_id": self.scope.scope_id,
            "proof_id": proof_id,
            "canonical_id": str(source.id),
            "removed_id": str(target.id),
            "sources": sources,
        }

    def complete_merge_in(self, session, canonical, removed_id, proof_id, evidence, audit):
        self.require_current(session)
        if (
            evidence["canonical_id"] != str(canonical.id)
            or evidence["removed_id"] != str(removed_id)
            or evidence["proof_id"] != proof_id
        ):
            raise DomainError("scoped_test_merge_denied", "Подтверждение объединения изменилось.", 403)
        audit.details = audit.details | {
            "scoped_scope_id": self.scope.scope_id,
            "scoped_merge_proof_id": proof_id,
        }
        session.flush()
        marker = session.get(Setting, "scoped_test_account:" + str(canonical.id), with_for_update=True)
        marker.value = {
            "version": 1,
            "scope_id": self.scope.scope_id,
            "origin": "verified_scoped_merge",
            "account_id": str(canonical.id),
            "email": canonical.email,
            "telegram_id": str(canonical.telegram_id),
            "legacy_consent_sha256": self._json_hash(canonical.legacy_consent),
            "proof_id": proof_id,
            "audit_id": audit.id,
            "aliases": [str(removed_id)],
            "sources": evidence["sources"],
        }
        session.flush()
        self.require_account_in(session, canonical)

    def _merged_account_valid(self, session, account, value):
        try:
            if (
                not account
                or set(value)
                != {
                    "version",
                    "scope_id",
                    "origin",
                    "account_id",
                    "email",
                    "telegram_id",
                    "legacy_consent_sha256",
                    "proof_id",
                    "audit_id",
                    "aliases",
                    "sources",
                }
                or type(value["version"]) is not int
                or value["version"] != 1
                or value["scope_id"] != self.scope.scope_id
                or value["account_id"] != str(account.id)
                or value["email"] != account.email
                or not account.email_verified
                or value["telegram_id"] != str(account.telegram_id)
                or str(account.telegram_id) != self.scope.telegram_id
                or value["legacy_consent_sha256"] != self._json_hash(account.legacy_consent)
                or session.get(Alias, account.id)
            ):
                return False
            aliases = sorted(
                str(x) for x in session.scalars(select(Alias.old_id).where(Alias.canonical_id == account.id))
            )
            if len(aliases) != 1 or aliases != value["aliases"] or len(value["sources"]) != 2:
                return False
            audit = session.get(Audit, value["audit_id"])
            if (
                not audit
                or audit.action != "account_merge"
                or audit.actor_id != account.id
                or audit.target != aliases[0]
                or audit.details
                != {
                    "canonical_id": str(account.id),
                    "scoped_scope_id": self.scope.scope_id,
                    "scoped_merge_proof_id": value["proof_id"],
                }
            ):
                return False
            source_ids = set()
            for source in value["sources"]:
                if set(source) != {
                    "account_id",
                    "marker",
                    "telegram_id",
                    "email",
                    "legacy_consent_sha256",
                } or source["legacy_consent_sha256"] != self._json_hash({}):
                    return False
                identity = source["account_id"]
                source_ids.add(identity)
                marker = source["marker"]
                if marker == {
                    "scope_id": self.scope.scope_id,
                    "account_id": identity,
                    "origin": "new_verified_email_account",
                    "email": account.email,
                }:
                    if (
                        identity != str(account.id)
                        or source["email"] != account.email
                        or source["telegram_id"] is not None
                    ):
                        return False
                elif marker == {
                    "scope_id": self.scope.scope_id,
                    "account_id": identity,
                    "origin": "new_synthetic_account",
                    "telegram_id": self.scope.telegram_id,
                }:
                    original_marker = session.get(Setting, "scoped_test_account:" + identity)
                    if (
                        identity != aliases[0]
                        or source["telegram_id"] != self.scope.telegram_id
                        or source["email"] is not None
                        or not original_marker
                        or original_marker.value != marker
                    ):
                        return False
                else:
                    return False
            return source_ids == {str(account.id), aliases[0]}
        except (KeyError, TypeError, ValueError, AttributeError):
            return False

    def owns_original_in(self, session, account, original_id, *, telegram_id=None, check_telegram=False):
        self.require_account_in(session, account)
        marker = session.get(Setting, "scoped_test_account:" + str(account.id), populate_existing=True)
        if marker.value.get("origin") == "verified_scoped_merge":
            source = next((x for x in marker.value["sources"] if x["account_id"] == original_id), None)
            return bool(source and (not check_telegram or source["telegram_id"] == telegram_id))
        return original_id == str(account.id) and (
            not check_telegram
            or telegram_id == (str(account.telegram_id) if account.telegram_id is not None else None)
        )

    def enroll_verified_email_in(self, session, account):
        self.require_current(session, "auth_mail")
        key = "scoped_test_account:" + str(account.id)
        if (
            not getattr(self.scope, "allow_email_signup", False)
            or not account.email
            or not account.email_verified
            or account.telegram_id is not None
            or account.legacy_consent
            or session.get(Alias, account.id)
            or session.scalar(select(Alias.old_id).where(Alias.canonical_id == account.id).limit(1))
            or session.get(Setting, key)
        ):
            raise DomainError("scoped_test_owner_denied", "Тестовый владелец не подтверждён.", 403)
        session.add(
            Setting(
                key=key,
                value={
                    "scope_id": self.scope.scope_id,
                    "account_id": str(account.id),
                    "email": account.email,
                    "origin": "new_verified_email_account",
                },
            )
        )

    def require_order_in(self, session, order, *, operation=None, provider_id=None):
        if (
            not order
            or order.environment != "scoped_live"
            or not isinstance(order.id, str)
            or not order.id.startswith(self.scope.order_prefix)
        ):
            raise DomainError("scoped_test_order_denied", "Тестовый заказ не подтверждён.", 403)
        self.require_account_in(session, session.get(Account, order.account_id))
        if operation is None:
            operation = session.scalar(
                select(ProviderOperation).where(ProviderOperation.order_id == order.id)
            )
        request = operation.request if operation and isinstance(operation.request, dict) else {}
        try:
            valid = bool(
                operation
                and operation.order_id == order.id
                and operation.terminal == self.scope.rollypay.terminal
                and request.get("scope_id") == self.scope.scope_id
                and request.get("environment") == "scoped_live"
                and request.get("provider") == "rollypay"
                and request.get("terminal") == self.scope.rollypay.terminal
                and request.get("order_id") == order.id
                and self.owns_original_in(
                    session, session.get(Account, order.account_id), request.get("account_id")
                )
                and request.get("currency") == order.currency
                and Decimal(request["amount"]) == order.amount
                and (provider_id is None or operation.provider_id == provider_id)
            )
        except (KeyError, ValueError, TypeError, ArithmeticError):
            valid = False
        if not valid:
            raise DomainError("scoped_test_order_denied", "Тестовый заказ не подтверждён.", 403)

    def authorize_invoice(self, order_id, account_id, amount, currency, payment_method=None):
        with self._database.transaction() as session:
            self.require_current(session, "invoice_create")
            order = session.get(Order, order_id)
            operation = session.scalar(
                select(ProviderOperation).where(ProviderOperation.order_id == order_id)
            )
            self.require_order_in(session, order, operation=operation)
            if (
                operation.state != "in_flight"
                or operation.provider_id is not None
                or operation.request.get("payment_method") != payment_method
                or payment_method not in {"sbp", "crypto", "xrocket"}
                or order.status != "awaiting_payment"
                or not self.owns_original_in(session, session.get(Account, order.account_id), str(account_id))
                or order.amount != Decimal(str(amount))
                or order.currency != currency
            ):
                raise DomainError("scoped_test_order_denied", "Тестовый заказ не подтверждён.", 403)

    def authorize_payment_read(self, payment_id):
        with self._database.transaction() as session:
            self.require_current(session, "payment_reconcile")
            operation = session.scalar(
                select(ProviderOperation).where(
                    or_(
                        ProviderOperation.provider_id == payment_id,
                        ProviderOperation.provider_id.is_(None)
                        & (
                            ProviderOperation.request["scoped_callback_candidate"]["payment_id"].as_string()
                            == payment_id
                        ),
                    )
                )
            )
            self.require_event_order_in(
                session,
                session.get(Order, operation.order_id) if operation else None,
                operation=operation,
                provider_id=payment_id,
            )

    def require_event_order_in(self, session, order, *, operation, provider_id):
        self.require_order_in(session, order, operation=operation)
        if operation.provider_id == provider_id:
            return
        candidate = operation.request.get("scoped_callback_candidate", {})
        if (
            operation.provider_id is not None
            or operation.state not in {"in_flight", "outcome_unknown"}
            or candidate.get("scope_id") != self.scope.scope_id
            or candidate.get("order_id") != order.id
            or candidate.get("payment_id") != provider_id
            or not re.fullmatch(r"[a-f0-9]{64}", candidate.get("body_sha256", ""))
        ):
            raise DomainError("scoped_test_order_denied", "Тестовый заказ не подтверждён.", 403)

    def record_callback_candidate_in(self, session, order, operation, provider_id, body_sha256):
        self.require_current(session)
        if operation is None:
            raise DomainError("scoped_test_order_denied", "Тестовый заказ не подтверждён.", 403)
        self.require_order_in(session, order, operation=operation)
        if not isinstance(provider_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", provider_id):
            raise DomainError("scoped_test_order_denied", "Тестовый заказ не подтверждён.", 403)
        if operation.provider_id is not None:
            self.require_order_in(session, order, operation=operation, provider_id=provider_id)
            return
        prior = operation.request.get("scoped_callback_candidate")
        if (
            operation.state not in {"in_flight", "outcome_unknown"}
            or prior
            and prior.get("payment_id") != provider_id
        ):
            raise DomainError("scoped_test_order_denied", "Тестовый заказ не подтверждён.", 403)
        operation.request = operation.request | {
            "scoped_callback_candidate": {
                "scope_id": self.scope.scope_id,
                "order_id": order.id,
                "payment_id": provider_id,
                "body_sha256": body_sha256,
            }
        }

    def require_subscription_in(self, session, subscription):
        if not subscription or subscription.environment != "scoped_live":
            raise DomainError("scoped_test_subscription_denied", "Тестовая подписка не подтверждена.", 403)
        self.require_account_in(session, session.get(Account, subscription.account_id))
        record = session.get(Setting, "panel_test:" + subscription.id, populate_existing=True)
        value = record.value if record else {}
        intent = value.get("creation_intent", {})
        order = (
            session.get(Order, intent.get("order_id"))
            if intent.get("order_id")
            else session.scalar(
                select(Order)
                .join(Quote, Quote.id == Order.quote_id)
                .where(
                    Order.subscription_id == subscription.id,
                    Order.environment == "scoped_live",
                    Order.status == "paid",
                    Quote.snapshot["action"].as_string() == "purchase",
                )
                .limit(1)
            )
        )
        self.require_paid_service_in(session, order)
        if (
            not order
            or order.subscription_id != subscription.id
            or order.account_id != subscription.account_id
            or order.status != "paid"
            or order.fulfilled_at is None
        ):
            raise DomainError("scoped_test_subscription_denied", "Тестовая подписка не подтверждена.", 403)

        if subscription.panel_user_id is None and not record:
            return
        binding = value.get("binding", {})
        if (
            value.get("origin") != "new_test_identity"
            or value.get("environment") != "scoped_live"
            or value.get("contract") != "3.4_id"
            or value.get("panel_host") != urlparse(self.scope.panel.origin).hostname
            or not self.owns_original_in(
                session,
                session.get(Account, subscription.account_id),
                value.get("account_id"),
                telegram_id=value.get("telegram_id"),
                check_telegram=True,
            )
            or value.get("subscription_id") != subscription.id
            or intent.get("subscription_id") != subscription.id
            or not self.owns_original_in(
                session, session.get(Account, subscription.account_id), intent.get("owner_id")
            )
            or intent.get("environment") != "scoped_live"
            or intent.get("origin_nonce") != binding.get("origin_nonce")
            or str(subscription.panel_user_id) != binding.get("user_id")
            or subscription.short_uuid != binding.get("short_uuid")
            or subscription.connection_url != value.get("connection_url")
        ):
            raise DomainError("scoped_test_subscription_denied", "Тестовая подписка не подтверждена.", 403)

    def require_paid_service_in(self, session, order):
        """Нулевой внешний платёж требует собственной сохранённой выдачи и источника прав."""
        if not order or not isinstance(order.id, str) or not order.id.startswith(self.scope.order_prefix):
            raise DomainError("scoped_test_order_denied", "Тестовый заказ не подтверждён.", 403)
        if order is None or order.amount > 0:
            self.require_order_in(session, order)
            return
        self.require_account_in(session, session.get(Account, order.account_id))
        quote = session.get(Quote, order.quote_id)
        job = session.scalar(select(Job).where(Job.key == "provision:" + order.id, Job.kind == "provision"))
        snapshot = quote.snapshot if quote and isinstance(quote.snapshot, dict) else {}
        try:
            bonus, credit = Decimal(snapshot["bonus_amount"]), Decimal(snapshot.get("credit_amount", "0"))
            covered = bonus + credit
            valid = bool(
                order.environment == "scoped_live"
                and order.status == "paid"
                and order.fulfilled_at is not None
                and order.amount == 0
                and quote.account_id == order.account_id
                and Decimal(snapshot["total"]) == 0
                and job
                and job.account_id == order.account_id
                and job.payload.get("order_id") == order.id
                and job.payload.get("subscription_id") == order.subscription_id
                and job.payload.get("environment") == "scoped_live"
            )
            if covered > 0 and covered == Decimal(snapshot["base_amount"]) - Decimal(
                snapshot["discount_amount"]
            ):
                bonus_spend = (
                    session.scalar(select(Ledger).where(Ledger.operation == "spend:" + order.id))
                    if bonus
                    else None
                )
                credit_spend = (
                    session.scalar(select(CreditLedger).where(CreditLedger.operation == "spend:" + order.id))
                    if credit
                    else None
                )
                valid = valid and (
                    not bonus
                    or bonus_spend
                    and bonus_spend.account_id == order.account_id
                    and bonus_spend.amount == -bonus
                )
                valid = valid and (
                    not credit
                    or credit_spend
                    and credit_spend.account_id == order.account_id
                    and credit_spend.amount == -credit
                    and credit_spend.currency == order.currency
                    and credit_spend.provenance == "order_redemption"
                )
            elif (
                snapshot.get("action") == "downgrade"
                and snapshot.get("subscription_id") == order.subscription_id
            ):
                segment = session.get(Segment, snapshot.get("basis", {}).get("segment_id"))
                source = (
                    session.get(Order, segment.order_id)
                    if segment and segment.subscription_id == order.subscription_id
                    else None
                )
                valid = (
                    valid
                    and source is not None
                    and source.id != order.id
                    and source.status == "paid"
                    and source.fulfilled_at is not None
                    and source.account_id == order.account_id
                )
                if valid:
                    if source.amount > 0:
                        self.require_order_in(session, source)
                    else:
                        original_quote = session.get(Quote, source.quote_id)
                        # A zero-funded purchase ends the chain at its wallet proof; downgrade cannot recurse.
                        valid = bool(
                            original_quote
                            and original_quote.account_id == order.account_id
                            and original_quote.snapshot.get("action") == "purchase"
                            and original_quote.snapshot.get("subscription_id") is None
                        )
                        if valid:
                            self.require_paid_service_in(session, source)
            else:
                valid = False
        except (KeyError, ValueError, TypeError, ArithmeticError, AttributeError):
            valid = False
        if not valid:
            raise DomainError("scoped_test_order_denied", "Источник оплаченных прав требует проверки.", 403)
