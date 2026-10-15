import hashlib
import json
import re
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy import func, select
from sunrise_core.errors import DomainError, missing

from . import panel_entitlements
from .db import (
    Account,
    Alias,
    Audit,
    Job,
    Order,
    Payment,
    Product,
    PromoClaim,
    PromoUse,
    Quote,
    Segment,
    Setting,
    Subscription,
    now,
    uid,
)


def validate_promo(values):
    promo = {
        "reward_type": "percent",
        "percent": "0",
        "enabled": True,
        "audience": "all",
        "per_account_limit": 1,
        "total_limit": None,
        "starts_at": None,
        "ends_at": None,
        "amount_rub": None,
        "reward_days": None,
        "product_id": None,
        "trial": False,
    } | values
    try:
        if not re.fullmatch(r"[A-Z0-9_-]{1,40}", promo["code"]):
            raise ValueError()
        if promo["reward_type"] not in {"percent", "fixed_rub", "free_days"}:
            raise ValueError()
        if type(promo["trial"]) is not bool or promo["trial"] and promo["reward_type"] != "free_days":
            raise ValueError()
        if (
            promo["audience"] not in {"all", "first_purchase", "renewal"}
            or type(promo["enabled"]) is not bool
        ):
            raise ValueError()
        if type(promo["per_account_limit"]) is not int or promo["per_account_limit"] != 1:
            raise ValueError()
        if promo["total_limit"] is not None and (
            type(promo["total_limit"]) is not int or promo["total_limit"] < 1
        ):
            raise ValueError()
        percent = Decimal(promo["percent"])
        if not percent.is_finite() or not 0 <= percent <= 100:
            raise ValueError()
        if promo["reward_type"] == "fixed_rub":
            amount = Decimal(promo["amount_rub"])
            if (
                not amount.is_finite()
                or amount <= 0
                or amount > Decimal("99999999.99")
                or amount.as_tuple().exponent < -2
            ):
                raise ValueError()
        if promo["reward_type"] == "free_days":
            if (
                type(promo["reward_days"]) is not int
                or not 1 <= promo["reward_days"] <= 3650
                or not promo["product_id"]
            ):
                raise ValueError()
        from datetime import datetime

        dates = [
            datetime.fromisoformat(promo[name].replace("Z", "+00:00")) if promo[name] else None
            for name in ("starts_at", "ends_at")
        ]
        if (
            any(date and date.tzinfo is None for date in dates)
            or dates[0]
            and dates[1]
            and dates[0] >= dates[1]
        ):
            raise ValueError()
    except (ValueError, TypeError, KeyError, ArithmeticError):
        raise DomainError(
            "discount_invalid", "Проверьте тип награды, сумму, срок и ограничения промокода.", 400
        ) from None
    return promo


def _canonical_sha256(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def normalize_legacy_percent_grant_terms(values):
    """Normalize approved CURRENT source-policy terms, never invent historical terms."""
    keys = {
        "code",
        "reward_type",
        "percent",
        "enabled",
        "audience",
        "per_account_limit",
        "total_limit",
        "starts_at",
        "ends_at",
        "amount_rub",
        "reward_days",
        "product_id",
        "trial",
    }
    if not isinstance(values, dict) or set(values) - keys:
        raise ValueError("legacy_promo_terms_invalid")
    values = dict(values)
    if type(values.get("total_limit")) is int and values["total_limit"] == 0:
        values["total_limit"] = None
    if type(values.get("percent", "0")) not in {str, int, Decimal}:
        raise ValueError("legacy_promo_terms_invalid")
    try:
        terms = validate_promo(values)
    except DomainError:
        raise ValueError("legacy_promo_terms_invalid") from None
    if terms["reward_type"] != "percent" or terms["trial"] or not terms["enabled"]:
        raise ValueError("legacy_promo_terms_invalid")
    terms["percent"] = format(Decimal(terms["percent"]).normalize(), "f")
    return terms


def legacy_promo_terms_sha256(terms):
    return _canonical_sha256(normalize_legacy_percent_grant_terms(terms))


def _source_identity(value):
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,18}", value) or int(value) >= 2**63:
        raise ValueError("legacy_promo_identity_invalid")
    return value


def legacy_pending_grant_id(scope_id, source_account_id, source_code):
    if not isinstance(scope_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", scope_id):
        raise ValueError("legacy_promo_scope_invalid")
    _source_identity(source_account_id)
    if not isinstance(source_code, str) or not source_code or len(source_code) > 200:
        raise ValueError("legacy_promo_source_code_invalid")
    return _canonical_sha256(
        {
            "version": 1,
            "scope_id": scope_id,
            "source_namespace": "legacy_shop_v1",
            "source_account_id": source_account_id,
            "source_code": source_code,
        }
    )


class LegacyPromoCodeUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    global_claimed: StrictInt | None = Field(ge=0)
    source_counter: StrictInt | None = Field(ge=0)
    used_account_ids: list[str]

    @field_validator("used_account_ids")
    @classmethod
    def identities(cls, values):
        if len(values) != len(set(values)):
            raise ValueError("legacy_promo_identity_duplicate")
        return [_source_identity(value) for value in values]

    @model_validator(mode="after")
    def count_bounds(self):
        if self.global_claimed is not None and self.global_claimed < max(
            self.source_counter or 0, len(self.used_account_ids)
        ):
            raise ValueError("legacy_promo_baseline_invalid")
        return self


class LegacyPromoGrantContext(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: StrictInt = Field(ge=1, le=1)
    scope_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    grant_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_account_id: str
    code: str = Field(pattern=r"^[A-Z0-9_-]{1,40}$")
    source_ref_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    terms_basis: Literal["migration_current_effective_source_policy"]
    terms_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("source_account_id")
    @classmethod
    def identity(cls, value):
        return _source_identity(value)


class LegacyPendingPromoGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_account_id: str
    code: str = Field(pattern=r"^[A-Z0-9_-]{1,40}$")
    source_ref_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    terms_basis: Literal["migration_current_effective_source_policy"]
    terms: dict
    terms_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("source_account_id")
    @classmethod
    def identity(cls, value):
        return _source_identity(value)

    @model_validator(mode="after")
    def effective_terms(self):
        normalized = normalize_legacy_percent_grant_terms(self.terms)
        if (
            normalized != self.terms
            or self.code != normalized["code"]
            or self.terms_sha256 != _canonical_sha256(normalized)
        ):
            raise ValueError("legacy_promo_terms_invalid")
        return self

    def context(self, scope_id, grant_id):
        return {
            "version": 1,
            "scope_id": scope_id,
            "grant_id": grant_id,
            "source_account_id": self.source_account_id,
            "code": self.code,
            "source_ref_sha256": self.source_ref_sha256,
            "evidence_sha256": self.evidence_sha256,
            "terms_basis": self.terms_basis,
            "terms_sha256": self.terms_sha256,
        }


class LegacyPromoUsageRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: StrictInt = Field(ge=1, le=1)
    scope_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    environment: Literal["live"]
    complete: StrictBool
    source_namespace: Literal["legacy_shop_v1"]
    source_snapshot_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    approved_at: str
    codes: dict[str, LegacyPromoCodeUsage]
    pending_grants: dict[str, LegacyPendingPromoGrant]

    @field_validator("approved_at")
    @classmethod
    def timestamp(cls, value):
        from .panel import PanelReview
        from .panel_v3 import instant

        try:
            instant(value)
        except PanelReview:
            raise ValueError("legacy_promo_timestamp_invalid") from None
        return value

    @model_validator(mode="after")
    def source_relations(self):
        if not self.complete or any(not re.fullmatch(r"[A-Z0-9_-]{1,40}", code) for code in self.codes):
            raise ValueError("legacy_promo_registry_invalid")
        refs, owners = set(), set()
        for gid, grant in self.pending_grants.items():
            usage = self.codes.get(grant.code)
            owner = (grant.source_account_id, grant.code)
            if (
                not re.fullmatch(r"[a-f0-9]{64}", gid)
                or usage is None
                or usage.global_claimed is None
                or grant.source_account_id not in usage.used_account_ids
                or grant.source_ref_sha256 in refs
                or owner in owners
            ):
                raise ValueError("legacy_promo_grant_invalid")
            refs.add(grant.source_ref_sha256)
            owners.add(owner)
        return self


@dataclass(frozen=True)
class LegacyPromoUsage:
    baseline: int
    account_used: bool
    account_count: int
    extra_global_count: int
    grant_context: dict | None = None
    grant_terms: dict | None = None


def _legacy_review():
    return DomainError(
        "legacy_promo_usage_review",
        "История промокода требует проверки поддержки.",
        409,
        next_action="support",
    )


def _same_legacy_context(proof, expected):
    return isinstance(proof, dict) and type(proof.get("version")) is int and proof == expected


def legacy_usage_in(session, commerce, account_id, code, *, snapshot=None, exclude_order_id=None):
    """Fresh root authority plus real modern rows; current Payment is not consumption."""
    commerce.runtime_policy.require_current(session)
    row = session.get(Setting, "legacy_promo_usage", populate_existing=True)
    try:
        registry = LegacyPromoUsageRegistry.model_validate(row.value if row else None)
        if registry.scope_id != commerce.runtime_policy.scope.scope_id:
            raise ValueError("legacy_promo_scope_invalid")
    except (ValidationError, ValueError, TypeError, DomainError):
        raise _legacy_review() from None
    identities = {account_id, *session.scalars(select(Alias.old_id).where(Alias.canonical_id == account_id))}
    usage = registry.codes.get(code)
    if usage and usage.global_claimed is None:
        raise _legacy_review()
    baseline = usage.global_claimed if usage else 0
    account_used = bool(usage and identities.intersection(int(value) for value in usage.used_account_ids))
    actor = session.get(Account, account_id, populate_existing=True)
    pending = actor.pending_promo if actor and isinstance(actor.pending_promo, dict) else {}
    captured = snapshot.get("legacy_promo_grant") if snapshot else None
    selected_id = captured.get("grant_id") if isinstance(captured, dict) else pending.get("legacy_grant_id")
    if selected_id is not None and (
        not isinstance(selected_id, str) or not re.fullmatch(r"[a-f0-9]{64}", selected_id)
    ):
        raise _legacy_review()
    selected = registry.pending_grants.get(selected_id) if selected_id else None
    context, terms = None, None
    if selected_id:
        if (
            selected is None
            or int(selected.source_account_id) not in identities
            or selected.code != code
            or snapshot is not None
            and captured is None
            or captured is not None
            and not _same_legacy_context(captured, selected.context(registry.scope_id, selected_id))
            or captured is None
            and (pending.get("id") != selected_id or pending.get("code") != code)
        ):
            raise _legacy_review()
        context = selected.context(registry.scope_id, selected_id)
        terms = dict(selected.terms)
    elif captured is not None:
        raise _legacy_review()
    rows = session.execute(
        select(PromoUse, Order, Quote)
        .join(Order, Order.id == PromoUse.order_id)
        .join(Quote, Quote.id == Order.quote_id)
        .where(PromoUse.code == code)
    ).all()
    paid_orders = (
        set(
            session.scalars(
                select(Payment.order_id).where(Payment.order_id.in_([order.id for _, order, _ in rows]))
            )
        )
        if rows
        else set()
    )
    account_count, extra_global = 0, 0
    for use, order, quote in rows:
        proof = quote.snapshot.get("legacy_promo_grant") if isinstance(quote.snapshot, dict) else None
        gid = proof.get("grant_id") if isinstance(proof, dict) else None
        grant = registry.pending_grants.get(gid) if gid else None
        alias = (
            session.get(Alias, int(grant.source_account_id))
            if grant and int(grant.source_account_id) != order.account_id
            else None
        )
        registered = bool(
            grant
            and _same_legacy_context(proof, grant.context(registry.scope_id, gid))
            and grant.code == code
            and order.environment == "live"
            and quote.account_id == order.account_id
            and (
                int(grant.source_account_id) == order.account_id
                or alias
                and alias.canonical_id == order.account_id
            )
        )
        own_domain = order.account_id in identities
        same_grant = selected_id is not None and gid == selected_id and own_domain
        if same_grant and not registered:
            raise _legacy_review()
        if order.id == exclude_order_id:
            if same_grant and (use.state == "used" or order.fulfilled_at is not None):
                raise DomainError("promo_used", "Льгота по этому заказу уже использована.", 409)
            continue
        if same_grant and (use.state == "used" or order.fulfilled_at is not None or order.id in paid_orders):
            raise DomainError("promo_used", "Промокод уже использован или ожидает проверки оплаты.", 409)
        if use.state != "released":
            account_count += int(own_domain)
            extra_global += int(not registered)
    claims = select(func.count()).select_from(PromoClaim).where(PromoClaim.code == code)
    account_count += session.scalar(claims.where(PromoClaim.account_id.in_(identities)))
    extra_global += session.scalar(claims)
    return LegacyPromoUsage(baseline, account_used, account_count, extra_global, context, terms)


def pending_dto(account):
    pending = account.pending_promo
    if not pending:
        return None
    return {
        "code": pending["code"],
        "percent": pending.get("percent", "0"),
        "state": "pending",
        "reward_type": pending.get("reward_type", "percent"),
        "amount_rub": pending.get("amount_rub"),
        "reward_days": pending.get("reward_days"),
        "product_id": pending.get("product_id"),
    }


def verified_gift_claim_in(session, job, sub, scope_id):
    payload = job.payload
    claim = session.get(PromoClaim, payload.get("promo_claim_id"))
    segment = session.get(Segment, payload.get("gift_segment_id"))
    if not claim or not segment or claim.account_id != sub.account_id or claim.segment_id != segment.id:
        return False
    terms = (segment.price_basis or {}).get("gift_terms")
    result = claim.result or {}
    return bool(
        isinstance(terms, dict)
        and terms
        == {
            "version": 1,
            "scope_id": scope_id,
            "environment": "live",
            "claim_id": claim.id,
            "code": claim.code,
            "subscription_id": sub.id,
            "product_id": segment.product_id,
            "devices": segment.devices,
            "starts_at": segment.starts_at.isoformat(),
            "ends_at": segment.ends_at.isoformat(),
            "internal_squad_uuid": segment.price_basis.get("panel_mappings", {}).get(segment.product_id),
        }
        and type(segment.devices) is int
        and 1 <= segment.devices <= 100
        and segment.subscription_id == sub.id
        and segment.kind == "promo_gift"
        and segment.paid_value == 0
        and segment.order_id is None
        and segment.ends_at > segment.starts_at
        and result.get("state") == "used"
        and result.get("code") == claim.code
        and result.get("subscription_id") == sub.id
        and result.get("expires_at") == segment.ends_at.isoformat()
        and type(result.get("reward_days")) is int
        and segment.ends_at - segment.starts_at == timedelta(days=result["reward_days"])
    )


class Promos:
    def __init__(self, commerce):
        self.commerce = commerce

    def _require_trial_in(self, session, account):
        consent = account.legacy_consent or {}
        identities = {
            account.id,
            *session.scalars(select(Alias.old_id).where(Alias.canonical_id == account.id)),
        }
        pending = [(consent, 0)]
        visited = {account.id}
        valid = True
        while pending and valid:
            snapshot, depth = pending.pop()
            if not isinstance(snapshot, dict) or depth > 32 or len(visited) > 256:
                valid = False
                break
            ordinary = {
                key: value
                for key, value in snapshot.items()
                if not isinstance(key, str) or not key.startswith("merged:")
            }
            if (ordinary or depth) and (
                type(ordinary.get("legacy_free_trial_used")) is not int
                or ordinary["legacy_free_trial_used"] != 0
            ):
                valid = False
                break
            for key, value in snapshot.items():
                if not isinstance(key, str) or not key.startswith("merged:"):
                    continue
                identifier = key[7:]
                if (
                    not re.fullmatch(r"[1-9][0-9]{0,18}", identifier)
                    or int(identifier) >= 2**63
                    or int(identifier) not in identities
                    or int(identifier) in visited
                ):
                    valid = False
                    break
                visited.add(int(identifier))
                pending.append((value, depth + 1))
        if not valid or visited != identities or self.commerce._has_purchase_history(session, account.id):
            raise DomainError(
                "trial_unavailable", "Пробный доступ уже использован или требует проверки поддержки.", 409
            )
        if any(
            session.get(Setting, "promo_trial:" + str(identifier)) is not None for identifier in identities
        ):
            raise DomainError("trial_unavailable", "Пробный доступ уже использован.", 409)

    def activate_in_transaction(self, session, account, code):
        normalized = code.strip().upper()
        archived = session.get(Setting, "promo_archive")
        if account.pending_promo and archived and account.pending_promo["code"] in archived.value:
            # Replace unavailable pending selection only; accepted orders retain
            # their independent immutable terms and financial reservations.
            account.pending_promo = None
        if account.pending_promo:
            if account.pending_promo["code"] == normalized:
                return pending_dto(account)
            raise DomainError("promo_pending", "Сначала используйте уже активированный промокод.", 409)
        promo = self.commerce._promo(session, account.id, normalized, None)
        account.pending_promo = {
            "id": uid(),
            "code": promo["code"],
            "percent": promo.get("percent", "0"),
            "reward_type": promo.get("reward_type", "percent"),
            "amount_rub": promo.get("amount_rub"),
            "reward_days": promo.get("reward_days"),
            "product_id": promo.get("product_id"),
            "state": "pending",
            "activated_at": now().isoformat(),
        }
        session.add(
            Audit(
                actor_id=account.id,
                action="promo_activate",
                target=normalized,
                reason="Пользователь активировал промокод",
                details={},
            )
        )
        return pending_dto(account)

    def activate(self, account_id, code):
        with self.commerce.db.transaction() as session:
            account = self.commerce._account(session, account_id, True)
            return self.activate_in_transaction(session, account, code)

    def active(self, account_id):
        with self.commerce.db.transaction() as session:
            return self.status_in_transaction(session, self.commerce._account(session, account_id))

    def targets_in_transaction(self, session, account, lock=False):
        from .subscription_archive import retired_local_fixture_clause

        query = select(Subscription).where(
            Subscription.account_id == account.id,
            Subscription.expires_at > now(),
            Subscription.status.not_in(["revoked", "expired"]),
            ~retired_local_fixture_clause(),
        )
        return list(session.scalars(query.with_for_update() if lock else query))

    def targets(self, account_id):
        with self.commerce.db.transaction() as session:
            account = self.commerce._account(session, account_id)
            return [sub.id for sub in self.targets_in_transaction(session, account)]

    def status_in_transaction(self, session, account):
        result = pending_dto(account)
        if result:
            result |= {"available": True, "unavailable_reason": None}
            try:
                self.commerce._promo(session, account.id, result["code"], None)
            except DomainError as exc:
                result |= {"available": False, "unavailable_reason": exc.message}
        return result

    def validate_fulfillment(self, session, account, order, snapshot):
        code = snapshot["promo_code"]
        terms = snapshot.get("promo_terms")
        if terms is None:
            terms = next(
                (
                    promo
                    for promo in self.commerce._setting(session, "discounts", {}).get("promos", [])
                    if promo["code"] == code
                ),
                {},
            )
        if self.commerce.live:
            usage = legacy_usage_in(
                session, self.commerce, account.id, code, snapshot=snapshot, exclude_order_id=order.id
            )
            if usage.grant_context:
                expected = {
                    "per_account_limit": 1,
                    "total_limit": usage.grant_terms.get("total_limit"),
                    "audience": usage.grant_terms.get("audience", "all"),
                }
                if terms != expected:
                    raise _legacy_review()
            account_count = usage.account_count + int(usage.account_used and usage.grant_context is None)
            total_count = usage.baseline + usage.extra_global_count
        else:
            usage = None
            uses = (
                select(func.count())
                .select_from(PromoUse)
                .where(PromoUse.code == code, PromoUse.state != "released", PromoUse.order_id != order.id)
            )
            claims = select(func.count()).select_from(PromoClaim).where(PromoClaim.code == code)
            account_count = session.scalar(uses.where(PromoUse.account_id == account.id)) + session.scalar(
                claims.where(PromoClaim.account_id == account.id)
            )
            total_count = session.scalar(uses) + session.scalar(claims)
        if (
            account_count >= min(terms.get("per_account_limit", 1), 1)
            or not (usage and usage.grant_context)
            and terms.get("total_limit")
            and total_count >= terms["total_limit"]
        ):
            raise DomainError("promo_used", "Промокод уже использован или зарезервирован.", 409)
        if terms.get("audience") == "first_purchase" and self.commerce._has_purchase_history(
            session, account.id, order.id
        ):
            raise DomainError("promo_audience", "Промокод подходит только для первой покупки.", 409)
        if terms.get("audience") == "renewal" and snapshot["action"] != "renew":
            raise DomainError("promo_audience", "Промокод доступен только при продлении.", 409)

    def claim(self, account_id, code, subscription_id=None):
        normalized = code.strip().upper()
        c = self.commerce
        with c.db.transaction() as session:
            account = c._account(session, account_id)
            existing = session.scalar(
                select(PromoClaim).where(PromoClaim.account_id == account.id, PromoClaim.code == normalized)
            )
            if existing:
                return existing.result
        sync_results = c._refresh_panel(account_id)
        with c.db.transaction() as session:
            account = c._account(session, account_id, True)
            existing = session.scalar(
                select(PromoClaim).where(PromoClaim.account_id == account.id, PromoClaim.code == normalized)
            )
            if existing:
                return existing.result
            pending = account.pending_promo
            if not pending or pending["code"] != normalized:
                raise DomainError("promo_pending_required", "Сначала активируйте этот промокод.", 409)
            c._assert_new_operations(session)
            c.legal.require_current(session, account)
            session.scalar(select(Setting).where(Setting.key == "discounts").with_for_update())
            promo = c._promo(session, account.id, normalized, None)
            if promo.get("reward_type", "percent") != "free_days":
                raise DomainError("promo_claim_invalid", "Этот промокод применяется при оплате.", 400)
            active = self.targets_in_transaction(session, account, True)
            if subscription_id:
                sub = next((sub for sub in active if sub.id == subscription_id), None)
                if not sub:
                    raise missing()
            elif len(active) > 1:
                raise DomainError("subscription_required", "Выберите подписку для подарочных дней.", 400)
            else:
                sub = active[0] if active else None
            c._promo(session, account.id, normalized, "renew" if sub else "purchase")
            if c.live:
                from .accounts import require_account_access_in
                from .retention import assert_legacy_operation_allowed_in

                require_account_access_in(session, account, c.runtime_policy)
                assert_legacy_operation_allowed_in(session, account.id, sub.id if sub else None)
                c.runtime_policy.require_current(session, "financial_apply")
                c.runtime_policy.require_current(session, "panel_mutation")
                if promo.get("trial") is True:
                    self._require_trial_in(session, account)
            moment = now()
            days = promo["reward_days"]
            if sub:
                c._assert_subscription_operations(session, sub)
                if panel_entitlements.bound(sub):
                    if sync_results.get(sub.id, {}).get("status") != "ok":
                        raise DomainError(
                            "panel_sync_required",
                            "Не удалось проверить условия подписки в панели. Обновите профиль.",
                            409,
                        )
                    panel_entitlements.require_pricing(session, sub)
                current = panel_entitlements.current_segment(session, sub, moment)
                if not current and panel_entitlements.override(session, sub, moment):
                    current = next(
                        (
                            item
                            for item in reversed(panel_entitlements.segments(session, sub))
                            if item.starts_at <= moment
                        ),
                        None,
                    )
                if not current:
                    raise DomainError("valuation_unknown", "Права подписки требуют проверки владельцем.", 409)
                product = session.get(
                    Product, sub.product_id if panel_entitlements.bound(sub) else current.product_id
                )
                basis = {
                    key: value
                    for key, value in current.price_basis.items()
                    if key in {"panel_mappings", "panel_configuration", "currency"}
                }
                if c.live:
                    c.runtime_policy.require_current(session, "financial_apply")
                    c.runtime_policy.require_current(session, "panel_mutation")
                start = sub.expires_at
                if panel_entitlements.bound(sub):
                    if basis.get("panel_mappings", {}).get(product.id) != product.internal_squad_uuid:
                        raise DomainError(
                            "promo_mapping_review",
                            "Текущий тариф панели не совпадает с сохранёнными условиями подарка. Нужна проверка владельца.",
                            409,
                        )
                    from .panel_v3 import instant

                    start = max(
                        moment,
                        instant(panel_entitlements.facts(session, sub)["expires_at"]),
                        panel_entitlements.paid_horizon(session, sub, moment) or moment,
                    )
            else:
                product = session.get(Product, promo["product_id"])
                if not product or not product.available:
                    raise DomainError("promo_product_unavailable", "Тариф подарка временно недоступен.", 409)
                start = moment
                basis = {
                    "currency": product.currency,
                    "panel_mappings": {p.id: p.internal_squad_uuid for p in session.scalars(select(Product))},
                    "panel_configuration": c._setting(session, "panel_mapping", {}),
                }
                sub = Subscription(
                    account_id=account.id,
                    product_id=product.id,
                    devices_limit=product.included_devices,
                    environment="live" if c.live else "local_test" if c.local_test else "sandbox",
                )
                session.add(sub)
                session.flush()
            end = start + timedelta(days=days)
            prior_status = (
                panel_entitlements.local_status(session, sub) if panel_entitlements.bound(sub) else "active"
            )
            segment = Segment(
                subscription_id=sub.id,
                product_id=product.id,
                starts_at=start,
                ends_at=end,
                devices=sub.devices_limit,
                paid_value=Decimal(0),
                price_basis=basis | {"promo_code": normalized, "valuation": "gift_unknown"},
                kind="promo_gift",
            )
            if c.live:
                if type(segment.devices) is not int or not 1 <= segment.devices <= 100:
                    raise DomainError(
                        "gift_capacity_review", "Лимит подарочного доступа требует проверки поддержки.", 409
                    )
                c.runtime_policy.require_current(session, "financial_apply")
                c.runtime_policy.require_current(session, "panel_mutation")
                segment.price_basis = segment.price_basis | {
                    "gift_terms": {
                        "version": 1,
                        "scope_id": c.runtime_policy.scope.scope_id,
                        "environment": "live",
                        "claim_id": pending["id"],
                        "code": normalized,
                        "subscription_id": sub.id,
                        "product_id": product.id,
                        "devices": segment.devices,
                        "starts_at": start.isoformat(),
                        "ends_at": end.isoformat(),
                        "internal_squad_uuid": basis.get("panel_mappings", {}).get(product.id),
                    }
                }
            session.add(segment)
            session.flush()
            sub.expires_at = end
            sub.version += 1
            result = {
                "code": normalized,
                "state": "used",
                "subscription_id": sub.id,
                "provisioning_state": "pending",
                "reward_days": days,
                "expires_at": end.isoformat(),
            }
            session.add(
                PromoClaim(
                    id=pending["id"],
                    account_id=account.id,
                    code=normalized,
                    segment_id=segment.id,
                    result=result,
                )
            )
            if c.live and promo.get("trial") is True:
                session.add(
                    Setting(
                        key="promo_trial:" + str(account.id),
                        value={
                            "version": 1,
                            "account_id": str(account.id),
                            "scope_id": c.runtime_policy.scope.scope_id,
                            "claim_id": pending["id"],
                        },
                    )
                )
            session.add(
                Job(
                    key="promo-gift:" + pending["id"],
                    kind="provision",
                    account_id=account.id,
                    payload=c._provision_payload(
                        session, sub, moment=moment, prior_status=prior_status, origin="gift"
                    )
                    | ({"promo_claim_id": pending["id"], "gift_segment_id": segment.id} if c.live else {}),
                )
            )
            session.add(
                Audit(
                    actor_id=account.id,
                    action="promo_claim",
                    target=normalized,
                    reason="Подтверждена выдача подарочных дней",
                    details=result,
                )
            )
            account.pending_promo = None
            return result
