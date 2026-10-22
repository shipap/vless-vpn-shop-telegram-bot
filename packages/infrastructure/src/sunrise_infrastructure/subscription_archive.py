"""Restricted technical archive of a positively scoped, confirmed absent panel user."""

import hashlib
import json
import re
from datetime import timedelta
from typing import Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy import and_, select
from sqlalchemy.exc import OperationalError
from sunrise_core.errors import DomainError

from . import panel_entitlements
from .db import (
    Account,
    Alias,
    Audit,
    Job,
    PanelEvent,
    PanelObservation,
    Product,
    Setting,
    Subscription,
    now,
)
from .panel import PanelReview, PanelUnavailable
from .panel_news import owner_account_in
from .panel_v3 import instant
from .retention import NumericBinding, ProductionNumericBinding, UUIDBinding


class ArchiveState(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: StrictInt = Field(default=1, ge=1, le=1)
    subscription_id: str = Field(min_length=1, max_length=64)
    owner_id: str = Field(pattern=r"^[1-9][0-9]{0,18}$")
    environment: Literal["local_test", "sandbox", "live", "scoped_live"]
    contract: Literal["2.8_uuid", "3.4_id"]
    panel_host: str
    binding: UUIDBinding | ProductionNumericBinding | NumericBinding
    subscription_version: StrictInt = Field(ge=1)
    reason: Literal["manual_panel", "expiry_cleanup", "verified_panel_absence"]
    proof_id: str
    deleted_at: str
    tariff_name: str
    subscription_name: str
    expires_at: str | None = None
    paid_horizon: str | None = None
    remaining_seconds: StrictInt | None = None
    device_limit: StrictInt | None = Field(ge=1, le=100)
    device_count: StrictInt | None = Field(default=None, ge=0, le=10000)
    observed_at: str | None = None
    review_required: bool = False
    user_intent_id: str | None = None
    owner_intent_id: str | None = None

    @field_validator("deleted_at", "expires_at", "paid_horizon", "observed_at")
    @classmethod
    def timestamp(cls, value):
        if value is not None:
            instant(value)
        return value

    @model_validator(mode="after")
    def identity(self):
        if (
            int(self.owner_id) >= 2**63
            or (self.environment == "live") != isinstance(self.binding, ProductionNumericBinding)
            or self.environment != "live"
            and self.device_limit is None
            or (self.contract == "2.8_uuid") != isinstance(self.binding, UUIDBinding)
            or isinstance(self.binding, NumericBinding)
            and int(self.binding.user_id) > 9007199254740991
        ):
            raise ValueError("archive_identity_invalid")
        return self


def verified_read_proof_in(session, sub, binding, proof_id, *, deleted_at=None):
    if not isinstance(proof_id, str) or not re.fullmatch(r"panel_read:[a-f0-9]{64}", proof_id):
        return False
    marker = session.get(Setting, proof_id)
    value = marker.value if marker and isinstance(marker.value, dict) else {}
    required = {
        "version",
        "subscription_id",
        "owner_id",
        "environment",
        "scope_id",
        "panel_host",
        "contract",
        "binding",
        "subscription_version",
        "authority_node_uuid",
        "observed_at",
    }
    try:
        observed = instant(value["observed_at"])
        node = value["authority_node_uuid"]
        if (
            set(value) != required
            or value["version"] != 1
            or value["subscription_id"] != sub.id
            or not panel_entitlements.owned_account(session, sub, value["owner_id"])
            or value["environment"] != "scoped_live"
            or not isinstance(value["scope_id"], str)
            or not value["scope_id"]
            or value["panel_host"] != url_host(sub, session)
            or value["contract"] != "3.4_id"
            or value["binding"] != binding
            or value["subscription_version"] != sub.version
            or not isinstance(node, str)
            or str(UUID(node)) != node
            or hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            != proof_id.removeprefix("panel_read:")
        ):
            return False
        end = instant(deleted_at) if deleted_at is not None else now()
        return timedelta(0) <= end - observed <= timedelta(minutes=1)
    except (KeyError, TypeError, ValueError, PanelReview):
        return False


def url_host(sub, session):
    record = session.get(Setting, "panel_test:" + sub.id)
    return record.value.get("panel_host") if record and isinstance(record.value, dict) else None


def retired_local_fixture_clause():
    return and_(Subscription.environment == "local_test", Subscription.status == "fixture_retired")


def is_retired_local_fixture(sub):
    return sub.environment == "local_test" and sub.status == "fixture_retired"


def unarchived_subscription_clause():
    return and_(
        ~retired_local_fixture_clause(),
        ~select(Setting.key)
        .where(Setting.key == "archive:" + Subscription.id)
        .correlate(Subscription)
        .exists(),
    )


def state_in(session, sub):
    row = session.get(Setting, "archive:" + sub.id)
    if row is None:
        return None
    try:
        state = ArchiveState.model_validate(row.value)
        from .panel_v3 import explicit_null_evidence, production_binding_key

        registration = session.get(
            Setting, production_binding_key(sub.id) if state.environment == "live" else "panel_test:" + sub.id
        )
        proof = registration.value if registration and isinstance(registration.value, dict) else {}
        if state.environment == "live":
            marker = session.get(Setting, "production_admission", populate_existing=True)
            scope = marker.value.get("scope_id") if marker and isinstance(marker.value, dict) else None
            if (
                not scope
                or state.binding.scope_id != scope
                or proof.get("scope_id") != scope
                or proof.get("origin") not in {"new_project_identity", "imported_project_identity"}
                or proof.get("environment") != "live"
                or proof.get("subscription_id") != sub.id
                or type(proof.get("generation")) is not int
                or proof["generation"] != state.binding.generation
                or state.binding.user_id != str(sub.panel_user_id)
                or state.binding.uuid != sub.panel_uuid
                or sub.short_uuid not in {None, state.binding.short_uuid}
                or proof.get("connection_url") != sub.connection_url
                or state.device_limit is None
                and (
                    proof.get("origin") != "imported_project_identity"
                    or not explicit_null_evidence(proof.get("capacity_evidence"))
                )
            ):
                raise ValueError
        if (
            state.subscription_id != sub.id
            or state.owner_id != str(sub.account_id)
            or state.environment != sub.environment
            or state.subscription_version != sub.version
            or state.contract != proof.get("contract")
            or state.panel_host != proof.get("panel_host")
            or state.environment != "live"
            and proof.get("origin") != "new_test_identity"
            or proof.get("binding") != state.binding.model_dump()
            or not panel_entitlements.owned_account(session, sub, proof.get("account_id"))
        ):
            raise ValueError
        if state.reason == "manual_panel":
            event = session.get(PanelEvent, state.proof_id)
            if (
                event is None
                or event.kind != "user.deleted"
                or event.subscription_id != sub.id
                or str(event.panel_user_id) != state.binding.user_id
            ):
                raise ValueError
        elif state.reason == "verified_panel_absence":
            if state.environment != "scoped_live" or not verified_read_proof_in(
                session, sub, state.binding.model_dump(), state.proof_id, deleted_at=state.deleted_at
            ):
                raise ValueError
        else:
            job = session.get(Job, state.proof_id)
            if (
                job is None
                or job.kind != "subscription_cleanup"
                or job.account_id != sub.account_id
                or job.state != "done"
                or job.attempts < 1
                or job.payload.get("phase") != "archived"
                or not job.payload.get("attempted_at")
            ):
                raise ValueError
        return state
    except (ValidationError, ValueError, PanelReview, AttributeError):
        raise DomainError("subscription_archive_invalid", "Архив подписки требует проверки.", 409) from None


def prepare_merge_in(session, subscriptions):
    return [(sub, state) for sub in subscriptions if (state := state_in(session, sub)) is not None]


def transfer_merge_in(session, states, canonical_id):
    for sub, state in states:
        if sub.account_id != canonical_id:
            raise DomainError("subscription_archive_invalid", "Архив подписки требует проверки.", 409)
        session.get(Setting, "archive:" + sub.id).value = state.model_copy(
            update={"owner_id": str(canonical_id)}
        ).model_dump()


def owner_deletion_message(state):
    cause = {
        "manual_panel": "удаление в панели",
        "expiry_cleanup": "7 дней после окончания",
        "verified_panel_absence": "подтверждённое отсутствие в панели",
    }[state.reason]

    def label(value):
        return " ".join(re.sub(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://\S+", "[ссылка скрыта]", value).split())[:200]

    capacity = str(state.device_limit) if state.device_limit is not None else "Лимит не задан в панели"
    remaining = (
        "неизвестно"
        if state.remaining_seconds is None
        else str(state.remaining_seconds) + " секунд (отрицательное значение — срок уже истёк)"
    )
    return f"Подписка: {label(state.subscription_name)}\nТариф: {label(state.tariff_name)}\nID: {state.subscription_id}\nОстаток: {remaining}\nСрок UTC: {state.expires_at or 'неизвестно'}\nОплаченные права до UTC: {state.paid_horizon or 'неизвестно'}\nУстройства: {state.device_count if state.device_count is not None else 'неизвестно'} / {capacity}\nПричина: {cause}\nНаблюдение UTC: {state.observed_at or 'неизвестно'}\nТребуется проверка оплаченных прав: {'да' if state.review_required else 'нет'}\nДля восстановления нужна ручная проверка; автоматического восстановления нет."


def owner_deletion_intent_in(session, service, owner, state):
    if state.owner_intent_id is not None:
        return state
    from .sale_notifications import staff_account_identity

    account = session.get(Account, int(state.owner_id))
    body = "Пользователь: " + staff_account_identity(account) + "\n" + owner_deletion_message(state)
    notice = service.emit_in(
        session,
        owner.id,
        "owner-subscription-deleted:" + state.subscription_id,
        "subscription_deleted_owner",
        "Удалена VPN-подписка",
        body,
        channel="auto",
        owner_only=True,
        allow_push=False,
    )
    return state.model_copy(update={"owner_intent_id": notice.id})


class DeferredArchiveOwnerNotices:
    """Bounded DB-only replay of an already validated live deletion archive."""

    def __init__(self, commerce, notifications=None):
        from .notifications import NotificationService

        self.db, self.policy = commerce.db, commerce.runtime_policy
        self.settings = commerce.public_settings or getattr(commerce.accounts, "settings", None)
        self.notifications = notifications or NotificationService(self.db, self.settings)
        self._cursor = None

    def run_pending(self, limit=50):
        limit = max(0, min(int(limit), 50))
        if not limit or not self.policy or not self.policy.production:
            return 0
        try:
            with self.db.transaction() as session:
                self.policy.require_current(session)
                owner = owner_account_in(session, self.settings)
                if owner is None:
                    return 0
                owner_hint = owner.id
                query = (
                    select(Setting.key, Subscription.id, Subscription.account_id)
                    .join(Subscription, Setting.key == "archive:" + Subscription.id)
                    .where(
                        Setting.key.like("archive:%"),
                        Setting.value["owner_intent_id"].as_string().is_(None),
                    )
                    .order_by(Setting.key)
                    .limit(limit)
                )
                if self._cursor is not None:
                    query = query.where(Setting.key > self._cursor)
                candidates = session.execute(query).all()
        except (DomainError, PanelUnavailable):
            return 0
        if not candidates:
            self._cursor = None
            return 0
        completed = 0
        for candidate in candidates:
            # Advance even on malformed/busy records; wrapping retries them after verification or recovery.
            self._cursor = candidate.key
            completed += self._record(candidate, owner_hint)
        return completed

    def _record(self, candidate, owner_hint):
        try:
            with self.db.transaction() as session:
                self.policy.require_current(session)
                # Both identities follow the same sorted order as account merge, without waiting.
                for identifier in sorted({candidate.account_id, owner_hint}):
                    if (
                        session.get(
                            Account, identifier, populate_existing=True, with_for_update={"nowait": True}
                        )
                        is None
                    ):
                        return 0
                if session.get(Alias, candidate.account_id, populate_existing=True) is not None:
                    return 0
                sub = session.get(
                    Subscription, candidate.id, populate_existing=True, with_for_update={"nowait": True}
                )
                row = session.get(
                    Setting, candidate.key, populate_existing=True, with_for_update={"nowait": True}
                )
                if sub is None or row is None or sub.account_id != candidate.account_id:
                    return 0
                state = state_in(session, sub)
                if (
                    state is None
                    or state.owner_intent_id is not None
                    or state.environment != "live"
                    or state.binding.scope_id != self.policy.scope.scope_id
                    or "https://" + state.panel_host != self.policy.scope.panel.origin
                    or state.contract != self.policy.scope.panel.contract
                ):
                    return 0
                owner = owner_account_in(session, self.settings)
                if owner is None or owner.id != owner_hint:
                    return 0
                self.policy.require_current(session)
                state = owner_deletion_intent_in(session, self.notifications, owner, state)
                row.value = state.model_dump()
                session.flush()
                return 1
        except (DomainError, PanelUnavailable):
            return 0
        except OperationalError as error:
            if getattr(error.orig, "sqlstate", None) == "55P03":
                return 0
            raise


def archive_absence_in(session, commerce, sub, binding, *, reason, proof_id):
    """Caller holds canonical Account then Subscription; exact GET404 already completed."""
    from .notifications import NotificationService
    from .panel_events import PanelEvents

    row = session.scalar(select(Setting).where(Setting.key == "archive:" + sub.id).with_for_update())
    if row is not None:
        return state_in(session, sub)
    settings = commerce.public_settings or getattr(commerce.accounts, "settings", None)
    current, proof = PanelEvents(commerce.db, commerce.panel_provider, settings)._owned(session, sub)
    if current != binding:
        raise PanelReview("archive_scope_changed")
    if sub.devices_limit is None:
        from .panel_v3 import explicit_null_evidence

        if proof.get("origin") != "imported_project_identity" or not explicit_null_evidence(
            proof.get("capacity_evidence")
        ):
            raise PanelReview("archive_capacity_proof_missing")
    if reason == "manual_panel":
        event = session.get(PanelEvent, proof_id)
        if (
            commerce.panel_provider.contract != "3.4_id"
            or event is None
            or event.kind != "user.deleted"
            or event.subscription_id != sub.id
            or str(event.panel_user_id) != binding["user_id"]
            or event.state != "pending"
        ):
            raise PanelReview("archive_event_proof_denied")
    elif reason == "expiry_cleanup":
        job = session.get(Job, proof_id)
        if (
            job is None
            or job.state != "done"
            or job.kind != "subscription_cleanup"
            or job.account_id != sub.account_id
            or job.payload.get("phase") != "archived"
            or not job.payload.get("attempted_at")
            or job.attempts < 1
        ):
            raise PanelReview("archive_attempt_proof_denied")
    elif reason == "verified_panel_absence":
        if (
            sub.environment != "scoped_live"
            or not getattr(commerce.panel_provider, "scoped_runtime_policy", None)
            or not verified_read_proof_in(session, sub, binding, proof_id)
        ):
            raise PanelReview("archive_read_proof_denied")
    else:
        raise PanelReview("archive_source_denied")
    observation = session.scalar(
        select(PanelObservation)
        .where(PanelObservation.kind == "user", PanelObservation.subscription_id == sub.id)
        .order_by(PanelObservation.observed_at.desc())
        .limit(1)
    )
    expiry, count, observed = None, None, None
    if observation and isinstance(observation.payload, dict):
        value = observation.payload
        if (
            value.get("source") == "remnawave"
            and value.get("binding") == binding
            and value.get("environment") == sub.environment
            and panel_entitlements.owned_account(session, sub, value.get("account_id"))
        ):
            try:
                expiry = instant(value["expires_at"])
                observed = observation.observed_at.isoformat()
                if type(value.get("device_count")) is int and 0 <= value["device_count"] <= 10000:
                    count = value["device_count"]
            except (KeyError, PanelReview, ValueError):
                expiry, count, observed = None, None, None
    moment = now()
    horizon = panel_entitlements.paid_horizon(session, sub, moment)
    paid = panel_entitlements.remaining_segments(session, sub, moment)
    product = session.get(Product, sub.product_id) if sub.product_id else None
    state = ArchiveState(
        subscription_id=sub.id,
        owner_id=str(sub.account_id),
        environment=sub.environment,
        contract=proof["contract"],
        panel_host=proof["panel_host"],
        binding=binding,
        subscription_version=sub.version,
        reason=reason,
        proof_id=proof_id,
        deleted_at=moment.isoformat(),
        tariff_name=product.name if product else "Тариф неизвестен",
        subscription_name=sub.name,
        expires_at=expiry.isoformat() if expiry else None,
        paid_horizon=horizon.isoformat() if horizon else None,
        remaining_seconds=int((expiry - moment).total_seconds()) if expiry else None,
        device_limit=sub.devices_limit,
        device_count=count,
        observed_at=observed,
        review_required=bool(paid or horizon),
    )
    service = NotificationService(commerce.db, settings)
    user = service.emit_in(
        session,
        sub.account_id,
        "subscription-deleted:" + sub.id,
        "subscription_deleted",
        "Подписка удалена",
        "Ваша подписка удалена, поскольку после окончания её срока действия прошло 7 дней."
        if reason == "expiry_cleanup"
        else "Подписка была удалена. Если это ошибка, обратитесь в поддержку",
        channel="telegram",
        allow_push=False,
    )
    state = state.model_copy(update={"user_intent_id": user.id})
    owner = owner_account_in(session, settings)
    if owner is not None:
        state = owner_deletion_intent_in(session, service, owner, state)
    session.add(Setting(key="archive:" + sub.id, value=state.model_dump()))
    session.add(
        Audit(
            action="subscription_archive",
            target=sub.id,
            reason="Подтверждено отсутствие собственного VPN-пользователя.",
            details={"source": reason, "review_required": state.review_required},
        )
    )
    return state
