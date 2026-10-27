import hashlib
import json
import re
from copy import deepcopy
from datetime import timedelta
from decimal import ROUND_FLOOR
from decimal import Decimal as D
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import func, or_, select
from sqlalchemy.orm import object_session
from sunrise_core.devices import device_surcharge
from sunrise_core.errors import DomainError, missing
from sunrise_core.pricing import Price, calculate, downgrade_seconds, money, upgrade

from . import panel_entitlements
from .db import (
    Account,
    Agreement,
    Alias,
    Audit,
    CreditLedger,
    Device,
    Job,
    Ledger,
    LegacyPurchase,
    Order,
    PanelObservation,
    Payment,
    Product,
    PromoClaim,
    PromoUse,
    ProviderOperation,
    Quote,
    Refund,
    Segment,
    Setting,
    Subscription,
    now,
    uid,
)
from .providers import checkout_url as verified_checkout_url
from .providers import sandbox_checkout_url

_USERNAME_UNSET = object()


def subscription_minimum(price, minimum):
    if not minimum.is_finite() or minimum <= 0 or price.discounted < 0 or price.bonus < 0:
        raise DomainError("payment_policy_invalid", "Условия оплаты требуют проверки.", 409)
    forced = price.external < minimum
    if not forced:
        return price, False
    discounted = max(price.discounted, minimum)
    bonus = min(price.bonus, max(D(0), discounted - minimum))
    return Price(price.base, discounted, bonus, money(discounted - bonus)), True


def validate_payment_methods(value):
    try:
        if (
            not isinstance(value, dict)
            or set(value) != {"version", "methods", "minimum_subscription_amount"}
            or type(value["version"]) is not int
            or value["version"] < 1
        ):
            raise ValueError
        if (
            not isinstance(value["minimum_subscription_amount"], str)
            or not isinstance(value["methods"], list)
            or not 1 <= len(value["methods"]) <= 3
        ):
            raise ValueError
        minimum = D(value["minimum_subscription_amount"])
        if not minimum.is_finite() or minimum <= 0 or minimum != money(minimum):
            raise ValueError
        seen = set()
        for item in value["methods"]:
            if not isinstance(item, dict) or set(item) != {
                "id",
                "label",
                "provider",
                "provider_method",
                "discount_percent",
                "enabled",
                "currencies",
                "mapping_contract",
            }:
                raise ValueError
            if (
                item["id"] not in {"crypto", "xrocket", "sbp"}
                or item["id"] in seen
                or item["provider"] != "rollypay"
                or item["provider_method"] != item["id"]
                or type(item["enabled"]) is not bool
            ):
                raise ValueError
            if (
                not isinstance(item["label"], str)
                or not 1 <= len(item["label"]) <= 80
                or any(ord(c) < 32 for c in item["label"])
            ):
                raise ValueError
            if (
                not item["mapping_contract"]
                or not isinstance(item["currencies"], list)
                or not item["currencies"]
                or any(c not in {"RUB", "EUR"} for c in item["currencies"])
            ):
                raise ValueError
            if not isinstance(item["discount_percent"], str):
                raise ValueError
            percent = D(item["discount_percent"])
            if not percent.is_finite() or not D(0) <= percent <= D(100):
                raise ValueError
            seen.add(item["id"])
        return value
    except (ValueError, TypeError, KeyError, ArithmeticError):
        raise DomainError(
            "payment_policy_invalid", "Способы оплаты требуют настройки владельцем.", 409
        ) from None


def payment_methods_hash(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def iso(value):
    return value.isoformat() if value else None


def successful_payment_history_clause():
    return Payment.status.in_(("paid", "partially_refunded", "refunded"))


def successful_order_history_clause():
    return or_(
        Order.status == "paid",
        select(Payment.id)
        .where(
            Payment.order_id == Order.id,
            Payment.account_id == Order.account_id,
            successful_payment_history_clause(),
        )
        .exists(),
    )


def deletion_notice_ack_key(sid):
    return "archive_dismiss:" + hashlib.sha256(sid.encode()).hexdigest()


def deletion_notice_dismissed_in(session, sub, archive):
    row = session.get(Setting, deletion_notice_ack_key(sub.id))
    value = row.value if row and isinstance(row.value, dict) else {}
    return value.get("subscription_id") == sub.id and value.get("proof_id") == archive.proof_id


def account_dto(a):
    return {
        "id": str(a.id),
        "email": a.email,
        "telegram_id": str(a.telegram_id) if a.telegram_id else None,
        "telegram_username": a.telegram_username,
        "unlink_blocked": a.unlink_blocked,
        "email_verified": a.email_verified,
    }


def credit_dto(account):
    available = max(D(0), account.credit_balance - account.credit_reserved).quantize(
        D("0.01"), rounding=ROUND_FLOOR
    )
    return {
        "credit_balance": str(account.credit_balance),
        "credit_reserved": str(account.credit_reserved),
        "credit_available": str(available) if account.credit_currency else "0.00",
        "credit_currency": account.credit_currency,
        "credit_provenance": account.credit_provenance,
    }


def product_dto(p, periods=()):
    from .catalog import presentation_dto

    return (
        {
            k: getattr(p, k)
            for k in (
                "id",
                "code",
                "name",
                "description",
                "tier_level",
                "currency",
                "included_devices",
                "version",
                "available",
            )
        }
        | {
            "price": str(p.price),
            "extra_device_price": str(p.extra_device_price) if p.extra_device_price is not None else None,
            "content": p.content,
        }
        | presentation_dto(p, periods)
    )


def subscription_display_name(product, fallback):
    from sunrise_core.content import validate_content
    from sunrise_core.telegram_content import render_plain, telegram_html_to_content

    if product is None:
        return fallback
    content = product.content if isinstance(product.content, dict) else {}
    try:
        title = render_plain(validate_content(content.get("name")))
        if title:
            return title
    except (ValueError, TypeError, AttributeError):
        pass
    return render_plain(telegram_html_to_content(product.name)) or fallback


def subscription_public_url(sub, panel, origin, allowed_hosts):
    """Rebase only the displayed URL; the verified provider URL and identity stay pinned."""
    if not origin:
        return sub.connection_url
    from .instance_config import InstanceConfig

    try:
        origin = InstanceConfig.subscription_origin(origin)
        if (
            not panel
            or not panel._safe_connection_url(sub.connection_url)
            or "\\" in sub.connection_url
            or not isinstance(sub.short_uuid, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", sub.short_uuid)
        ):
            return None
        original, target = urlsplit(sub.connection_url), urlsplit(origin)
        if original.hostname not in allowed_hosts or original.path.rsplit("/", 1)[-1] != sub.short_uuid:
            return None
        return urlunsplit((target.scheme, target.netloc, original.path, "", ""))
    except (ValueError, TypeError):
        return None


class Commerce:
    def __init__(
        self,
        db,
        local_test=False,
        accounts=None,
        notification_dir=None,
        notification_allowlist=None,
        panel_provider=None,
        mail_transport=None,
        device_bindings=None,
        referral_bot_url=None,
        telegram_notification_allowlist=None,
        sandbox_payments=False,
        owner_id=None,
        public_settings=None,
        connection_provider=None,
        runtime_policy=None,
    ):
        self.db, self.local_test, self.accounts = db, local_test, accounts
        self.sandbox_payments = sandbox_payments
        self.notification_dir = notification_dir
        self.notification_allowlist = set(notification_allowlist or [])
        self.panel_provider = panel_provider
        self.mail_transport = mail_transport
        self.device_bindings = dict(device_bindings or {})
        self.referral_bot_url = referral_bot_url
        self.telegram_notification_allowlist = frozenset(telegram_notification_allowlist or [])
        self.owner_id = str(owner_id) if owner_id else None
        self.public_settings = public_settings
        from .settings import runtime_policy as configured_policy

        self.runtime_policy = runtime_policy or (
            configured_policy(public_settings) if public_settings else None
        )
        if self.runtime_policy and self.runtime_policy.production:
            self.runtime_policy.require()
            if local_test:
                raise ValueError("production_test_transport_denied")
        self.connection_provider = connection_provider
        if connection_provider is None and panel_provider is not None and public_settings is not None:
            from .subscription_instructions import SubscriptionInstructions

            self.connection_provider = SubscriptionInstructions(
                public_settings.panel_subscription_allowed_hosts
            )

    @property
    def legal(self):
        from .legal import Legal

        return Legal(self)

    @property
    def live(self):
        return bool(self.runtime_policy and self.runtime_policy.production)

    @property
    def scoped_payments(self):
        return bool(self.runtime_policy and getattr(self.runtime_policy, "scoped_payments", False))

    def _payment_methods_in(self, session):
        value = self._setting(session, "payment_methods", None)
        if value is None:
            return []
        validate_payment_methods(value)
        available = not (self.live or self.scoped_payments)
        if self.live or self.scoped_payments:
            try:
                self.runtime_policy.require_current(session, "invoice_create")
                available = True
            except DomainError:
                available = False
        return [
            {
                "id": item["id"],
                "label": item["label"],
                "discount_percent": item["discount_percent"],
                "available": item["enabled"] and available,
                "unavailable_reason": None
                if item["enabled"] and available
                else "Способ оплаты временно недоступен.",
            }
            for item in value["methods"]
        ]

    def _payment_method_in(self, session, method, currency):
        if method is None:
            return None, None
        value = validate_payment_methods(self._setting(session, "payment_methods", {}))
        item = next((x for x in value["methods"] if x["id"] == method), None)
        if item is None or not item["enabled"] or currency not in item["currencies"]:
            raise DomainError("payment_method_unavailable", "Выберите доступный способ оплаты.", 409)
        if self.live or self.scoped_payments:
            self.runtime_policy.require_current(session, "invoice_create")
        dto = {
            "id": item["id"],
            "label": item["label"],
            "discount_percent": item["discount_percent"],
            "available": True,
            "unavailable_reason": None,
        }
        return dto, {
            "config_hash": payment_methods_hash(value),
            "version": value["version"],
            "provider": item["provider"],
            "provider_method": item["provider_method"],
            "mapping_contract": item["mapping_contract"],
            "minimum_subscription_amount": value["minimum_subscription_amount"],
        }

    def _lock_accounts_in(self, session, identifiers):
        identifiers = {int(identifier) for identifier in identifiers if identifier is not None}
        for _ in range(3):
            with session.begin_nested() as attempt:
                canonical = {
                    identifier: self._account(session, identifier, customer=False).id
                    for identifier in identifiers
                }
                locked = list(
                    session.scalars(
                        select(Account)
                        .where(Account.id.in_(set(canonical.values())))
                        .order_by(Account.id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                )
                current = {
                    identifier: self._account(session, identifier, customer=False).id
                    for identifier in identifiers
                }
                if len(locked) == len(set(canonical.values())) and current == canonical:
                    return canonical
                # A committed merge can remove a hinted account while this lock waits.
                # Release every lock from this attempt before sorting the new canonical set.
                attempt.rollback()
        raise missing()

    def legal_status(self, account_id):
        return self.legal.status(account_id)

    def accept_legal(self, account_id, documents, source, context):
        return self.legal.accept(account_id, documents, source, context)

    def resume_telegram_notifications(self, account_id, telegram_id):
        from .notifications import NotificationService

        return NotificationService(self.db, self.public_settings).resume_telegram(account_id, telegram_id)

    @property
    def promos(self):
        from .promos import Promos

        return Promos(self)

    def activate_promo(self, account_id, code):
        return self.promos.activate(account_id, code)

    def active_promo(self, account_id):
        return self.promos.active(account_id)

    def claim_promo(self, account_id, code, subscription_id=None):
        return self.promos.claim(account_id, code, subscription_id)

    def promo_claim_targets(self, account_id):
        return self.promos.targets(account_id)

    def account_info(self, account_id):
        with self.db.transaction() as s:
            return account_dto(self._account(s, account_id))

    def referral_info(self, account_id):
        with self.db.transaction() as s:
            return self._referral_info(s, self._account(s, account_id))

    def _referral_info(self, s, account):
        import re
        from urllib.parse import urlsplit

        referral_url = None
        try:
            parsed = urlsplit(self.referral_bot_url or "")
            if (
                parsed.scheme == "https"
                and parsed.netloc == "t.me"
                and not parsed.query
                and not parsed.fragment
                and re.fullmatch(r"/[A-Za-z][A-Za-z0-9_]{4,31}", parsed.path)
            ):
                referral_url = "https://t.me" + parsed.path + "?start=ref_" + str(account.id)
        except ValueError:
            pass
        from .referral_attribution import website_referral_url
        from .referrals import earned_total

        return {
            "referral_code": "ref_" + str(account.id),
            "referral_url": referral_url,
            "website_referral_url": website_referral_url(
                account.id, self.public_settings, local_test=self.local_test and not self.scoped_payments
            ),
            "reward_percent": self._setting(s, "discounts", {}).get("referral_percent", "0"),
            "invited_count": s.scalar(
                select(func.count()).select_from(Account).where(Account.referrer_id == account.id)
            ),
            "rewarded_amount": str(earned_total(s, account.id)),
            "currency": "RUB",
            "enrollment_available": "referral_percent" in self._setting(s, "discounts", {}),
        }

    @property
    def retention(self):
        from .retention import RetentionService

        return RetentionService(self, self.public_settings)

    @property
    def provisioning(self):
        from .provisioning import Provisioning

        return Provisioning(self.db, self.panel_provider, self)

    def _provision_payload(self, s, sub, mappings=None, moment=None, prior_status=None, origin=None):
        moment = moment or now()
        active = panel_entitlements.current_segment(s, sub, moment)
        mapping_segment = active or s.scalar(
            select(Segment)
            .where(Segment.subscription_id == sub.id, Segment.starts_at <= moment, Segment.kind != "refunded")
            .order_by(Segment.starts_at.desc())
            .limit(1)
        )
        paid_basis = mapping_segment.price_basis if mapping_segment else {}
        if mappings is None:
            mappings = paid_basis.get("panel_mappings", {})
        mapping = (mappings or {}).get(sub.product_id)
        restriction = s.get(Setting, "restriction:" + sub.id)
        marker = panel_entitlements.override(s, sub, moment)
        observed = panel_entitlements.verified_observation(s, sub)
        if marker:
            observed_squads = marker.get("internal_squad_uuids", [])
            current_mapping = panel_entitlements.mapped_product(s, observed_squads)
            mapping = (
                observed_squads[0]
                if len(observed_squads) == 1
                and current_mapping.get("panel_product_id") == marker.get("panel_product_id")
                and current_mapping["panel_product_state"] == "matched"
                else None
            )
        from .panel_v3 import instant

        effective_expiry = (
            instant(marker["expires_at"])
            if marker
            else (panel_entitlements.effective_end(s, sub, active) if active else moment)
        )
        imported = panel_entitlements.imported_baseline(s, sub, moment) if self.live else None
        legacy_basis = (paid_basis.get("legacy_catalog") or {}) if self.live else {}
        legacy_binding = legacy_basis.get("binding") or {}
        current_binding = panel_entitlements.registered_binding(s, sub) if legacy_basis else None
        legacy_access = (
            active is not None and active.kind == "legacy_catalog"
            and legacy_basis.get("origin") == "legacy_catalog"
            and legacy_basis.get("subscription_id") == sub.id
            and legacy_basis.get("environment") == sub.environment
            and current_binding is not None
            # Approved credential rotation changes only short_uuid, never the owned identity.
            and {key: value for key, value in legacy_binding.items() if key != "short_uuid"}
            == {key: value for key, value in current_binding.items() if key != "short_uuid"}
        )
        if imported or legacy_access:
            effective_expiry = max(
                effective_expiry, panel_entitlements.paid_horizon(s, sub, moment) or effective_expiry
            )
        effective_access = effective_expiry > moment and (active is not None or marker is not None)
        local = prior_status if prior_status is not None else panel_entitlements.local_status(s, sub)
        prior_local_status = local
        own_target = panel_entitlements.local_origin_target(
            s,
            sub,
            mock_only=sub.environment == "local_test"
            and self.panel_provider is None
            and not panel_entitlements.bound(sub),
            observed=panel_entitlements.observed_target(s, sub),
        )
        refunded_segment = (
            s.get(Segment, own_target.get("refunded_segment_id"))
            if own_target and isinstance(own_target.get("refunded_segment_id"), str)
            else None
        )
        if (
            effective_access
            and active
            and active.kind == "paid"
            and active.order_id
            and local in {"revoked", "expired"}
            and own_target
            and own_target.get("target_origin") in {"refund", "refund_review", "gap"}
            and (
                own_target.get("target_origin") == "gap"
                or refunded_segment
                and refunded_segment.subscription_id == sub.id
                and refunded_segment.kind == "refunded"
            )
            and own_target.get("prior_local_status") in {"active", "expired"}
            and own_target.get("active_segment_id") is None
            and own_target.get("desired_subscription_status") in {"revoked", "expired"}
            and own_target.get("issued_at")
            and (
                active.id in own_target.get("future_paid_segment_ids", [])
                or active.starts_at > instant(own_target["issued_at"])
            )
            and not (restriction and restriction.value.get("suspended"))
            and not self._setting(s, "dispute:" + sub.id, {}).get("enabled")
        ):
            local = "active"
        desired = (
            "active"
            if effective_access and local in {"active", "expired"}
            else ("expired" if not effective_access and local in {"active", "expired"} else local)
        )
        capacity = marker["devices_limit"] if marker else sub.devices_limit
        null_evidence = panel_entitlements.imported_null_capacity(s, sub, moment) if self.live else None
        if (
            not effective_access
            and not marker
            and observed is None
            and not (
                sub.environment == "local_test"
                and self.panel_provider is None
                and not panel_entitlements.bound(sub)
            )
        ):
            mapping, capacity = None, None
        if not effective_access and not marker and observed:
            current = observed.payload
            squads = current.get("internal_squad_uuids")
            capacity = current.get("devices_limit")
            last_target = panel_entitlements.applied_target(s, sub)
            product_fact = panel_entitlements.mapped_product(s, squads) if isinstance(squads, list) else {}
            mapping = (
                squads[0]
                if observed.observed_at > moment - timedelta(minutes=10)
                and observed.status in {"ACTIVE", "DISABLED", "LIMITED", "EXPIRED"}
                and isinstance(squads, list)
                and len(squads) == 1
                and product_fact.get("panel_product_state") == "matched"
                and product_fact.get("panel_product_id") == current.get("panel_product_id")
                and type(capacity) is int
                and 1 <= capacity <= 100
                and (not last_target or last_target["version"] <= current["subscription_version"])
                else None
            )
        return {
            "subscription_id": sub.id,
            "expires_at": iso(effective_expiry),
            "active_segment_id": active.id if active else None,
            "devices_limit": capacity,
            **(
                {"preserve_null_capacity": True, "capacity_evidence": null_evidence}
                if capacity is None and null_evidence
                else {}
            ),
            "product_id": sub.product_id,
            "version": sub.version,
            "environment": sub.environment,
            **({"scope_id": self.runtime_policy.scope.scope_id} if self.live else {}),
            "internal_squad_uuid": mapping,
            "panel_object_type": "internal_squad",
            **(
                {"origin_binding": panel_entitlements.registered_binding(s, sub)}
                if panel_entitlements.registered_binding(s, sub)
                else {}
            ),
            "issued_at": moment.isoformat(),
            "target_origin": origin,
            "prior_local_status": prior_local_status,
            "future_paid_segment_ids": [
                item.id
                for item in panel_entitlements.segments(s, sub)
                if item.kind == "paid" and item.starts_at > moment
            ],
            "desired_subscription_status": desired,
            "external_squad_uuid": paid_basis.get("panel_configuration", {}).get("external_squad_uuid"),
            "project_prefix": paid_basis.get("panel_configuration", {}).get("project_prefix"),
            "panel_status": "DISABLED"
            if not effective_access
            or panel_entitlements.remote_status(s, sub)
            or desired != "active"
            or (restriction and restriction.value.get("suspended"))
            else "ACTIVE",
        }

    def confirm_telegram_link(self, account_id, proof):
        if self.accounts is None:
            raise DomainError("link_unavailable", "Привязка временно недоступна.", 503)
        return self.accounts.confirm_telegram_link(account_id, proof)

    def quote_with_max_bonus(self, account_id, **kwargs):
        with self.db.transaction() as s:
            a = self._account(s, account_id)
            bonus = max(D(0), a.bonus_balance - a.bonus_reserved).quantize(D("0.01"), rounding=ROUND_FLOOR)
        return self.quote(account_id, **(kwargs | {"bonus_amount": str(bonus)}))

    def _refresh_panel(self, account_id, subscription_id=None):
        # Authorize locally, release the transaction, then read only registered own panel identities.
        with self.db.transaction() as s:
            account = self._account(s, account_id)
            if subscription_id and self.scoped_payments:
                selected = s.scalar(
                    select(Subscription).where(
                        Subscription.id == subscription_id, Subscription.account_id == account.id
                    )
                )
                if selected is None:
                    raise missing()
                # Old fake-panel cards remain readable; they have no authority for real panel reads.
                owned = (
                    []
                    if selected.environment in {"local_test", "sandbox"}
                    else [self._sub(s, account.id, subscription_id)]
                )
            elif subscription_id:
                owned = [self._sub(s, account.id, subscription_id)]
            else:
                owned = list(s.scalars(select(Subscription).where(Subscription.account_id == account.id)))
            from .retention import fenced_in

            ids = [
                sub.id
                for sub in owned
                if (not self.scoped_payments or sub.environment == "scoped_live")
                and panel_entitlements.bound(sub)
                and not fenced_in(s, sub)
            ]
        if not ids:
            return {}
        if self.panel_provider is None:
            return {sid: {"status": "error", "error": "panel_unavailable"} for sid in ids}
        from types import SimpleNamespace

        from .panel_events import PanelEvents

        settings = (
            self.public_settings
            or getattr(self.accounts, "settings", None)
            or SimpleNamespace(local_test=self.local_test)
        )
        events = PanelEvents(self.db, self.panel_provider, settings)
        return {sid: events.reconcile_owned(sid) for sid in ids}

    def _refresh_for_price(self, account_id, subscription_id):
        with self.db.transaction() as session:
            sub = self._sub(session, account_id, subscription_id)
            self._assert_subscription_operations(session, sub)
        results = self._refresh_panel(account_id, subscription_id)
        if any(result["status"] != "ok" for result in results.values()):
            raise DomainError(
                "panel_sync_required",
                "Не удалось проверить данные панели. Обновите профиль перед расчётом цены.",
                409,
            )

    def quote_with_max_credit(self, account_id, **kwargs):
        with self.db.transaction() as s:
            account = self._account(s, account_id)
            credit = credit_dto(account)["credit_available"]
            if account.credit_balance > 0 and account.credit_currency is None:
                raise DomainError(
                    "credit_currency_unverified", "Валюта денежного остатка требует проверки.", 409
                )
        return self.quote(account_id, **(kwargs | {"credit_amount": credit}))

    def _account(self, s, account_id, lock=False, *, customer=True):
        aid = int(account_id)
        alias = s.get(Alias, aid, populate_existing=True)
        if alias:
            aid = alias.canonical_id
        query = select(Account).where(Account.id == aid)
        if lock:
            query = query.with_for_update()
        a = s.scalar(query)
        if not a:
            # A merge may have committed while this row lock waited; resolve its new alias in READ COMMITTED.
            alias = s.get(Alias, aid, populate_existing=True)
            if alias:
                retry = select(Account).where(Account.id == alias.canonical_id)
                if lock:
                    retry = retry.with_for_update()
                a = s.scalar(retry.execution_options(populate_existing=True))
        if not a:
            raise missing()
        if customer and self.scoped_payments:
            self.runtime_policy.require_account_in(s, a)
        if customer and self.live:
            from .accounts import require_account_access_in

            require_account_access_in(s, a, self.runtime_policy)
        return a

    def _sub(self, s, account_id, sid, lock=False):
        from .subscription_archive import is_retired_local_fixture

        q = select(Subscription).where(Subscription.id == sid, Subscription.account_id == int(account_id))
        if lock:
            q = q.with_for_update()
        sub = s.scalar(q)
        if not sub or is_retired_local_fixture(sub):
            raise missing()
        if self.scoped_payments:
            self.runtime_policy.require_current(s)
            self.runtime_policy.require_subscription_in(s, sub)
        return sub

    def resolve_telegram(self, telegram_id, referral_code=None, *, username=_USERNAME_UNSET):
        if not isinstance(telegram_id, int) or telegram_id <= 0:
            raise DomainError("identity_invalid", "Не удалось подтвердить Telegram.", 400)
        with self.db.transaction() as s:
            # Transaction-scoped lock prevents duplicate first-login rows.
            from sqlalchemy import text

            s.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": telegram_id})
            a = s.scalar(select(Account).where(Account.telegram_id == telegram_id))
            if not a:
                a = Account(telegram_id=telegram_id)
                from .referral_attribution import assign_new_referrer

                s.add(a)
                assign_new_referrer(s, a, referral_code, allow_legacy_alias=True)
                s.flush()
            if self.live:
                from .accounts import require_account_access_in

                require_account_access_in(s, a, self.runtime_policy)
            if username is not _USERNAME_UNSET:
                import re

                if username is not None and (
                    not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username)
                ):
                    raise DomainError("identity_invalid", "Проверьте имя Telegram.", 400)
                a.telegram_username = username
            return account_dto(a)

    def _setting(self, s, key, fallback):
        row = s.get(Setting, key)
        return row.value if row else fallback

    def _welcome_eligible(self, s, account, accepted_order=None):
        if not account.referrer_id or account.referrer_id == account.id:
            return False
        history = select(Payment.id).where(Payment.account_id == account.id)
        if accepted_order:
            history = history.where(Payment.order_id != accepted_order.id)
        if s.scalar(history.limit(1)) or s.scalar(
            select(LegacyPurchase.id).where(LegacyPurchase.account_id == account.id).limit(1)
        ):
            return False
        fulfilled = select(Order.id).where(
            Order.account_id == account.id, Order.status.in_(["paid", "refund_review"])
        )
        if accepted_order:
            fulfilled = fulfilled.where(Order.id != accepted_order.id)
        if s.scalar(fulfilled.limit(1)):
            return False
        rows = s.execute(
            select(Order, Quote)
            .join(Quote, Quote.id == Order.quote_id)
            .where(
                Order.account_id == account.id,
                Order.status == "awaiting_payment",
                Quote.expires_at > now(),
            )
        ).all()
        for order, quote in rows:
            if (quote.snapshot.get("selected_secondary") or {}).get("source") != "referral_welcome":
                continue
            if accepted_order and order.id == accepted_order.id:
                continue
            if accepted_order and (order.created_at, order.id) > (
                accepted_order.created_at,
                accepted_order.id,
            ):
                continue
            return False
        return True

    def _assert_new_operations(self, s):
        if self.live:
            self.runtime_policy.require_current(s)
        # Общий транзакционный ключ линеаризует включение режима и принятие новой операции.
        s.execute(select(func.pg_advisory_xact_lock(617045501)))
        if self.live:
            self.runtime_policy.require_current(s)
        if self._setting(s, "maintenance", {}).get("enabled"):
            raise DomainError(
                "maintenance",
                "Новые покупки и изменения подписок временно приостановлены на обслуживание.",
                503,
                True,
            )

    def _assert_subscription_operations(self, s, sub, *, read_existing=False):
        if sub:
            from .retention import assert_legacy_operation_allowed_in, assert_mutation_allowed_in

            if not read_existing:
                assert_legacy_operation_allowed_in(s, sub.account_id, sub.id)
            assert_mutation_allowed_in(s, sub, include_legacy=not read_existing)
        if sub and self._setting(s, "dispute:" + sub.id, {}).get("enabled"):
            raise DomainError(
                "subscription_disputed", "Операции по этой подписке ожидают проверки спорного платежа.", 409
            )

    def _help_center(self, session):
        from .service_content import help_center

        return help_center(
            self.public_settings,
            self._setting(session, "public_content", {}),
            self._setting(session, "discounts", {}),
            self._setting(session, "periods", []),
        )

    def catalog(self):
        from .catalog import period_dto

        with self.db.transaction() as s:
            periods = self._setting(s, "periods", [])
            return {
                "payment_methods": self._payment_methods_in(s),
                "products": sorted(
                    [product_dto(p, periods) for p in s.scalars(select(Product).where(Product.available))],
                    key=lambda p: (p["sort_order"], p["id"]),
                ),
                "periods": [period_dto(period) for period in periods],
                "content": {
                    "emoji_registry": self._setting(s, "emoji_registry", {}),
                    "bot_menu": self._setting(s, "content:bot_menu", {}).get("nodes", []),
                    "faq": self._setting(s, "public_content", {}).get("faq", []),
                    "how_to_connect": self._setting(s, "public_content", {}).get("how_to_connect", []),
                    "catalog_description": self._setting(s, "public_content", {}).get(
                        "catalog_description", ""
                    ),
                    "catalog_description_content": self._setting(s, "public_content", {}).get(
                        "catalog_description_content", []
                    ),
                    "telegram_theme": self._setting(s, "telegram_theme", {}),
                    "help_center": self._help_center(s),
                },
                "legal": self.legal.current(s),
                "recurring_enrollment_enabled": bool(self._setting(s, "recurring", {}).get("enabled", False)),
                "capabilities": {
                    "payments": (
                        "live"
                        if self.live
                        else "sandbox"
                        if self.sandbox_payments
                        else "local_test"
                        if self.local_test
                        else "unavailable"
                    ),
                    "recurring": False,
                    "dynamic_recurring_amount": False,
                    "provisioning": (
                        "panel"
                        if self.live and self.panel_provider is not None
                        else "test_panel"
                        if self.panel_provider is not None
                        else "local_test"
                        if self.local_test
                        else "unavailable"
                    ),
                },
            }

    def renewal_offer(self, account_id, sid, *, quote_id=None, payment_method=None, refresh=True):
        return self._renewal_offer(
            account_id, sid, quote_id=quote_id, refresh=refresh, payment_method=payment_method
        )

    def _renewal_offer(self, account_id, sid, *, quote_id=None, refresh=False, payment_method=None):
        # Ownership is checked before the targeted refresh; hints never allocate an order.
        with self.db.transaction() as session:
            account = self._account(session, account_id)
            self._sub(session, account.id, sid)
            saved = session.get(Quote, quote_id) if quote_id else None
            if quote_id and (
                saved is None
                or saved.account_id != account.id
                or saved.subscription_id != sid
                or saved.snapshot.get("action") != "renew"
            ):
                raise missing()
        if refresh:
            self._refresh_for_price(account_id, sid)
        with self.db.transaction() as session:
            account = self._account(session, account_id, True)
            sub = self._sub(session, account.id, sid, True)
            self._assert_new_operations(session)
            self.legal.require_current(session, account)
            self._assert_subscription_operations(session, sub)
            panel_entitlements.require_pricing(session, sub, finite_renewal=True)
            if session.scalar(
                select(Agreement.id)
                .where(Agreement.subscription_id == sid, Agreement.state != "stopped")
                .limit(1)
            ):
                raise DomainError(
                    "agreement_change_required", "Сначала подтвердите отмену автопродления.", 409
                )
            product = session.get(Product, sub.product_id)
            if product is None or not product.available:
                raise missing()
            periods = (product.presentation or {}).get("periods") or self._setting(session, "periods", [])
            if not periods:
                raise DomainError("period_invalid", "Доступный срок продления не настроен.", 409)
            days = next(
                (period["days"] for period in periods if D(str(period["months"])) == 1),
                min(period["days"] for period in periods),
            )
            saved = session.get(Quote, quote_id) if quote_id else None
            if saved is not None:
                if (
                    saved.account_id != account.id
                    or saved.subscription_id != sid
                    or saved.snapshot.get("action") != "renew"
                ):
                    raise missing()
                saved_days = saved.snapshot.get("period_days") or saved.snapshot.get("basis", {}).get(
                    "period_days"
                )
                if any(period["days"] == saved_days for period in periods):
                    days = saved_days
                if (
                    saved.expires_at > now()
                    and saved.snapshot.get("version") == sub.version
                    and saved.snapshot.get("product_version") == product.version
                    and saved.snapshot.get("product_id") == product.id
                    and saved_days == days
                    and (saved.snapshot.get("payment_method") or {}).get("id") == payment_method
                    and (
                        not payment_method
                        or (saved.snapshot.get("payment_method_policy") or {}).get("config_hash")
                        == payment_methods_hash(self._setting(session, "payment_methods", {}))
                    )
                ):
                    return self._quote_dto(saved) | {"period_days": days}
            product_id = product.id
        return self._quote_from_verified_observation(
            account_id,
            product_id,
            days,
            "renew",
            sid,
            payment_method=payment_method,
            method_preview=payment_method is None,
        ) | {"period_days": days}

    def content_registry(self):
        with self.db.transaction() as s:
            return self._setting(s, "emoji_registry", {})

    def node_news(self, account_id, *, before=None, limit=20):
        from .panel_news import node_news

        return node_news(self, account_id, before=before, limit=limit)

    def record_bot_capabilities(self, status):
        if not self.local_test:
            raise DomainError("environment_mismatch", "Отчёт разрешён только тестовому боту.", 403)
        if set(status) != {"rich_messages", "custom_emoji", "reason"} or not all(
            isinstance(status[k], bool) for k in ("rich_messages", "custom_emoji")
        ):
            raise ValueError("Некорректный отчёт возможностей")
        if not isinstance(status["reason"], str) or len(status["reason"]) > 300:
            raise ValueError("Некорректное описание возможностей")
        with self.db.transaction() as s:
            row = s.scalar(select(Setting).where(Setting.key == "bot_capabilities").with_for_update())
            value = status | {"observed_at": iso(now()), "environment": "local_test"}
            if row:
                row.value = value
            else:
                s.add(Setting(key="bot_capabilities", value=value))

    def quote(
        self,
        account_id,
        product_id,
        period_days,
        action,
        subscription_id=None,
        bonus_amount="0.00",
        extra_devices=0,
        promo_code=None,
        credit_amount="0.00",
        payment_method=None,
    ):
        if subscription_id and action in {"renew", "upgrade", "downgrade", "devices"}:
            self._refresh_for_price(account_id, subscription_id)
        return self._quote_from_verified_observation(
            account_id,
            product_id,
            period_days,
            action,
            subscription_id,
            bonus_amount,
            extra_devices,
            promo_code,
            credit_amount,
            payment_method,
        )

    def _quote_from_verified_observation(
        self,
        account_id,
        product_id,
        period_days,
        action,
        subscription_id=None,
        bonus_amount="0.00",
        extra_devices=0,
        promo_code=None,
        credit_amount="0.00",
        payment_method=None,
        *,
        method_preview=False,
    ):
        with self.db.transaction() as s:
            a = self._account(s, account_id, True)
            from .retention import assert_legacy_operation_allowed_in

            assert_legacy_operation_allowed_in(s, a.id, subscription_id)
            p = s.get(Product, product_id)
            if not p or not p.available:
                if subscription_id:
                    target_sub = self._sub(s, a.id, subscription_id, True)
                    if panel_entitlements.bound(target_sub):
                        panel_entitlements.require_pricing(s, target_sub)
                raise missing()
            if D(bonus_amount) > 0 and p.currency != "RUB":
                raise DomainError(
                    "bonus_currency_mismatch", "Бонусы в рублях нельзя применять к другой валюте.", 409
                )
            periods = (p.presentation or {}).get("periods") or self._setting(s, "periods", [])
            period = next((x for x in periods if x["days"] == period_days), None)
            if not period:
                raise DomainError("period_invalid", "Выберите доступный срок.", 400)
            sub = self._sub(s, a.id, subscription_id, True) if subscription_id else None
            if (action == "purchase") != (sub is None):
                raise DomainError("subscription_required", "Выберите подписку или добавьте новую.", 400)
            self._assert_new_operations(s)
            self.legal.require_current(s, a)
            eligibility = (p.presentation or {}).get("eligibility", {})
            if action in {"upgrade", "downgrade"} and not eligibility.get("allow_" + action, True):
                raise DomainError("tier_change_unavailable", "Этот переход временно недоступен.", 409)
            if sub:
                self._assert_subscription_operations(s, sub)
                if action == "renew":
                    panel_entitlements.require_pricing(s, sub, finite_renewal=True)
                    if panel_entitlements.bound(sub) and p.id != sub.product_id:
                        raise DomainError(
                            "panel_product_changed",
                            "Тариф изменён в панели. Обновите профиль и расчёт продления.",
                            409,
                        )
                elif action in {"upgrade", "downgrade", "devices"}:
                    panel_entitlements.require_valuation(s, sub)
                ag = s.scalar(
                    select(Agreement).where(Agreement.subscription_id == sub.id, Agreement.state != "stopped")
                )
                if ag and ag.state != "stopped":
                    raise DomainError(
                        "agreement_change_required",
                        "Сначала подтвердите отмену автопродления, чтобы исключить двойную оплату.",
                    )
            moment = now()
            start = max(moment, sub.expires_at) if sub and sub.expires_at else moment
            if sub and action == "renew" and panel_entitlements.bound(sub):
                current_expiry = panel_entitlements.facts(s, sub)["expires_at"]
                from .panel_v3 import instant

                horizon = panel_entitlements.paid_horizon(s, sub, moment)
                start = max(moment, instant(current_expiry), horizon or moment)
            end = start + timedelta(days=period_days)
            devices = max((sub.devices_limit or 0) if sub else 0, p.included_devices)
            if sub and action == "renew" and panel_entitlements.bound(sub):
                devices = panel_entitlements.facts(s, sub)["panel_device_limit"]
                if devices is None and (
                    panel_entitlements.imported_null_capacity(s, sub, current_only=False)
                    or panel_entitlements.facts(s, sub)["renewal_device_limit_required"]
                ):
                    devices = p.included_devices
            version = sub.version if sub else 0
            policy = self._setting(s, "discounts", {})
            method_dto, method_policy = self._payment_method_in(s, payment_method, p.currency)
            if not {"cap", "bonus_cap"} <= set(policy):
                raise DomainError(
                    "pricing_policy_unavailable", "Условия оплаты требуют настройки владельцем.", 503
                )
            secondary = [D(str(x)) for x in policy.get("secondary", [])]
            candidates = [{"source": "configured_secondary", "percent": str(x)} for x in secondary]
            near_expiry_evidence = None
            if action == "renew" and sub:
                from .recurring import Recurring

                loyalty = D(Recurring(self, None).loyalty(sub.id)["percent"])
                secondary.append(loyalty)
                candidates.append({"source": "loyalty", "percent": str(loyalty)})
                campaign = policy.get("renewal_campaign") or {}
                if campaign.get("enabled"):
                    secondary.append(D(campaign["percent"]))
                    candidates.append({"source": "renewal_campaign", "percent": campaign["percent"]})
                near_expiry = policy.get("near_expiry_renewal") or {}
                horizon = panel_entitlements.verified_service_horizon(s, sub, moment)
                if near_expiry.get("enabled") is True and horizon is not None:
                    near_expiry_evidence = {
                        "evaluated_at": iso(moment),
                        "effective_horizon": iso(horizon),
                        "percent": near_expiry["percent"],
                    }
                    if horizon - moment < timedelta(hours=24):
                        secondary.append(D(near_expiry["percent"]))
                        candidates.append(
                            {"source": "near_expiry_renewal", "percent": near_expiry["percent"]}
                        )
            if action == "purchase" and self._welcome_eligible(s, a):
                welcome = policy.get("referral_welcome") or {}
                if welcome.get("enabled"):
                    secondary.append(D(welcome["percent"]))
                    candidates.append({"source": "referral_welcome", "percent": welcome["percent"]})
            applied_promo = None
            if promo_code:
                if action not in {"purchase", "renew"}:
                    raise DomainError(
                        "promo_action_invalid", "Промокод доступен при покупке или продлении.", 400
                    )
                self.promos.activate_in_transaction(s, a, promo_code)
            pending = a.pending_promo
            secondary_without_promo = list(secondary)
            promo = None
            if pending and action in {"purchase", "renew"}:
                try:
                    promo = self._promo(s, a.id, pending["code"], action)
                except DomainError as exc:
                    if exc.code not in {"promo_invalid", "promo_audience", "promo_used"}:
                        raise
            base = p.price * D(str(period["months"]))
            from .catalog import product_device_surcharge, product_device_terms

            device_amount = product_device_surcharge(p, devices) * D(str(period["months"])) if sub else D(0)
            if promo and promo.get("reward_type", "percent") == "percent":
                percent = min(D(100), D(promo["percent"]))
            else:
                percent = None
            if percent is not None:
                candidates.append(
                    {
                        "source": "promo",
                        "percent": str(percent),
                        "reward_type": promo.get("reward_type", "percent"),
                    }
                )
                if percent > max(secondary, default=D(0)):
                    applied_promo = promo["code"]
                    secondary.append(percent)
            price = calculate(
                base,
                D(str(period["discount_percent"])),
                secondary,
                D(str(policy["cap"])),
                D(str(policy["bonus_cap"])),
                D(bonus_amount),
            )
            if (
                applied_promo
                and price.discounted
                >= calculate(
                    base,
                    D(str(period["discount_percent"])),
                    secondary_without_promo,
                    D(str(policy["cap"])),
                    D(str(policy["bonus_cap"])),
                    D(bonus_amount),
                ).discounted
            ):
                applied_promo = None
            basis = {
                "currency": p.currency,
                "period_days": period_days,
                "months": period["months"],
                "products": {},
                "segment_id": None,
                "device_pricing_included": True,
                "device_terms": {},
            }
            factor = price.discounted / base if base else D(1)
            device_factor = D(1)
            if applied_promo and promo.get("legacy_promo_grant"):
                # Source activation already claimed this whole-service percentage.
                # Preserve its device component only when it adds an actual benefit.
                device_factor = 1 - min(percent, D(str(policy["cap"]))) / 100
            if device_amount:
                paid_devices = money(device_amount * device_factor)
                if applied_promo and promo.get("legacy_promo_grant"):
                    # Component rounding must not exceed the whole-service cap.
                    paid_devices = max(
                        paid_devices,
                        money((price.base + device_amount) * (1 - D(str(policy["cap"])) / 100))
                        - price.discounted,
                    )
                    device_factor = paid_devices / device_amount
                combined = calculate(
                    price.discounted + paid_devices,
                    D(0),
                    [],
                    D(0),
                    D(str(policy["bonus_cap"])),
                    D(bonus_amount),
                )
                price = Price(
                    price.base + device_amount, combined.discounted, combined.bonus, combined.external
                )
            if promo and promo.get("reward_type") == "fixed_rub" and p.currency == "RUB":
                # A money reward competes with the actual ordinary price, rather
                # than an equivalent raw percentage subject to a different cap.
                period_only = calculate(
                    base,
                    D(str(period["discount_percent"])),
                    [],
                    D(str(policy["cap"])),
                    D(0),
                    D(0),
                )
                candidate_amount = money(
                    max(D(0), period_only.discounted + device_amount - D(promo["amount_rub"]))
                )
                method_percent = D(method_dto["discount_percent"]) if method_dto else D(0)
                candidate_final = money(candidate_amount * (1 - method_percent / 100))
                ordinary_final = money(
                    max(
                        price.discounted * (1 - method_percent / 100),
                        price.base * (1 - D(str(policy["cap"])) / 100),
                    )
                )
                configured_minimum = self._setting(s, "payment_methods", {})
                comparison_floor = (
                    D(validate_payment_methods(configured_minimum)["minimum_subscription_amount"])
                    if configured_minimum
                    else D(0)
                )
                if max(comparison_floor, candidate_final) < max(comparison_floor, ordinary_final):
                    applied_promo = promo["code"]
                    candidate = calculate(
                        candidate_amount, D(0), [], D(0), D(str(policy["bonus_cap"])), D(bonus_amount)
                    )
                    price = Price(price.base, candidate.discounted, candidate.bonus, candidate.external)
                    # Allocate the whole-service money reward proportionally to
                    # plan and device rights for later segment valuation.
                    factor = price.discounted / price.base if price.base else D(1)
                    device_factor = factor
                    candidates.append(
                        {
                            "source": "promo",
                            "percent": str(
                                (price.base - price.discounted) * 100 / price.base if price.base else D(0)
                            ),
                            "reward_type": "fixed_rub",
                        }
                    )
            unadjusted_products = {}
            fixed_promo_products = {}
            basis["device_price_factor"] = str(device_factor)
            for item in s.scalars(select(Product).where(Product.currency == p.currency)):
                basis["device_terms"][item.id] = product_device_terms(item)
                try:
                    addon = product_device_surcharge(item, devices)
                except DomainError:
                    continue
                unadjusted_products[item.id] = money((item.price + addon) * D(str(period["months"])))
                basis["products"][item.id] = str(
                    money((item.price * factor + addon * device_factor) * D(str(period["months"])))
                )
                if applied_promo and promo.get("reward_type") == "fixed_rub":
                    item_period = calculate(
                        item.price * D(str(period["months"])),
                        D(str(period["discount_percent"])),
                        [],
                        D(str(policy["cap"])),
                        D(0),
                        D(0),
                    )
                    fixed_promo_products[item.id] = money(
                        max(
                            D(0),
                            item_period.discounted
                            + addon * D(str(period["months"]))
                            - D(promo["amount_rub"]),
                        )
                    )
            if action in {"upgrade", "downgrade", "devices"}:
                if not sub or not sub.expires_at or sub.expires_at <= moment:
                    raise DomainError("no_paid_segment", "Нет действующего оплаченного периода.")
                segments = panel_entitlements.valuation_segments(s, sub, moment)
                active = next((item for item in reversed(segments) if item.starts_at <= moment), None)
                from .legacy_catalog_valuation import supported

                if not active or not supported(active):
                    raise DomainError(
                        "valuation_unknown", "Стоимость оставшегося периода требует проверки владельцем."
                    )
                basis = dict(active.price_basis) | {"segment_id": active.id}
                devices = active.devices
                paid_order = s.get(Order, active.order_id) if active.order_id else None
                paid_quote = s.get(Quote, paid_order.quote_id) if paid_order else None
                paid_currency = basis.get("currency") or (
                    paid_quote.snapshot.get("currency") if paid_quote else None
                )
                if paid_currency != p.currency:
                    raise DomainError(
                        "valuation_currency_mismatch",
                        "Валюта оплаченного периода не допускает пересчёт в другую валюту.",
                        409,
                    )
                remaining = max(0, int((active.ends_at - moment).total_seconds()))
                old = s.get(Product, active.product_id)
                if action == "devices":
                    if (
                        extra_devices < 1
                        or extra_devices > 20
                        or p.id != active.product_id
                        or devices + extra_devices > 100
                    ):
                        raise DomainError(
                            "devices_invalid", "Выберите число устройств и настройте цену.", 400
                        )
                    previous_terms = basis.get("device_terms", {}).get(active.product_id)
                    try:
                        if not previous_terms or type(previous_terms.get("included")) is not int:
                            raise ValueError()
                        device_surcharge(
                            max(0, active.devices - previous_terms["included"]),
                            previous_terms.get("rules"),
                            previous_terms.get("price"),
                        )
                    except (ValueError, TypeError, KeyError):
                        raise DomainError(
                            "valuation_unknown",
                            "Стоимость ранее оплаченных устройств требует проверки владельцем.",
                            409,
                        ) from None
                    from .device_pricing import fixed_remaining_terms

                    base, segment_adjustments = fixed_remaining_terms(segments, moment, p, extra_devices)
                    basis = segment_adjustments[0]["basis"] | {"segment_id": active.id}
                    basis["device_segment_adjustments"] = segment_adjustments
                    devices += extra_devices
                else:
                    if active.devices > old.included_devices and not basis.get("device_pricing_included"):
                        raise DomainError(
                            "valuation_unknown", "Стоимость оплаченных устройств требует проверки владельцем."
                        )
                    old_price = D(basis["products"].get(active.product_id, "0"))
                    new_price = D(basis["products"].get(p.id, "0"))
                    if not old_price or not new_price:
                        raise DomainError("valuation_unknown", "Не найдены сопоставимые оплаченные условия.")
                    if action == "upgrade" and p.tier_level > old.tier_level:
                        base = upgrade(old_price, new_price, remaining, basis["period_days"] * 86400)
                    elif action == "downgrade" and p.tier_level < old.tier_level:
                        base = D("0.00")
                        end = moment + timedelta(seconds=downgrade_seconds(old_price, new_price, remaining))
                    else:
                        raise DomainError(
                            "tier_invalid", "Это действие недоступно для выбранного тарифа.", 400
                        )
                if action != "downgrade":
                    end = active.ends_at
                basis["active_ends_at"] = iso(end)
                basis["future_shift_seconds"] = int((end - active.ends_at).total_seconds())
                if len(segments) > 1:
                    end = segments[-1].ends_at + timedelta(seconds=basis["future_shift_seconds"])
                start = moment
                price = calculate(base, D(0), [], D(0), D(str(policy["bonus_cap"])), D(bonus_amount))
            elif action not in {"purchase", "renew"}:
                raise DomainError("action_invalid", "Действие недоступно.", 400)
            method_discount_amount = D(0)
            fixed_money_promo = bool(applied_promo and promo.get("reward_type") == "fixed_rub")
            period_discount_amount = D(0)
            secondary_discount_amount = D(0)
            promo_discount_amount = D(0)
            if action in {"purchase", "renew"}:
                period_only = calculate(
                    base,
                    D(str(period["discount_percent"])),
                    [],
                    D(str(policy["cap"])),
                    D(0),
                    D(0),
                )
                period_discount_amount = max(D(0), base - period_only.discounted)
                after_period_discount = max(D(0), price.base - price.discounted - period_discount_amount)
                if applied_promo:
                    promo_discount_amount = after_period_discount
                else:
                    secondary_discount_amount = after_period_discount
            if method_dto:
                previous_discounted = price.discounted
                percent = D(method_dto["discount_percent"])
                discounted = money(
                    max(
                        previous_discounted * (1 - percent / 100),
                        D(0) if fixed_money_promo else price.base * (1 - D(str(policy["cap"])) / 100),
                    )
                )
                recalculated = calculate(
                    discounted, D(0), [], D(0), D(str(policy["bonus_cap"])), D(bonus_amount)
                )
                price = Price(price.base, discounted, recalculated.bonus, recalculated.external)
                method_discount_amount = max(D(0), previous_discounted - discounted)
                if action == "devices":
                    # Discount only the new fixed supplement, preserving already purchased value.
                    _, adjustments = fixed_remaining_terms(
                        segments, moment, p, extra_devices, accepted_charge=discounted
                    )
                    basis = basis | {
                        key: adjustments[0]["basis"][key]
                        for key in ("products", "device_pricing_included", "device_adjustments")
                    } | {"device_segment_adjustments": adjustments}
                elif action == "upgrade":
                    if method_discount_amount:
                        # Preserve the old remainder; only the new tier payment receives this discount.
                        basis["products"] = basis["products"] | {
                            p.id: str(old_price + discounted * D(basis["period_days"] * 86400) / D(remaining))
                        }
                elif previous_discounted:
                    basis["products"] = {
                        key: str(money(D(value) * discounted / previous_discounted))
                        for key, value in basis["products"].items()
                    }
                    if basis.get("device_price_factor") is not None:
                        basis["device_price_factor"] = str(
                            D(basis["device_price_factor"]) * discounted / previous_discounted
                        )
            forced_minimum = False
            minimum = None
            if p.currency == "RUB" and action in {"purchase", "renew"}:
                configured_methods = self._setting(s, "payment_methods", {})
                if self.live or self.scoped_payments or configured_methods:
                    minimum = D(validate_payment_methods(configured_methods)["minimum_subscription_amount"])
            if minimum is not None:
                before_floor = price.discounted
                price, forced_minimum = subscription_minimum(price, minimum)
                if price.discounted != before_floor and price.base > 0:
                    basis["products"] = {
                        key: str(money(value * price.discounted / price.base))
                        for key, value in unadjusted_products.items()
                    }
                    basis["device_price_factor"] = str(price.discounted / price.base)
            if fixed_promo_products and action in {"purchase", "renew"}:
                # Freeze each alternative tariff with the same absolute reward.
                # A percentage inferred from the purchased tariff would invent
                # a larger reward on expensive tariffs during a later upgrade.
                method_factor = 1 - D(method_dto["discount_percent"]) / 100 if method_dto else D(1)
                basis["products"] = {
                    key: str(money(max(minimum or D(0), money(value * method_factor))))
                    for key, value in fixed_promo_products.items()
                }
                # Per-tier minimum floors make the marginal historical device rate ambiguous.
                basis["device_price_factor"] = None
            requested_credit = D(credit_amount)
            if not requested_credit.is_finite() or requested_credit < 0:
                raise DomainError("credit_invalid", "Некорректная сумма денежного остатка.", 400)
            credit = min(requested_credit.quantize(D("0.01"), rounding=ROUND_FLOOR), price.external)
            if (
                method_policy
                and action in {"purchase", "renew"}
                and p.currency == "RUB"
                and D(method_dto["discount_percent"]) > 0
                and price.external - credit < minimum
            ):
                forced_minimum = True
            if forced_minimum:
                credit = min(credit, max(D(0), price.external - minimum))
            if (
                (self.live or self.scoped_payments)
                and price.external - credit > 0
                and method_dto is None
                and not method_preview
            ):
                raise DomainError("payment_method_required", "Выберите способ оплаты.", 409)
            if credit > 0:
                self._validate_credit(a, p.currency, credit)
            # The minimum charge can reduce the realised discount; itemised amounts
            # must still add up to the immutable total discount, without double counting.
            available_discount = max(D(0), price.base - price.discounted)
            period_discount_amount = min(period_discount_amount, available_discount)
            available_discount -= period_discount_amount
            secondary_discount_amount = min(secondary_discount_amount, available_discount)
            available_discount -= secondary_discount_amount
            promo_discount_amount = min(promo_discount_amount, available_discount)
            available_discount -= promo_discount_amount
            method_discount_amount = min(method_discount_amount, available_discount)
            from sunrise_core.content import validate_content

            content_name = p.content.get("name") if isinstance(p.content, dict) else None
            try:
                content_name = deepcopy(validate_content(content_name)) if content_name else None
            except ValueError:
                content_name = None
            snapshot = {
                "period_discount_amount": str(money(period_discount_amount)),
                "secondary_discount_amount": str(money(secondary_discount_amount)),
                "promo_discount_amount": str(money(promo_discount_amount)),
                "promo_reward_amount_rub": promo.get("amount_rub") if applied_promo else None,
                "payment_method": method_dto,
                "payment_method_policy": method_policy,
                "payment_method_discount_amount": str(money(method_discount_amount)),
                "minimum_subscription_amount": str(money(minimum)) if minimum is not None else None,
                "minimum_external_required": forced_minimum,
                "panel_mappings": basis.get("panel_mappings", {})
                if action in {"upgrade", "downgrade", "devices"}
                else {item.id: item.internal_squad_uuid for item in s.scalars(select(Product))},
                "panel_configuration": basis.get("panel_configuration", {})
                if action in {"upgrade", "downgrade", "devices"}
                else self._setting(s, "panel_mapping", {}),
                "action": action,
                "product_id": p.id,
                "product_version": p.version,
                "product_name": p.name,
                "product_content_name": content_name,
                "subscription_id": subscription_id,
                "base_amount": str(price.base),
                "device_amount": str(base if action == "devices" else device_amount),
                "device_pricing": {
                    "amount": str(base if action == "devices" else money(device_amount)),
                    "unit_price": str(p.extra_device_price) if p.extra_device_price is not None else None,
                    "extra_devices": extra_devices
                    if action == "devices"
                    else max(0, devices - p.included_devices),
                    "billing_months": None if action == "devices" else period["months"],
                    "mode": "fixed_remaining_term" if action == "devices" else "monthly_renewal",
                }
                if action in {"devices", "renew"}
                else None,
                "discount_amount": str(price.base - price.discounted),
                "bonus_amount": str(price.bonus),
                "credit_amount": str(credit),
                "credit_currency": a.credit_currency if credit > 0 else None,
                "total": str(price.external - credit),
                "currency": p.currency,
                "resulting_expires_at": iso(end),
                "starts_at": iso(start),
                "devices_limit": devices,
                "version": version,
                "basis": basis,
                "promo_code": applied_promo,
                "promo_terms": {
                    "per_account_limit": 1,
                    "total_limit": promo.get("total_limit"),
                    "audience": promo.get("audience", "all"),
                }
                if applied_promo
                else None,
                "promo_activation_id": pending["id"] if applied_promo else None,
                "discount_candidates": candidates,
                "near_expiry_renewal_evidence": near_expiry_evidence,
                "selected_secondary": next((item for item in candidates if item["source"] == "promo"), None)
                if applied_promo
                else max(candidates, key=lambda x: D(x["percent"]))
                if candidates
                else None,
                "breakdown": [
                    {"label": "Стоимость периода", "amount": str(price.base)},
                    {"label": "Скидка", "amount": str(price.base - price.discounted)},
                    {"label": "Бонусы", "amount": str(price.bonus)},
                    {"label": "Денежный остаток", "amount": str(credit)},
                    {"label": "К оплате", "amount": str(price.external - credit)},
                ],
            }
            if method_dto:
                snapshot["breakdown"].insert(
                    2, {"label": "Скидка за способ оплаты", "amount": str(money(method_discount_amount))}
                )
            if forced_minimum:
                snapshot["breakdown"].insert(
                    -1, {"label": "Минимальная оплата подписки", "amount": str(money(minimum))}
                )
            if applied_promo and promo.get("legacy_promo_grant"):
                snapshot["legacy_promo_grant"] = dict(promo["legacy_promo_grant"])
            q = Quote(
                # The selected method is bound to the immutable quote, never an order override.
                account_id=a.id,
                subscription_id=subscription_id,
                snapshot=snapshot,
                expires_at=moment + timedelta(minutes=10),
            )
            s.add(q)
            s.flush()
            return self._quote_dto(q)

    def _quote_dto(self, q):
        from .catalog import billing_months_dto

        result = {
            k: v
            for k, v in q.snapshot.items()
            if k
            not in {
                "basis",
                "starts_at",
                "payment_method_policy",
                "product_version",
                "panel_mappings",
                "promo_activation_id",
                "promo_terms",
                "legacy_promo_grant",
            }
        } | {
            "id": q.id,
            "expires_at": iso(q.expires_at),
        }
        device_pricing = result.get("device_pricing")
        if device_pricing and device_pricing.get("billing_months") is not None:
            result["device_pricing"] = device_pricing | {
                "billing_months": billing_months_dto(device_pricing["billing_months"])
            }
        return result

    def _validate_credit(self, account, currency, amount):
        if account.credit_currency is None:
            raise DomainError("credit_currency_unverified", "Валюта денежного остатка требует проверки.", 409)
        if account.credit_currency != currency:
            raise DomainError("credit_currency_mismatch", "Валюты кошелька и покупки не совпадают.", 409)
        if account.credit_balance - account.credit_reserved < amount:
            raise DomainError(
                "credit_insufficient", "Денежный остаток уже использован или зарезервирован.", 409
            )

    def _order_dto(self, o):
        from .retention import fenced_in

        checkout_url = None
        session = object_session(o)
        quote = session.get(Quote, o.quote_id) if session is not None else None
        window = (
            self._invoice_window_in(session, o, quote)
            if session and quote and o.environment in {"live", "scoped_live"}
            else None
        )
        if o.environment in {"sandbox", "live", "scoped_live"} and o.status == "awaiting_payment" and quote:
            operation = (
                session.scalar(select(ProviderOperation).where(ProviderOperation.order_id == o.id))
                if session is not None
                else None
            )
            if (
                operation
                and operation.state in {"created", "reconciled"}
                and isinstance(operation.pay_url, str)
                and (now() <= window[1] if window is not None else quote.expires_at > now())
            ):
                if o.environment in {"live", "scoped_live"}:
                    checkout_url = (
                        verified_checkout_url(
                            operation.pay_url, self.runtime_policy.scope.rollypay.checkout_hosts
                        )
                        if self.live or self.scoped_payments
                        else None
                    )
                else:
                    checkout_url = sandbox_checkout_url(operation.pay_url)
        readiness = None
        if o.status == "paid" and session is not None:
            job = session.scalar(select(Job).where(Job.key == "provision:" + o.id, Job.kind == "provision"))
            sub = session.get(Subscription, o.subscription_id) if o.subscription_id else None
            owned = (
                job
                and sub
                and sub.account_id == o.account_id
                and job.account_id == o.account_id
                and job.payload.get("subscription_id") == sub.id
            )
            readiness = (
                "ready"
                if owned
                and job.state == "done"
                and not job.last_error
                and job.payload.get("panel_status", "ACTIVE") == "ACTIVE"
                and job.payload.get("environment") == sub.environment
                and panel_entitlements.local_status(session, sub) == "active"
                and not fenced_in(session, sub)
                and not panel_entitlements.remote_status(session, sub)
                else "pending"
                if owned and job.state in {"pending", "in_flight"}
                else "review"
            )
        return {
            "id": o.id,
            "payment_method": quote.snapshot.get("payment_method") if quote else None,
            "status": o.status,
            "total": str(o.amount),
            "currency": o.currency,
            "checkout_url": checkout_url,
            "payment_expires_at": iso(window[1]) if window is not None else None,
            "quote_id": o.quote_id,
            "environment": o.environment,
            "subscription_id": o.subscription_id,
            "provisioning_status": readiness,
        }

    def create_order(self, account_id, quote_id, idempotency_key, source="site"):
        if not 1 <= len(idempotency_key) <= 128:
            raise DomainError("key_invalid", "Неверный ключ операции.", 400)
        with self.db.transaction() as s:
            account = self._account(s, account_id)
            existing = s.scalar(
                select(Order).where(Order.account_id == account.id, Order.quote_id == quote_id)
            )
            quote = s.scalar(select(Quote).where(Quote.account_id == account.id, Quote.id == quote_id))
            refresh_sid = quote.subscription_id if quote and not existing else None
        if refresh_sid:
            self._refresh_for_price(account_id, refresh_sid)
        with self.db.transaction() as s:
            a = self._account(s, account_id, True)
            duplicate_quote = s.scalar(
                select(Order).where(Order.account_id == a.id, Order.quote_id == quote_id)
            )
            if duplicate_quote:
                return self._order_dto(duplicate_quote)
            existing = s.scalar(
                select(Order).where(Order.account_id == a.id, Order.idempotency_key == idempotency_key)
            )
            if existing:
                if existing.quote_id != quote_id:
                    raise DomainError("key_conflict", "Ключ операции уже использован.")
                return self._order_dto(existing)
            q = s.scalar(
                select(Quote).where(Quote.id == quote_id, Quote.account_id == a.id).with_for_update()
            )
            if not q:
                raise missing()
            from .retention import assert_legacy_operation_allowed_in

            assert_legacy_operation_allowed_in(s, a.id, q.subscription_id)
            self._assert_new_operations(s)
            self.legal.require_current(s, a)
            duplicate = s.scalar(select(Order).where(Order.quote_id == q.id))
            if duplicate:
                return self._order_dto(duplicate)
            if q.expires_at <= now():
                raise DomainError("quote_expired", "Расчёт устарел. Рассчитайте цену ещё раз.")
            selected_method = q.snapshot.get("payment_method")
            if selected_method:
                current, binding = self._payment_method_in(s, selected_method["id"], q.snapshot["currency"])
                if binding != q.snapshot.get("payment_method_policy") or current != selected_method:
                    raise DomainError("quote_stale", "Способ оплаты изменился. Обновите расчёт.", 409)
            elif self.live and D(q.snapshot["total"]) > 0:
                raise DomainError("payment_method_required", "Выберите способ оплаты.", 409)
            if (q.snapshot.get("selected_secondary") or {}).get(
                "source"
            ) == "referral_welcome" and not self._welcome_eligible(s, a):
                raise DomainError(
                    "quote_stale", "Скидка первой покупки уже использована или зарезервирована."
                )
            p = s.get(Product, q.snapshot["product_id"])
            if p.version != q.snapshot["product_version"] or not p.available:
                raise DomainError("quote_stale", "Условия изменились. Рассчитайте цену ещё раз.")
            from .legacy_catalog_valuation import require_current_catalog

            require_current_catalog(s, q.snapshot["basis"])
            if q.subscription_id:
                sub = self._sub(s, a.id, q.subscription_id, True)
                self._assert_subscription_operations(s, sub)
                if sub.version != q.snapshot["version"]:
                    raise DomainError("quote_stale", "Подписка изменилась. Обновите расчёт.")
                if q.snapshot["action"] == "renew":
                    panel_entitlements.require_pricing(s, sub, finite_renewal=True)
                    if panel_entitlements.bound(sub) and p.id != sub.product_id:
                        raise DomainError(
                            "panel_product_changed", "Тариф изменён в панели. Обновите расчёт продления.", 409
                        )
                else:
                    panel_entitlements.require_valuation(s, sub)
            bonus = D(q.snapshot["bonus_amount"])
            if bonus > 0 and a.bonus_balance - a.bonus_reserved < bonus:
                raise DomainError("bonus_insufficient", "Бонусы уже используются другим заказом.")
            credit = D(q.snapshot.get("credit_amount", "0"))
            if credit:
                self._validate_credit(a, q.snapshot["currency"], credit)
                if q.snapshot.get("credit_currency") != a.credit_currency:
                    raise DomainError("quote_stale", "Валюта денежного остатка изменилась.", 409)
            promo_code = q.snapshot.get("promo_code")
            if promo_code:
                activation_id = q.snapshot.get("promo_activation_id")
                if activation_id and (not a.pending_promo or a.pending_promo["id"] != activation_id):
                    raise DomainError(
                        "quote_stale", "Активированный промокод изменился. Обновите расчёт.", 409
                    )
                if activation_id and s.scalar(
                    select(PromoUse.order_id)
                    .where(
                        PromoUse.account_id == a.id, PromoUse.code == promo_code, PromoUse.state == "reserved"
                    )
                    .limit(1)
                ):
                    raise DomainError("promo_reserved", "Промокод ожидает оплаты другого заказа.", 409)
                s.scalar(select(Setting).where(Setting.key == "discounts").with_for_update())
                current_promo = self._promo(s, a.id, promo_code, q.snapshot["action"])
                if q.snapshot.get("legacy_promo_grant") != current_promo.get("legacy_promo_grant"):
                    raise DomainError(
                        "quote_stale", "Подтверждение промокода изменилось. Обновите расчёт.", 409
                    )
            if self.live:
                from .accounts import require_account_access_in

                require_account_access_in(s, a, self.runtime_policy)
                self.runtime_policy.require_current(
                    s, "invoice_create" if D(q.snapshot["total"]) > 0 else "financial_apply"
                )
            if self.scoped_payments:
                self.runtime_policy.require_current(
                    s, "invoice_create" if D(q.snapshot["total"]) > 0 else "financial_apply"
                )
                self.runtime_policy.require_account_in(s, a)
            a.bonus_reserved += bonus
            a.credit_reserved += credit
            o = Order(
                id=self.runtime_policy.scope.order_prefix + uid() if self.scoped_payments else uid(),
                account_id=a.id,
                quote_id=q.id,
                idempotency_key=idempotency_key,
                amount=D(q.snapshot["total"]),
                currency=q.snapshot["currency"],
                subscription_id=q.subscription_id,
                source=source,
                environment="scoped_live"
                if self.scoped_payments
                else "live"
                if self.live
                else "local_test"
                if self.local_test and not self.sandbox_payments
                else "sandbox",
            )
            s.add(o)
            s.flush()
            if promo_code:
                s.add(PromoUse(order_id=o.id, account_id=a.id, code=promo_code))
            if o.amount == 0:
                self._fulfill(s, a, o, q)
            return self._order_dto(o)

    def order(self, account_id, order_id):
        with self.db.transaction() as s:
            a = self._account(s, account_id)
            o = s.scalar(select(Order).where(Order.id == order_id, Order.account_id == a.id))
            if not o:
                raise missing()
            return self._order_dto(o)

    def test_pay(self, account_id, order_id):
        if not self.local_test:
            raise missing()
        with self.db.transaction() as s:
            a = self._account(s, account_id, True)
            o = s.scalar(
                select(Order).where(Order.id == order_id, Order.account_id == a.id).with_for_update()
            )
            if not o:
                raise missing()
            if o.environment != "local_test":
                raise missing()
            p = s.scalar(select(Payment).where(Payment.order_id == o.id))
            if p:
                return self._order_dto(o)
            if o.amount <= 0:
                return self._order_dto(o)
            pid = uid()
            s.add(
                Payment(
                    id=pid,
                    provider="local_test",
                    provider_id=o.id,
                    environment="local_test",
                    order_id=o.id,
                    account_id=a.id,
                    amount=o.amount,
                    currency=o.currency,
                )
            )
            q = s.get(Quote, o.quote_id)
            self._fulfill(s, a, o, q, pid)
            return self._order_dto(o)

    def _invoice_window_in(self, session, order, quote):
        """Bound merchant evidence preserves a placed live order's immutable price."""
        if (
            not (self.live or self.scoped_payments)
            or order.environment not in {"live", "scoped_live"}
            or order.created_at >= quote.expires_at
        ):
            return None
        operation = session.scalar(select(ProviderOperation).where(ProviderOperation.order_id == order.id))
        request = operation.request if operation and isinstance(operation.request, dict) else {}
        creation = request.get("live_creation")
        if (
            operation is None
            or operation.state not in {"created", "reconciled"}
            or not isinstance(creation, dict)
            or request.get("scope_id") != self.runtime_policy.scope.scope_id
            or request.get("environment") != order.environment
            or request.get("provider") != "rollypay"
            or request.get("terminal") != operation.terminal
            or request.get("order_id") != order.id
            or request.get("currency") != order.currency
            or not panel_entitlements.owned_account(session, order, request.get("account_id"))
            or creation.get("test") is not False
            or creation.get("environment") != "prod"
            or creation.get("provider") != "rollypay"
            or creation.get("scope_id") != self.runtime_policy.scope.scope_id
            or creation.get("terminal_id") != operation.terminal
            or creation.get("order_id") != order.id
            or creation.get("payment_id") != operation.provider_id
            or not operation.provider_id
        ):
            return None
        from .panel import PanelReview
        from .panel_v3 import instant

        try:
            if isinstance(request.get("amount"), bool) or D(request["amount"]) != order.amount:
                return None
            created, expiry = instant(creation["created_at"]), instant(creation["expires_at"])
            if not order.created_at.replace(microsecond=0) <= created <= now() or expiry <= created:
                return None
        except (KeyError, ValueError, TypeError, ArithmeticError, PanelReview):
            return None
        return created, expiry, operation

    def _paid_order_timing_in(self, session, order, quote, payment_id):
        window = self._invoice_window_in(session, order, quote)
        if window is None:
            return quote.expires_at > now()
        if not payment_id:
            return False
        created, expiry, operation = window
        payments = list(session.scalars(select(Payment).where(Payment.order_id == order.id)))
        if len(payments) != 1 or payments[0].id != payment_id:
            return False
        payment = payments[0]
        return bool(
            payment.status == "paid"
            and payment.provider == "rollypay"
            and payment.provider_id == operation.provider_id
            and payment.terminal == operation.terminal
            and payment.environment == order.environment
            and payment.account_id == order.account_id
            and payment.amount == order.amount
            and payment.currency == order.currency
            and payment.paid_at is not None
            and payment.paid_at.tzinfo is not None
            and max(created, order.created_at) <= payment.paid_at <= min(expiry, now())
            and session.scalar(select(Refund.id).where(Refund.payment_id == payment.id)) is None
        )

    def _fulfill(self, s, a, o, q, payment_id=None):
        if self.scoped_payments:
            self.runtime_policy.require_current(s, "financial_apply")
            self.runtime_policy.require_account_in(s, a)
            if o.amount > 0:
                self.runtime_policy.require_order_in(s, o)
            elif o.environment != "scoped_live":
                raise DomainError("scoped_order_denied", "Заказ не относится к этому стенду.", 409)
        if self.live:
            self.runtime_policy.require_current(s, "financial_apply")
        from datetime import datetime

        if o.fulfilled_at is not None:
            return
        v = q.snapshot
        if not self._paid_order_timing_in(s, o, q, payment_id) or o.status in {"canceled", "expired"}:
            o.status = "reconciliation_required"
            return
        if (v.get("selected_secondary") or {}).get(
            "source"
        ) == "referral_welcome" and not self._welcome_eligible(s, a, o):
            o.status = "reconciliation_required"
            return
        sub = self._sub(s, a.id, q.subscription_id, True) if q.subscription_id else None
        if sub:
            from .retention import fenced_in

            if fenced_in(s, sub):
                o.status = "reconciliation_required"
                return
        if sub and self._setting(s, "dispute:" + sub.id, {}).get("enabled"):
            o.status = "reconciliation_required"
            return
        if sub and sub.version != v["version"]:
            o.status = "reconciliation_required"
            return
        if sub:
            from .legacy_catalog_valuation import require_accepted_import

            try:
                require_accepted_import(s, sub, v["basis"])
            except DomainError:
                o.status = "reconciliation_required"
                return
        if v.get("promo_code"):
            s.scalar(select(Setting).where(Setting.key == "discounts").with_for_update())
            try:
                self.promos.validate_fulfillment(s, a, o, v)
            except DomainError:
                o.status = "reconciliation_required"
                return
        referral_percent = None
        if a.referrer_id and a.referrer_id != a.id and o.amount > 0 and payment_id:
            policy = self._setting(s, "discounts", {})
            try:
                referral_percent = D(str(policy["referral_percent"]))
                if not referral_percent.is_finite() or not 0 <= referral_percent <= 100:
                    raise ValueError("invalid_referral_policy")
            except (KeyError, ValueError, ArithmeticError):
                o.status = "reconciliation_required"
                s.add(
                    Audit(
                        action="fulfillment_policy_blocked",
                        target=o.id,
                        reason="Подтверждённая оплата сохранена; настройте реферальное вознаграждение и проверьте заказ.",
                        details={"code": "referral_policy_unavailable", "payment_id": payment_id},
                    )
                )
                return
        bonus = D(v["bonus_amount"])
        promo = s.get(PromoUse, o.id)
        if self.live:
            self.runtime_policy.require_current(s, "financial_apply")
        if self.scoped_payments:
            self.runtime_policy.require_current(s, "financial_apply")
            self.runtime_policy.require_account_in(s, a)
            if o.amount > 0:
                self.runtime_policy.require_order_in(s, o)
        if promo:
            promo.state = "used"
            if a.pending_promo and a.pending_promo["id"] == v.get("promo_activation_id"):
                a.pending_promo = None
        a.bonus_reserved -= bonus
        a.bonus_balance -= bonus
        credit = D(v.get("credit_amount", "0"))
        if credit:
            a.credit_reserved -= credit
            a.credit_balance -= credit
            s.add(
                CreditLedger(
                    account_id=a.id,
                    original_account_id=a.id,
                    operation="spend:" + o.id,
                    amount=-credit,
                    currency=v["credit_currency"],
                    provenance="order_redemption",
                )
            )
        if bonus:
            s.add(Ledger(account_id=a.id, operation="spend:" + o.id, amount=-bonus))
        end = datetime.fromisoformat(v["resulting_expires_at"])
        start = datetime.fromisoformat(v["starts_at"])
        prior_status = panel_entitlements.local_status(s, sub) if sub else "active"
        if not sub:
            sub = Subscription(
                account_id=a.id,
                product_id=v["product_id"],
                expires_at=end,
                devices_limit=v["devices_limit"],
                environment=o.environment,
            )
            s.add(sub)
            s.flush()
            o.subscription_id = sub.id
        if v["action"] in {"purchase", "renew"}:
            s.add(
                Segment(
                    subscription_id=sub.id,
                    order_id=o.id,
                    product_id=v["product_id"],
                    starts_at=start,
                    ends_at=end,
                    devices=v["devices_limit"],
                    paid_value=D(v["base_amount"]) - D(v["discount_amount"]),
                    price_basis=v["basis"]
                    | {
                        "panel_mappings": v.get("panel_mappings", {}),
                        "panel_configuration": v.get("panel_configuration", {}),
                    },
                )
            )
            if start <= now():
                sub.product_id = v["product_id"]
        else:
            from .legacy_catalog_valuation import accepted_basis, materialize

            segment = materialize(s, sub, v["basis"])
            old_end = segment.ends_at
            segment.ends_at = datetime.fromisoformat(
                v["basis"].get("active_ends_at", v["resulting_expires_at"])
            )
            segment.product_id = v["product_id"]
            segment.devices = v["devices_limit"]
            segment.price_basis = accepted_basis({
                k: value for k, value in v["basis"].items() if k != "device_segment_adjustments"
            }) | {
                "panel_mappings": v.get("panel_mappings", {}),
                "panel_configuration": v.get("panel_configuration", {}),
            }
            shift = v["basis"].get("future_shift_seconds", 0)
            if shift:
                for future in s.scalars(
                    select(Segment).where(
                        Segment.subscription_id == sub.id,
                        Segment.starts_at >= old_end,
                        Segment.id != segment.id,
                        Segment.kind != "refunded",
                    )
                ):
                    future.starts_at += timedelta(seconds=shift)
                    future.ends_at += timedelta(seconds=shift)
            paid_increment = D(v["total"]) + bonus + credit
            if v["action"] == "devices":
                adjustments = v["basis"].get("device_segment_adjustments", [])
                for adjustment in adjustments:
                    target = s.get(Segment, adjustment["segment_id"])
                    if target.id != segment.id:
                        target.devices = adjustment["devices"]
                        target.price_basis = adjustment["basis"]
                        target.paid_value += D(adjustment["charge"])
                        paid_increment -= D(adjustment["charge"])
            segment.paid_value += paid_increment
            panel_entitlements.accept_valuation(s, sub, v["basis"])
            sub.product_id = v["product_id"]
        sub.expires_at = end
        if start <= now():
            sub.devices_limit = v["devices_limit"]
        sub.version += 1
        sub.status = "provisioning_pending"
        o.status = "paid"
        o.fulfilled_at = o.fulfilled_at or now()
        if payment_id and self.public_settings is not None:
            from .campaigns import Campaigns

            Campaigns(self, self.public_settings).record_paid_in(s, o, payment_id)
        s.add(
            Job(
                key="provision:" + o.id,
                kind="provision",
                account_id=a.id,
                payload=self._provision_payload(s, sub, prior_status=prior_status, origin="paid")
                | {"order_id": o.id},
            )
        )
        s.add(
            Job(
                key="notify:" + o.id,
                kind="notification",
                account_id=a.id,
                payload={"order_id": o.id, "template": "payment_received"},
            )
        )
        if a.referrer_id and a.referrer_id != a.id and o.amount > 0 and payment_id:
            ref = self._account(s, a.referrer_id, True, customer=False)
            if self.live:
                self.runtime_policy.require_current(s, "financial_apply")
            if self.scoped_payments:
                self.runtime_policy.require_current(s, "financial_apply")
                self.runtime_policy.require_account_in(s, a)
                if o.amount > 0:
                    self.runtime_policy.require_order_in(s, o)
            reward = money(o.amount * referral_percent / 100)
            ref.bonus_balance += reward
            s.add(Ledger(account_id=ref.id, operation="referral_payment:" + payment_id, amount=reward))
        if self.public_settings is not None:
            from .notifications import NotificationService
            from .sale_notifications import paid_staff_sale_content

            if self.scoped_payments:
                self.runtime_policy.require_paid_service_in(s, o)

            notifier = NotificationService(self.db, self.public_settings)
            notifier.emit_in(
                s,
                a.id,
                "payment:" + o.id,
                "payment_received",
                "Заказ оплачен",
                "Заказ оплачен. Готовим доступ. Статус подписки можно проверить в меню.",
                target="/#subscriptions",
            )
            sale_title, sale_body = paid_staff_sale_content(a, o, v)
            from .staff_permissions import telegram_admin_ids

            for staff_account in s.scalars(
                select(Account).where(
                    Account.telegram_id.in_(telegram_admin_ids(self.public_settings)),
                )
            ):
                notifier.emit_in(
                    s,
                    staff_account.id,
                    "sale:" + o.id,
                    "sale",
                    sale_title,
                    sale_body,
                    target="/admin#statistics",
                )

    def _subscription_connection_url_in(self, session, sub):
        from .instance_config import configured_instance
        from .panel import PanelReview
        from .panel_events import PanelEvents

        origin = configured_instance(self.public_settings).subscription_public_origin
        if not origin:
            return sub.connection_url
        panel = self.panel_provider
        if not panel:
            return None
        try:
            PanelEvents(self.db, panel, self.public_settings)._owned(session, sub)
            hosts = set(panel.subscription_hosts) & set(self.public_settings.panel_subscription_allowed_hosts)
            policy = getattr(panel, "scoped_runtime_policy", None) or getattr(panel, "runtime_policy", None)
            if policy:
                hosts &= set(policy.scope.panel.subscription_hosts)
            return subscription_public_url(sub, panel, origin, hosts)
        except (DomainError, ValueError, PanelReview):
            return None

    def connection_instructions(self, account_id, sid):
        from .service_content import CONNECTION_NOTICE

        with self.db.transaction() as session:
            sub = self._sub(session, account_id, sid)
            self._assert_subscription_operations(session, sub, read_existing=True)
            url = sub.connection_url if self._subscription_connection_url_in(session, sub) else None
        # Авторизация завершается до сети; никакие блокировки БД не держатся во время загрузки.
        platforms = self.connection_provider.get(url) if self.connection_provider and url else []
        return {
            "source": "remnawave" if platforms else "fallback",
            "platforms": platforms,
            "notice": CONNECTION_NOTICE,
        }

    def _manual_renewal_discount_projection(self, session, account, sub):
        result = {"manual_renewal_discount_eligible": False, "manual_renewal_discount_percent": None}
        policy = self._setting(session, "discounts", {})
        candidate = policy.get("near_expiry_renewal") or {}
        moment = now()
        horizon = panel_entitlements.verified_service_horizon(session, sub, moment)
        if candidate.get("enabled") is not True or horizon is None or horizon - moment >= timedelta(hours=24):
            return result
        try:
            self._assert_new_operations(session)
            self._assert_subscription_operations(session, sub)
            self.legal.require_current(session, account)
            panel_entitlements.require_pricing(session, sub)
            product = session.get(Product, sub.product_id) if sub.product_id else None
            if not product or not product.available or not {"cap", "bonus_cap"} <= set(policy):
                return result
            if session.scalar(
                select(Agreement.id)
                .where(Agreement.subscription_id == sub.id, Agreement.state != "stopped")
                .limit(1)
            ):
                return result
            percent, cap = D(candidate["percent"]), D(policy["cap"])
            if not percent.is_finite() or not cap.is_finite() or not 0 < percent <= 100 or cap <= 0:
                return result
        except (DomainError, ValueError, KeyError):
            return result
        return {
            "manual_renewal_discount_eligible": True,
            "manual_renewal_discount_percent": candidate["percent"],
        }

    def dismiss_deletion_notice(self, account_id, sid):
        from .subscription_archive import state_in

        with self.db.transaction() as session:
            account = self._account(session, account_id, True)
            sub = self._sub(session, account.id, sid, True)
            archive = state_in(session, sub)
            if archive is None:
                raise missing()
            key = deletion_notice_ack_key(sub.id)
            if not deletion_notice_dismissed_in(session, sub, archive):
                value = {
                    "subscription_id": sub.id,
                    "proof_id": archive.proof_id,
                    "dismissed_at": now().isoformat(),
                }
                row = session.get(Setting, key)
                if row:
                    row.value = value
                else:
                    session.add(Setting(key=key, value=value))
        return {"dismissed": True}

    def dashboard(self, account_id, *, refresh_panel=False, refresh_subscription_id=None):
        from .subscription_archive import is_retired_local_fixture

        sync_results = self._refresh_panel(account_id, refresh_subscription_id) if refresh_panel else {}
        with self.db.transaction() as s:
            a = self._account(s, account_id)
            promo_targets = {sub.id for sub in self.promos.targets_in_transaction(s, a)}
            subs = []
            deletion_notices, archived_agreements = [], []
            for sub in s.scalars(
                select(Subscription).where(Subscription.account_id == a.id).order_by(Subscription.id)
            ):
                if is_retired_local_fixture(sub):
                    continue
                segs = list(
                    s.scalars(
                        select(Segment).where(Segment.subscription_id == sub.id).order_by(Segment.starts_at)
                    )
                )
                active = panel_entitlements.current_segment(s, sub)
                ag = s.scalar(
                    select(Agreement)
                    .where(Agreement.subscription_id == sub.id)
                    .order_by((Agreement.state == "stopped").asc(), Agreement.created_at.desc())
                    .limit(1)
                )
                from .subscription_archive import state_in as archive_state_in

                try:
                    archive = archive_state_in(s, sub)
                except DomainError:
                    archive = None
                if archive is not None:
                    if not deletion_notice_dismissed_in(s, sub, archive):
                        deletion_notices.append(
                            {
                                "subscription_id": sub.id,
                                "name": sub.name,
                                "deleted_at": archive.deleted_at,
                                "reason": archive.reason,
                            }
                        )
                    if ag:
                        archived_agreements.append(
                            {
                                "subscription_id": sub.id,
                                "name": sub.name,
                                "recurring": {
                                    "id": ag.id,
                                    "state": ag.state,
                                    "amount": str(ag.consent_amount),
                                    "currency": ag.currency,
                                    "next_charge_at": iso(ag.next_charge_at),
                                },
                            }
                        )
                    continue
                subs.append(
                    {
                        "id": sub.id,
                        "promo_claim_eligible": sub.id in promo_targets,
                        "product_id": active.product_id if active else sub.product_id,
                        "name": sub.name,
                        "product_name": s.get(Product, active.product_id if active else sub.product_id).name
                        if sub.product_id
                        else None,
                        "status": sub.status if not sub.expires_at or sub.expires_at > now() else "expired",
                        "expires_at": iso(sub.expires_at),
                        "devices_limit": sub.devices_limit,
                        "version": sub.version,
                        "connection_url": sub.connection_url,
                        "devices": [
                            {"id": d.id, "name": d.name, "last_seen_at": iso(d.last_seen_at)}
                            for d in s.scalars(
                                select(Device).where(Device.subscription_id == sub.id, ~Device.revoked)
                            )
                        ],
                        "segments": [
                            {
                                "id": x.id,
                                "product_id": x.product_id,
                                "starts_at": iso(x.starts_at),
                                "ends_at": iso(x.ends_at),
                                "devices": x.devices,
                                "paid_value": str(x.paid_value),
                            }
                            for x in segs
                        ],
                        "recurring": (
                            {
                                "id": ag.id,
                                "state": ag.state,
                                "amount": str(ag.consent_amount),
                                "currency": ag.currency,
                                "next_charge_at": iso(ag.next_charge_at),
                            }
                            if ag
                            else None
                        ),
                    }
                )
                from .subscription_engagement import SubscriptionEngagement

                subs[-1].update(SubscriptionEngagement(self).projection_in(s, sub))
                observation = s.scalar(
                    select(PanelObservation)
                    .where(PanelObservation.subscription_id == sub.id, PanelObservation.kind == "user")
                    .order_by(PanelObservation.observed_at.desc())
                    .limit(1)
                )
                subs[-1].update(
                    {
                        "panel_status": observation.status if observation else None,
                        "observed_at": iso(observation.observed_at) if observation else None,
                        "traffic_bytes": str(observation.traffic_bytes)
                        if observation and observation.traffic_bytes is not None
                        else None,
                        "traffic_limit_bytes": str(observation.traffic_limit_bytes)
                        if observation and observation.traffic_limit_bytes is not None
                        else None,
                        "device_inventory_state": "unavailable"
                        if observation and observation.last_error
                        else "fresh"
                        if observation and observation.observed_at > now() - timedelta(minutes=10)
                        else "stale"
                        if observation
                        else "unknown",
                    }
                )
                subs[-1].update(panel_entitlements.facts(s, sub, sync_results.get(sub.id)))
                if subs[-1]["panel_product_state"] == "matched":
                    product = s.get(Product, subs[-1]["panel_product_id"])
                    subs[-1]["product_id"], subs[-1]["product_name"] = product.id, product.name
                product = s.get(Product, subs[-1]["product_id"]) if subs[-1]["product_id"] else None
                subs[-1]["name"] = subscription_display_name(product, sub.name)
                if product:
                    subs[-1]["product_name"] = subs[-1]["name"]
                subs[-1]["connection_url"] = self._subscription_connection_url_in(s, sub)
                from .panel_v3 import instant

                effective_expiry = subs[-1]["expires_at"]
                if (
                    effective_expiry
                    and instant(effective_expiry) <= now()
                    and sub.status
                    not in {"revoked", "revocation_pending", "suspended", "suspension_pending", "disputed"}
                ):
                    subs[-1]["status"] = "expired"
                if sub.status in {
                    "revoked",
                    "revocation_pending",
                    "suspended",
                    "suspension_pending",
                    "disputed",
                }:
                    subs[-1]["status"] = sub.status
                restriction = s.get(Setting, "restriction:" + sub.id)
                if restriction and restriction.value.get("suspended"):
                    subs[-1]["status"] = "suspended"
                if self._setting(s, "dispute:" + sub.id, {}).get("enabled"):
                    subs[-1]["status"] = "disputed"
                if sub.status not in {
                    "revoked",
                    "revocation_pending",
                    "suspended",
                    "suspension_pending",
                    "provisioning_pending",
                    "disputed",
                }:
                    confirmed_status = panel_entitlements.remote_status(s, sub) or (
                        "EXPIRED" if observation and observation.status == "EXPIRED" else None
                    )
                    subs[-1]["status"] = {
                        "DISABLED": "suspended",
                        "LIMITED": "limited",
                        "EXPIRED": "expired",
                    }.get(confirmed_status, subs[-1]["status"])
                rotation = s.scalar(
                    select(Job)
                    .where(
                        Job.kind == "credential_rotate",
                        Job.payload["subscription_id"].as_string() == sub.id,
                        Job.state.in_(["pending", "in_flight", "review", "blocked"]),
                    )
                    .order_by(Job.id.desc())
                    .limit(1)
                )
                subs[-1]["credential_rotation_state"] = rotation.state if rotation else None
                from .customer_operations import rotation_cancelable

                subs[-1]["credential_rotation_job_id"] = rotation.id if rotation else None
                subs[-1]["credential_rotation_cancelable"] = rotation_cancelable(
                    rotation, sub, panel_entitlements.registered_binding(s, sub)
                )
                if self.scoped_payments and sub.environment in {"local_test", "sandbox"}:
                    subs[-1].update({"retention_state": "none", "cleanup_at": None, "archived_at": None})
                else:
                    subs[-1].update(self.retention.projection_in(s, sub))
                subs[-1].update(self._manual_renewal_discount_projection(s, a, sub))
                if subs[-1]["retention_state"] != "none":
                    subs[-1]["connection_url"] = None
                    subs[-1]["devices"] = []
                if rotation:
                    subs[-1]["connection_url"] = None
            payments = [
                {
                    "id": p.id,
                    "order_id": p.order_id,
                    "amount": str(p.amount),
                    "currency": p.currency,
                    "status": p.status,
                    "paid_at": iso(p.paid_at),
                    "fee": str(p.fee) if p.fee is not None else None,
                    "fee_currency": p.fee_currency,
                    "environment": p.environment,
                }
                for p in s.scalars(
                    select(Payment)
                    .where(Payment.account_id == a.id, successful_payment_history_clause())
                    .order_by(Payment.paid_at.desc())
                )
            ]
            for payment in payments:
                order = s.get(Order, payment["order_id"])
                quote = s.get(Quote, order.quote_id) if order else None
                snapshot = quote.snapshot if quote else {}
                subscription = (
                    s.get(Subscription, order.subscription_id) if order and order.subscription_id else None
                )
                operation = (
                    s.scalar(select(ProviderOperation).where(ProviderOperation.order_id == order.id).limit(1))
                    if order
                    else None
                )
                payment.update(
                    {
                        "product_name": snapshot.get("product_name"),
                        "action": snapshot.get("action"),
                        "period_days": snapshot.get("period_days")
                        or snapshot.get("basis", {}).get("period_days"),
                        "devices": snapshot.get("devices_limit"),
                        "payment_method": operation.request.get("payment_method") if operation else None,
                        "provisioning_status": subscription.status if subscription else None,
                    }
                )
            return {
                "account": account_dto(a),
                "subscriptions": subs,
                "subscription_deletion_notices": deletion_notices,
                "archived_subscription_agreements": archived_agreements,
                "payments": payments,
                "bonus_balance": str(a.bonus_balance),
                "active_promo": self.promos.status_in_transaction(s, a),
                "referral": self._referral_info(s, a),
                "bonus_reserved": str(a.bonus_reserved),
                **credit_dto(a),
                "referrer_id": str(a.referrer_id) if a.referrer_id else None,
                "recurring_enrollment_enabled": bool(self._setting(s, "recurring", {}).get("enabled", False)),
            }

    def enroll(self, account_id, sid):
        with self.db.transaction() as s:
            a = self._account(s, account_id, True)
            self._sub(s, a.id, sid, True)
            self._assert_new_operations(s)
            self.legal.require_current(s, a)
            if not self._setting(s, "recurring", {}).get("enabled", False):
                raise DomainError("recurring_disabled", "Новые подключения автопродления отключены.")
            raise DomainError(
                "provider_capability_unavailable",
                "Автопродление ожидает подтверждения платёжного провайдера.",
                503,
            )

    def cancel_agreement(self, account_id, sid):
        with self.db.transaction() as s:
            a = self._account(s, account_id, True)
            self._sub(s, a.id, sid, True)
            ag = s.scalar(
                select(Agreement)
                .where(Agreement.subscription_id == sid)
                .order_by((Agreement.state == "stopped").asc(), Agreement.created_at.desc())
                .limit(1)
                .with_for_update()
            )
            if not ag:
                raise missing()
            if self.live:
                self.runtime_policy.require_current(s, "existing_recurring_cancel")
            if ag.state != "stopped":
                ag.state = "stop_pending"
                if not s.scalar(select(Job).where(Job.key == "cancel:" + ag.id)):
                    s.add(
                        Job(
                            key="cancel:" + ag.id,
                            kind="cancel_agreement",
                            account_id=a.id,
                            payload={"agreement_id": ag.id},
                        )
                    )
            return {"id": ag.id, "state": ag.state}

    def revoke_device(self, account_id, sid, device_id):
        with self.db.transaction() as s:
            a = self._account(s, account_id, True)
            sub = self._sub(s, a.id, sid, True)
            d = s.scalar(select(Device).where(Device.id == device_id, Device.subscription_id == sid))
            if not d:
                raise missing()
            from .moderation import DeviceModeration

            moderation = DeviceModeration(self)
            moderation.assert_unlink_allowed(a)
            if d.revoked:
                return {"status": "revoked"}
            self._assert_new_operations(s)
            self.legal.require_current(s, a)
            self._assert_subscription_operations(s, sub)
            if self.panel_provider:
                hwid = self.device_bindings.get(device_id)
                binding = self.panel_provider.bindings.get(sid)
                if not binding or hwid not in self.panel_provider.hwids:
                    raise DomainError("panel_device_denied", "Устройство не разрешено тестовой панели.", 503)
                key = "device-revoke:" + d.id
                if not s.scalar(select(Job).where(Job.key == key)):
                    s.add(
                        Job(
                            key=key,
                            kind="device_revoke",
                            account_id=a.id,
                            payload={
                                "subscription_id": sid,
                                "device_id": d.id,
                                "hwid": hwid,
                                "environment": "local_test",
                            },
                        )
                    )
                return {"status": "revoke_pending"}
            if not self.local_test:
                raise DomainError("panel_unavailable", "Панель временно недоступна.", 503, True)
            moderation.complete(s, a.id, sid, device_id)
            return {"status": "revoked"}

    def run_jobs(self, limit=50):
        self.apply_segment_boundaries()
        completed = self.provisioning.run_jobs(limit) if self.panel_provider else 0
        for _ in range(limit):
            with self.db.transaction() as s:
                query = select(Job).where(
                    Job.state == "pending",
                    Job.available_at <= now(),
                    Job.kind.not_in(
                        [
                            "owner_device_unlink",
                            "daily_backup",
                            "credential_rotate",
                            "utm_start_event",
                            "subscription_cleanup",
                            "panel_route_sync",
                        ]
                    ),
                )
                query = query.where(
                    (Job.kind != "broadcast") | Job.payload["source"].as_string().is_distinct_from("unified")
                )
                if self.panel_provider:
                    query = query.where(Job.kind.not_in(["provision", "device_revoke"]))
                j = s.scalar(query.order_by(Job.id).with_for_update(skip_locked=True).limit(1))
                if not j:
                    break
                j.attempts += 1
                if j.kind == "notification" and self.public_settings is not None:
                    from .notifications import NotificationService

                    NotificationService(self.db, self.public_settings).emit_in(
                        s,
                        j.account_id,
                        "payment:" + j.payload["order_id"],
                        "payment_received",
                        "Оплата получена",
                        "Готовим доступ. Следите за состоянием в личном кабинете.",
                        target="/#history",
                    )
                    j.state = "done"
                    completed += 1
                elif (
                    j.kind == "provision" and self.local_test and j.payload.get("environment") == "local_test"
                ):
                    sub = s.get(Subscription, j.payload["subscription_id"])
                    from .retention import fenced_in

                    if fenced_in(s, sub):
                        j.state, j.last_error = "review", "subscription_cleanup_pending"
                        continue
                    if sub.version == j.payload["version"]:
                        sub.status = j.payload.get("desired_subscription_status") or (
                            "suspended" if j.payload.get("panel_status") == "DISABLED" else "active"
                        )
                    j.state = "done"
                    completed += 1
                elif j.kind == "broadcast" and j.payload.get("delivery") == "local_artifact_only":
                    import json
                    from pathlib import Path

                    recipients = j.payload.get("recipients", [])
                    if (
                        not self.local_test
                        or j.payload.get("environment") != "local_test"
                        or not recipients
                        or not set(recipients) <= self.telegram_notification_allowlist
                        or not self.notification_dir
                    ):
                        j.state, j.last_error = "blocked", "recipient_denied"
                        continue
                    folder = Path(self.notification_dir)
                    folder.mkdir(parents=True, exist_ok=True)
                    (folder / (j.id + ".json")).write_text(
                        json.dumps(
                            {
                                "environment": "local_test",
                                "channel": "telegram",
                                "recipients": recipients,
                                "content": j.payload,
                            },
                            ensure_ascii=False,
                        ),
                        encoding="utf-8",
                    )
                    j.payload = j.payload | {"delivered_recipients": recipients}
                    j.state = "done"
                    completed += 1
                elif j.kind in {"notification", "broadcast"} and self.mail_transport:
                    from sunrise_core.errors import DomainError

                    from .instance_config import configured_instance
                    from .mail import render_mail_text

                    account = s.get(Account, j.account_id)
                    recipients = j.payload.get(
                        "recipients", [account.email] if account and account.email else []
                    )
                    if not recipients or not set(recipients) <= self.notification_allowlist:
                        j.state, j.last_error = "blocked", "recipient_denied"
                        continue
                    try:
                        delivered = set(j.payload.get("delivered_recipients", []))
                        for recipient in recipients:
                            if recipient not in delivered:
                                self.mail_transport.deliver(
                                    recipient,
                                    "Сообщение " + configured_instance(self.public_settings).app_name,
                                    "Оплата получена. Состояние подписки доступно в личном кабинете."
                                    if j.kind == "notification"
                                    else render_mail_text(j.payload.get("nodes", [])),
                                    j.id,
                                )
                                delivered.add(recipient)
                                j.payload = j.payload | {"delivered_recipients": sorted(delivered)}
                        j.state = "done"
                        completed += 1
                    except DomainError as exc:
                        j.last_error = exc.code
                        if exc.code == "mail_delivery_unknown":
                            j.state = "review"
                        else:
                            j.available_at = now() + timedelta(minutes=10)
                elif j.kind in {"notification", "broadcast"} and self.local_test and self.notification_dir:
                    import json
                    from pathlib import Path

                    account = s.get(Account, j.account_id)
                    recipients = j.payload.get(
                        "recipients", [account.email] if account and account.email else []
                    )
                    if not recipients or not set(recipients) <= self.notification_allowlist:
                        j.state = "blocked"
                        j.last_error = "recipient_denied"
                        continue
                    folder = Path(self.notification_dir)
                    folder.mkdir(parents=True, exist_ok=True)
                    # Stable file ID is idempotent local delivery evidence, never a claim of external sending.
                    (folder / (j.id + ".json")).write_text(
                        json.dumps(
                            {"environment": "local_test", "recipients": recipients, "content": j.payload},
                            ensure_ascii=False,
                        ),
                        encoding="utf-8",
                    )
                    j.state = "done"
                    completed += 1
                elif j.kind == "cancel_agreement" and self.local_test:
                    ag = s.get(Agreement, j.payload["agreement_id"])
                    sub = s.get(Subscription, ag.subscription_id) if ag else None
                    if (
                        ag
                        and sub.environment == "local_test"
                        and (ag.provider_id or "").startswith("synthetic-")
                    ):
                        ag.state = "stopped"
                        j.state = "done"
                        completed += 1
                    else:
                        j.last_error = "provider_unavailable"
                        j.available_at = now() + timedelta(minutes=10)
                else:
                    j.last_error = "provider_unavailable"
                    j.available_at = now() + timedelta(minutes=min(60, 2 ** min(j.attempts, 6)))
        return completed

    def _has_purchase_history(self, s, account_id, exclude_order_id=None):
        payments = select(Payment.id).where(Payment.account_id == account_id)
        orders = select(Order.id).where(
            Order.account_id == account_id, Order.status.in_(["paid", "refund_review"])
        )
        if exclude_order_id:
            payments = payments.where(Payment.order_id != exclude_order_id)
            orders = orders.where(Order.id != exclude_order_id)
        return bool(
            s.scalar(payments.limit(1))
            or s.scalar(orders.limit(1))
            or s.scalar(select(LegacyPurchase.id).where(LegacyPurchase.account_id == account_id).limit(1))
        )

    def _promo(self, s, account_id, code, action, exclude_order_id=None):
        from datetime import datetime

        code = code.strip().upper()
        archive = s.get(Setting, "promo_archive")
        if archive and code in archive.value:
            raise DomainError("promo_invalid", "Промокод удалён.", 400)
        if self.live:
            from .promos import legacy_usage_in

            legacy = legacy_usage_in(s, self, account_id, code, exclude_order_id=exclude_order_id)
        else:
            legacy = None
        promo = next(
            (
                x
                for x in self._setting(s, "discounts", {}).get("promos", [])
                if x["code"] == code and x.get("enabled")
            ),
            None,
        )
        if legacy and legacy.grant_terms:
            promo = dict(legacy.grant_terms)
        if not promo:
            raise DomainError("promo_invalid", "Промокод недоступен.", 400)
        for name, comparison in [("starts_at", lambda x: now() < x), ("ends_at", lambda x: now() >= x)]:
            if promo.get(name):
                date = datetime.fromisoformat(promo[name].replace("Z", "+00:00"))
                if date.tzinfo is None or comparison(date):
                    raise DomainError("promo_invalid", "Срок действия промокода не подходит.", 400)
        audience = promo.get("audience", "all")
        previous = self._has_purchase_history(s, account_id, exclude_order_id)
        if (audience == "first_purchase" and previous) or (
            audience == "renewal" and action is not None and action != "renew"
        ):
            raise DomainError("promo_audience", "Промокод не подходит для этого заказа.", 400)
        query = (
            select(func.count())
            .select_from(PromoUse)
            .where(PromoUse.code == code, PromoUse.state != "released")
        )
        if exclude_order_id:
            query = query.where(PromoUse.order_id != exclude_order_id)
        claims = select(func.count()).select_from(PromoClaim).where(PromoClaim.code == code)
        if legacy:
            used = legacy.account_count + int(legacy.account_used and legacy.grant_context is None)
            total = legacy.baseline + legacy.extra_global_count
        else:
            used = s.scalar(query.where(PromoUse.account_id == account_id))
            used += s.scalar(claims.where(PromoClaim.account_id == account_id))
            total = s.scalar(query) + s.scalar(claims) if promo.get("total_limit") else 0
        if used >= min(promo.get("per_account_limit", 1), 1):
            raise DomainError("promo_used", "Промокод уже использован или зарезервирован.")
        if (
            not (legacy and legacy.grant_context)
            and promo.get("total_limit")
            and total >= promo["total_limit"]
        ):
            raise DomainError("promo_used", "Лимит промокода исчерпан.")
        return (
            promo | {"legacy_promo_grant": legacy.grant_context} if legacy and legacy.grant_context else promo
        )

    def apply_segment_boundaries(self):
        with self.db.transaction() as s:
            moment = now()
            query = select(Subscription, Segment)
            query = query.join(Segment, Segment.subscription_id == Subscription.id).where(
                Segment.starts_at <= moment,
                Segment.ends_at > moment,
                Segment.kind != "refunded",
            )
            if self.live:
                try:
                    self.runtime_policy.require_current(s, "financial_apply")
                    self.runtime_policy.require_current(s, "panel_mutation")
                except DomainError as failure:
                    if failure.code != "production_stage_denied":
                        raise
                    return
                hints = s.execute(query).all()
                owners = self._lock_accounts_in(s, {sub.account_id for sub, _ in hints})
                query = query.where(Subscription.id.in_([sub.id for sub, _ in hints]))
                rows = s.execute(
                    query.with_for_update(of=Subscription).execution_options(populate_existing=True)
                ).all()
                if any(sub.account_id not in set(owners.values()) for sub, _ in rows):
                    raise DomainError("account_merge_busy", "Подписки изменились. Повторите проверку.", 409)
            else:
                rows = s.execute(query.with_for_update(of=Subscription, skip_locked=True)).all()
            current_segments = {
                (payload["subscription_id"], payload["version"]): payload.get("active_segment_id")
                for payload in s.scalars(
                    select(Job.payload).where(
                        Job.kind == "provision",
                        Job.payload["subscription_id"].as_string().in_([sub.id for sub, _ in rows]),
                    )
                )
            }
            for sub, segment in rows:
                from .retention import fenced_in

                if fenced_in(s, sub):
                    continue
                effective_current = panel_entitlements.current_segment(s, sub, moment)
                if not effective_current or segment.id != effective_current.id:
                    continue
                marker_row = s.get(Setting, panel_entitlements.override_key(sub.id))
                if panel_entitlements.override(s, sub, moment) and sub.status not in {
                    "revoked",
                    "revocation_pending",
                }:
                    continue
                if self.live:
                    try:
                        self.runtime_policy.require_current(s, "financial_apply")
                        self.runtime_policy.require_current(s, "panel_mutation")
                    except DomainError as failure:
                        if failure.code != "production_stage_denied":
                            raise
                        return
                if marker_row:
                    s.delete(marker_row)
                if (
                    sub.product_id == segment.product_id
                    and sub.devices_limit == segment.devices
                    and sub.status not in {"revoked", "revocation_pending"}
                    and current_segments.get((sub.id, sub.version)) == segment.id
                ):
                    continue
                prior_status = panel_entitlements.local_status(s, sub)
                sub.product_id = segment.product_id
                sub.devices_limit = segment.devices
                sub.version += 1
                sub.status = "provisioning_pending"
                key = "segment:" + segment.id + ":" + str(sub.version)
                s.add(
                    Job(
                        key=key,
                        kind="provision",
                        account_id=sub.account_id,
                        payload=self._provision_payload(
                            s,
                            sub,
                            segment.price_basis.get("panel_mappings", {}),
                            moment=moment,
                            prior_status=prior_status,
                            origin="paid_boundary",
                        ),
                    )
                )

    def analytics(
        self,
        start=None,
        end=None,
        timezone="Europe/Moscow",
        currency=None,
        source=None,
        product_id=None,
        interval="day",
        mode="cash",
    ):
        from zoneinfo import ZoneInfo

        zone = ZoneInfo(timezone)
        end = end or now()
        start = start or end - timedelta(days=30)
        if (
            start.tzinfo is None
            or end.tzinfo is None
            or start >= end
            or interval not in {"day", "week", "month"}
            or mode not in {"cash", "cohort"}
        ):
            raise DomainError("range_invalid", "Проверьте период, группировку и часовой пояс.", 400)

        def bucket(value):
            date = value.astimezone(zone).date()
            if interval == "week":
                date -= timedelta(days=date.weekday())
            elif interval == "month":
                date = date.replace(day=1)
            return date.isoformat()

        with self.db.transaction() as s:
            from .db import RecurringAttempt, RecurringCycle, Settlement

            all_records = list(
                s.execute(
                    select(Payment, Order, Quote)
                    .join(Order, Order.id == Payment.order_id)
                    .join(Quote, Quote.id == Order.quote_id)
                    .where(Payment.environment == "live")
                )
            )

            def matches(record):
                p, o, q = record
                return (
                    (currency is None or p.currency == currency)
                    and (source is None or o.source == source)
                    and (product_id is None or q.snapshot["product_id"] == product_id)
                )

            records = [r for r in all_records if matches(r)]
            selected = [r for r in records if start <= r[0].paid_at < end]
            previous_records = [r for r in records if start - (end - start) <= r[0].paid_at < start]
            currencies = {}
            previous = {}
            refunds = {}
            fees = {}
            series = {}
            rows = []
            composition = {}
            net = D(0)
            net_count = 0
            unknown = 0
            settlement_series = {}
            first = {}
            for p, _o, _q in all_records:
                first[p.account_id] = min(first.get(p.account_id, p.paid_at), p.paid_at)
            new_accounts = set()
            repeat_accounts = set()
            service_orders = set()
            renewals = 0
            bonus_spent = D(0)
            credit_spent = {}
            service_value = {}
            service_rows = []
            for p, _o, _q in previous_records:
                previous[p.currency] = previous.get(p.currency, D(0)) + p.amount
            for p, o, q in selected:
                currencies[p.currency] = currencies.get(p.currency, D(0)) + p.amount
                key = (bucket(p.paid_at), p.currency)
                entry = series.setdefault(key, {"payments": D(0), "refunds": D(0)})
                entry["payments"] += p.amount
                if p.fee is None or p.fee_currency is None:
                    unknown += 1
                has_events = (
                    s.scalar(select(Settlement.id).where(Settlement.payment_id == p.id).limit(1)) is not None
                )
                if not has_events:
                    if p.fee is not None and p.fee_currency:
                        fees[p.fee_currency] = fees.get(p.fee_currency, D(0)) + p.fee
                    if p.net_usdt is not None:
                        net += p.net_usdt
                        net_count += 1
                rows.append(
                    {
                        "id": p.id,
                        "payment_id": p.id,
                        "order_id": o.id,
                        "date": iso(p.paid_at),
                        "kind": "payment",
                        "amount": str(p.amount),
                        "currency": p.currency,
                        "fee": str(p.fee) if not has_events and p.fee is not None else None,
                        "fee_currency": p.fee_currency if not has_events else None,
                        "net_usdt": str(p.net_usdt) if not has_events and p.net_usdt is not None else None,
                        "source": o.source,
                        "product_id": q.snapshot["product_id"],
                        "canonical_account_id": str(p.account_id),
                        "original_account_id": str(p.original_account_id) if p.original_account_id else None,
                        "provenance": "provider_confirmed",
                        "time_source": "provider_confirmed" if has_events else "legacy_paid_date",
                    }
                )
                if first[p.account_id] >= start:
                    new_accounts.add(p.account_id)
                else:
                    repeat_accounts.add(p.account_id)
                snapshot = q.snapshot
                ckey = (
                    snapshot["product_id"],
                    snapshot["basis"].get("period_days"),
                    o.source,
                    p.currency,
                    snapshot["action"],
                    snapshot["devices_limit"],
                    p.provider,
                )
                sale = composition.setdefault(
                    ckey,
                    {
                        "amount": D(0),
                        "service_value": D(0),
                        "count": 0,
                        "label": snapshot.get("product_name", snapshot["product_id"]),
                    },
                )
                sale["amount"] += p.amount
            payments_by_order = {}
            for payment, _order, _quote in sorted(records, key=lambda row: (row[0].paid_at, row[0].id)):
                payments_by_order.setdefault(payment.order_id, payment)
            for order, quote in s.execute(
                select(Order, Quote)
                .join(Quote, Quote.id == Order.quote_id)
                .where(Order.environment == "live")
            ):
                snapshot = quote.snapshot
                if (
                    currency is not None
                    and snapshot["currency"] != currency
                    or source is not None
                    and order.source != source
                    or product_id is not None
                    and snapshot["product_id"] != product_id
                ):
                    continue
                payment = payments_by_order.get(order.id)
                stamp = order.fulfilled_at
                time_source = "fulfilled_at"
                segment_proof = s.scalar(select(Segment.id).where(Segment.order_id == order.id).limit(1))
                if stamp is None and segment_proof and order.status in {"paid", "refund_review"}:
                    stamp = payment.paid_at if payment else order.created_at
                    time_source = "legacy_paid_date" if payment else "legacy_order_created_at"
                if stamp is None or not start <= stamp < end or order.id in service_orders:
                    continue
                service_orders.add(order.id)
                method = (
                    payment.provider
                    if payment
                    else ("cash_credit" if D(snapshot.get("credit_amount", "0")) else "bonus")
                )
                ckey = (
                    snapshot["product_id"],
                    snapshot["basis"].get("period_days"),
                    order.source,
                    snapshot["currency"],
                    snapshot["action"],
                    snapshot["devices_limit"],
                    method,
                )
                sale = composition.setdefault(
                    ckey,
                    {
                        "amount": D(0),
                        "service_value": D(0),
                        "count": 0,
                        "label": snapshot.get("product_name", snapshot["product_id"]),
                    },
                )
                value = D(snapshot["base_amount"]) - D(snapshot["discount_amount"])
                cash_credit = D(snapshot.get("credit_amount", "0"))
                sale["count"] += 1
                sale["service_value"] += value
                service_value[snapshot["currency"]] = service_value.get(snapshot["currency"], D(0)) + value
                if cash_credit:
                    credit_spent[snapshot["currency"]] = (
                        credit_spent.get(snapshot["currency"], D(0)) + cash_credit
                    )
                bonus_spent += D(snapshot["bonus_amount"])
                renewals += snapshot["action"] == "renew"
                service_rows.append(
                    {
                        "order_id": order.id,
                        "date": iso(stamp),
                        "time_source": time_source,
                        "product_id": snapshot["product_id"],
                        "currency": snapshot["currency"],
                        "service_value": str(value),
                        "bonus_amount": snapshot["bonus_amount"],
                        "credit_amount": str(cash_credit),
                        "external_amount": snapshot["total"],
                        "action": snapshot["action"],
                        "source": order.source,
                        "canonical_account_id": str(order.account_id),
                    }
                )
            record_by_id = {r[0].id: r for r in records}
            selected_ids = {r[0].id for r in selected}
            for refund in s.scalars(select(Refund)):
                record = record_by_id.get(refund.payment_id)
                if not record:
                    continue
                p, o, q = record
                if mode == "cash" and not start <= refund.occurred_at < end:
                    continue
                if mode == "cohort" and refund.payment_id not in selected_ids:
                    continue
                refunds[p.currency] = refunds.get(p.currency, D(0)) + refund.amount
                key = (bucket(refund.occurred_at if mode == "cash" else p.paid_at), p.currency)
                series.setdefault(key, {"payments": D(0), "refunds": D(0)})["refunds"] += refund.amount
                rows.append(
                    {
                        "id": refund.id,
                        "payment_id": p.id,
                        "order_id": o.id,
                        "date": iso(refund.occurred_at),
                        "kind": "refund",
                        "amount": str(-refund.amount),
                        "currency": p.currency,
                        "fee": None,
                        "fee_currency": None,
                        "net_usdt": None,
                        "source": o.source,
                        "product_id": q.snapshot["product_id"],
                        "canonical_account_id": str(p.account_id),
                        "original_account_id": str(p.original_account_id) if p.original_account_id else None,
                        "provenance": "provider_confirmed",
                        "time_source": "provider_confirmed",
                    }
                )
            for settlement in s.scalars(select(Settlement)):
                record = record_by_id.get(settlement.payment_id)
                if not record:
                    continue
                p, o, q = record
                if mode == "cash" and not start <= settlement.occurred_at < end:
                    continue
                if mode == "cohort" and p.id not in selected_ids:
                    continue
                if settlement.fee is not None:
                    fees[settlement.fee_currency] = fees.get(settlement.fee_currency, D(0)) + settlement.fee
                if settlement.net_usdt is not None:
                    net += settlement.net_usdt
                    net_count += 1
                stamp = bucket(settlement.occurred_at if mode == "cash" else p.paid_at)
                key = (stamp, settlement.fee_currency or "USDT")
                series_entry = settlement_series.setdefault(
                    key, {"fee": D(0), "net_usdt": D(0), "net_known": False}
                )
                if settlement.fee is not None:
                    series_entry["fee"] += settlement.fee
                if settlement.net_usdt is not None:
                    series_entry["net_usdt"] += settlement.net_usdt
                    series_entry["net_known"] = True
                rows.append(
                    {
                        "id": settlement.id,
                        "payment_id": p.id,
                        "order_id": o.id,
                        "date": iso(settlement.occurred_at),
                        "kind": "settlement",
                        "amount": "0.00",
                        "currency": p.currency,
                        "fee": str(settlement.fee) if settlement.fee is not None else None,
                        "fee_currency": settlement.fee_currency,
                        "net_usdt": str(settlement.net_usdt) if settlement.net_usdt is not None else None,
                        "source": o.source,
                        "product_id": q.snapshot["product_id"],
                        "canonical_account_id": str(p.account_id),
                        "original_account_id": str(p.original_account_id) if p.original_account_id else None,
                        "provenance": settlement.source,
                        "time_source": settlement.time_source,
                    }
                )
            active = s.scalar(
                select(func.count(func.distinct(Subscription.id)))
                .join(Segment, Segment.subscription_id == Subscription.id)
                .where(
                    Subscription.environment == "live",
                    Segment.kind == "paid",
                    Segment.starts_at <= now(),
                    Segment.ends_at > now(),
                )
            )
            provision = {}
            for state, count in s.execute(
                select(Subscription.status, func.count())
                .where(Subscription.environment == "live")
                .group_by(Subscription.status)
            ):
                provision[state] = count
            cycles = {}
            cycle_facts = {}

            def recurring_scope(agreement, subscription):
                return (
                    subscription.environment == "live"
                    and (product_id is None or subscription.product_id == product_id)
                    and (currency is None or agreement.currency == currency)
                    and source in {None, "recurring"}
                )

            for attempt, agreement, subscription in s.execute(
                select(RecurringAttempt, Agreement, Subscription)
                .join(Agreement, Agreement.id == RecurringAttempt.agreement_id)
                .join(Subscription, Subscription.id == Agreement.subscription_id)
            ):
                if not recurring_scope(agreement, subscription):
                    continue
                stamp = attempt.occurred_at or attempt.created_at
                key = (agreement.id, attempt.cycle_number)
                entry = cycle_facts.setdefault(
                    key,
                    {
                        "first": stamp,
                        "latest": stamp,
                        "state": attempt.state,
                        "attempts": 0,
                        "time_source": "provider_confirmed" if attempt.occurred_at else "received_at",
                    },
                )
                entry["first"] = min(entry["first"], stamp)
                entry["attempts"] += 1
                if stamp >= entry["latest"]:
                    entry["latest"] = stamp
                    entry["state"] = attempt.state
            for cycle, agreement, subscription, order in s.execute(
                select(RecurringCycle, Agreement, Subscription, Order)
                .join(Agreement, Agreement.id == RecurringCycle.agreement_id)
                .join(Subscription, Subscription.id == Agreement.subscription_id)
                .join(Order, Order.id == RecurringCycle.order_id)
            ):
                if not recurring_scope(agreement, subscription):
                    continue
                key = (agreement.id, cycle.cycle_number)
                payment = s.scalar(
                    select(Payment)
                    .where(Payment.order_id == order.id, Payment.environment == "live")
                    .order_by(Payment.paid_at)
                    .limit(1)
                )
                stamp = payment.paid_at if payment else order.created_at
                entry = cycle_facts.setdefault(
                    key,
                    {
                        "first": stamp,
                        "latest": stamp,
                        "state": cycle.state,
                        "attempts": 0,
                        "time_source": "provider_confirmed" if payment else "received_at",
                    },
                )
                if cycle.state == "paid":
                    entry["state"] = "paid"
                elif cycle.state == "review":
                    entry["state"] = "review"
            cycle_rows = []
            cycle_series = {}
            attempt_count = 0
            for (agreement_id, number), entry in cycle_facts.items():
                if not start <= entry["first"] < end:
                    continue
                state = entry["state"]
                if state in {"failed", "declined", "error", "canceled"}:
                    state = "declined"
                elif state in {"payed", "paid"}:
                    state = "paid" if state == "paid" else "review"
                elif state not in {"paid", "review", "declined"}:
                    state = "pending"
                cycles[state] = cycles.get(state, 0) + 1
                attempt_count += entry["attempts"]
                series_key = (bucket(entry["first"]), state)
                cycle_series[series_key] = cycle_series.get(series_key, 0) + 1
                cycle_rows.append(
                    {
                        "agreement_id": agreement_id,
                        "cycle_number": number,
                        "state": state,
                        "attempts": entry["attempts"],
                        "first_observed_at": iso(entry["first"]),
                        "latest_observed_at": iso(entry["latest"]),
                        "time_source": entry["time_source"],
                    }
                )
            return {
                "from": iso(start),
                "to": iso(end),
                "timezone": timezone,
                "interval": interval,
                "mode": mode,
                "currencies": {k: str(v) for k, v in currencies.items()},
                "refunds": {k: str(v) for k, v in refunds.items()},
                "net_after_refunds": {
                    k: str(currencies.get(k, D(0)) - refunds.get(k, D(0)))
                    for k in currencies.keys() | refunds.keys()
                },
                "previous": {k: str(v) for k, v in previous.items()},
                "confirmed_fees": {k: str(v) for k, v in fees.items()},
                "net_usdt": str(net) if net_count else None,
                "settlement_count": net_count,
                "unknown_fee_count": unknown,
                "paid_count": len(selected),
                "provisioning_pending": provision.get("provisioning_pending", 0),
                "active_paid_subscriptions": active,
                "new_payers": len(new_accounts),
                "returning_payers": len(repeat_accounts),
                "renewals": renewals,
                "bonus_spent": str(bonus_spent),
                "credit_spent": {key: str(value) for key, value in credit_spent.items()},
                "service_value": {key: str(value) for key, value in service_value.items()},
                "service_count": len(service_orders),
                "service_rows": sorted(service_rows, key=lambda row: (row["date"], row["order_id"])),
                "series": [
                    {
                        "date": k[0],
                        "currency": k[1],
                        "amount": str(v["payments"]),
                        "refunds": str(v["refunds"]),
                        "net": str(v["payments"] - v["refunds"]),
                    }
                    for k, v in sorted(series.items())
                ],
                "sales_composition": [
                    {
                        "product_id": k[0],
                        "label": v["label"],
                        "period_days": k[1],
                        "source": k[2],
                        "currency": k[3],
                        "action": k[4],
                        "devices": k[5],
                        "method": k[6],
                        "amount": str(v["amount"]),
                        "service_value": str(v["service_value"]),
                        "count": v["count"],
                    }
                    for k, v in composition.items()
                ],
                "settlement_series": [
                    {
                        "date": k[0],
                        "fee_currency": k[1],
                        "fee": str(v["fee"]),
                        "net_usdt": str(v["net_usdt"]) if v["net_known"] else None,
                    }
                    for k, v in sorted(settlement_series.items())
                ],
                "recurring_cycles": [{"state": k, "count": v} for k, v in cycles.items()],
                "recurring_due_count": sum(cycles.values()),
                "recurring_paid_count": cycles.get("paid", 0),
                "recurring_attempt_count": attempt_count,
                "recurring_success_percent": str(D(cycles.get("paid", 0)) * 100 / D(sum(cycles.values())))
                if cycles
                else None,
                "recurring_rows": cycle_rows,
                "recurring_series": [
                    {"date": k[0], "state": k[1], "count": v} for k, v in sorted(cycle_series.items())
                ],
                "recurring_observation": "first_observed_cycle_date",
                "provisioning": [{"state": k, "count": v} for k, v in provision.items()],
                "rows": sorted(rows, key=lambda x: (x["date"], x["id"])),
                "environment": "live",
                "test_excluded": True,
                "historical_accounting_complete": False,
            }


def bootstrap(db, seed=None):
    """????? ????????????? ?????? ????????; ???????????? ?????? ?? ????????????????."""
    from .bootstrap_config import load_bootstrap_seed

    with db.transaction() as session:
        if session.scalar(select(func.count()).select_from(Product)):
            return
        configured = load_bootstrap_seed(seed)
        session.add_all(Product(**item.model_dump()) for item in configured.products)
        for key, value in configured.settings.model_dump().items():
            if session.get(Setting, key) is None:
                session.add(Setting(key=key, value=value))
