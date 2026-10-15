"""Удаление только подтверждённых новых тестовых VPN-пользователей; история остаётся."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy import select
from sunrise_core.errors import DomainError

from . import panel_entitlements
from .db import (
    Account,
    Agreement,
    Alias,
    Audit,
    Inbox,
    Job,
    Order,
    Payment,
    ProviderOperation,
    RefundReview,
    Setting,
    Subscription,
    now,
)
from .panel import PanelReview, PanelUnavailable
from .panel_events import PanelEvents
from .panel_v3 import instant


class RetentionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool = False
    days_after_expiry: Literal[7] = 7


class UUIDBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    uuid: str
    short_uuid: str
    username: str

    @field_validator("uuid")
    @classmethod
    def canonical_uuid(cls, value):
        parsed = UUID(value)
        if str(parsed) != value or parsed.version != 4:
            raise ValueError("retention_uuid_invalid")
        return value


class NumericBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    user_id: str = Field(pattern=r"^[1-9][0-9]{0,15}$")
    origin_nonce: str = Field(pattern=r"^[a-f0-9]{32}$")
    short_uuid: str
    username: str


class ProductionNumericBinding(NumericBinding):
    scope_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    generation: StrictInt = Field(ge=1)
    uuid: str | None

    @field_validator("uuid")
    @classmethod
    def preserved_uuid(cls, value):
        return UUIDBinding.canonical_uuid(value) if value is not None else None


class ImportRetentionAuthorization(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: StrictInt = Field(ge=1, le=1)
    state: Literal["approved"]
    scope_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    environment: Literal["live"]
    subscription_id: str
    account_id: str = Field(pattern=r"^[1-9][0-9]{0,18}$")
    origin_nonce: str = Field(pattern=r"^[a-f0-9]{32}$")
    panel_user_id: str = Field(pattern=r"^[1-9][0-9]{0,15}$")
    binding_generation: StrictInt = Field(ge=1)
    legacy_uuid: str
    evidence_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    approved_at: str

    @field_validator("legacy_uuid")
    @classmethod
    def preserved_uuid(cls, value):
        return UUIDBinding.canonical_uuid(value)

    @field_validator("approved_at")
    @classmethod
    def approved_timestamp(cls, value):
        return instant(value).isoformat()

    @model_validator(mode="after")
    def bounded_identities(self):
        if int(self.account_id) >= 2**63 or int(self.panel_user_id) > 9007199254740991:
            raise ValueError("import_retention_identity_invalid")
        return self


def import_retention_key(sid):
    import hashlib

    key = "import_retention:" + sid
    return key if len(key) <= 80 else "import_retention:" + hashlib.sha256(sid.encode()).hexdigest()[:40]


class RetentionState(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: StrictInt = Field(default=1, ge=1, le=1)
    subscription_id: str
    owner_id: str = Field(pattern=r"^[1-9][0-9]{0,18}$")
    environment: Literal["local_test", "sandbox", "live"]
    contract: Literal["2.8_uuid", "3.4_id"]
    panel_host: str
    binding: UUIDBinding | ProductionNumericBinding | NumericBinding
    target_version: StrictInt = Field(ge=1)
    expires_at: str
    deadline: str
    job_id: str
    phase: Literal["pending", "in_flight", "unknown", "review", "canceled", "archived"]
    attempted_at: str | None = None
    archived_at: str | None = None
    lease: str | None = None

    @model_validator(mode="after")
    def coherent_state(self):
        if (
            int(self.owner_id) >= 2**63
            or (self.environment == "live") != isinstance(self.binding, ProductionNumericBinding)
            or (self.contract == "2.8_uuid") != isinstance(self.binding, UUIDBinding)
            or isinstance(self.binding, NumericBinding)
            and int(self.binding.user_id) > 9007199254740991
            or self.phase == "archived"
            and (self.attempted_at is None or self.archived_at is None)
        ):
            raise ValueError("retention_state_inconsistent")
        return self

    @field_validator("expires_at", "deadline", "attempted_at", "archived_at")
    @classmethod
    def valid_instant(cls, value):
        if value is not None:
            try:
                instant(value)
            except PanelReview:
                raise ValueError("retention_timestamp_invalid") from None
        return value


def policy_in(session):
    row = session.get(Setting, "subscription_retention")
    try:
        return RetentionPolicy.model_validate(row.value if row else {})
    except ValidationError:
        raise DomainError("retention_policy_invalid", "Настройки удаления требуют проверки.", 409) from None


def state_in(session, sub):
    row = session.get(Setting, "retention:" + sub.id)
    if row is None:
        return None
    try:
        state = RetentionState.model_validate(row.value)
        job = session.get(Job, state.job_id)
        if (
            state.subscription_id != sub.id
            or state.owner_id != str(sub.account_id)
            or state.environment != sub.environment
            or instant(state.deadline) != instant(state.expires_at) + timedelta(days=7)
            or (
                state.target_version > sub.version
                if state.phase == "canceled"
                else state.target_version != sub.version
            )
            or job is None
            or job.kind != "subscription_cleanup"
            or job.account_id != sub.account_id
            or job.key != f"subscription-cleanup:{sub.id}:{state.target_version}"
            or state.attempted_at is not None
            and not 1 <= job.attempts <= 3
            or state.phase == "archived"
            and job.state != "done"
            or state.phase == "canceled"
            and (
                job.state != "done"
                or job.attempts != 0
                or state.attempted_at is not None
                or state.archived_at is not None
                or state.lease is not None
            )
            or job.payload != state.model_dump()
        ):
            raise ValueError
        return state
    except (ValidationError, ValueError, PanelReview):
        raise DomainError("retention_state_invalid", "Состояние удаления требует проверки.", 409) from None


def legacy_operation_key(identifier, *, account=False):
    import hashlib

    prefix = "legacy_operation_account:" if account else "legacy_operation:"
    value = prefix + str(identifier)
    return value if len(value) <= 80 else prefix + hashlib.sha256(str(identifier).encode()).hexdigest()[:40]


def legacy_operation_in(session, account_id, subscription_id=None):
    if (
        session.get(Setting, legacy_operation_key(account_id, account=True), populate_existing=True)
        is not None
    ):
        return True
    if (
        subscription_id is not None
        and session.get(Setting, legacy_operation_key(subscription_id), populate_existing=True) is not None
    ):
        return True
    return any(
        session.get(Setting, legacy_operation_key(identifier, account=True), populate_existing=True)
        is not None
        for identifier in session.scalars(select(Alias.old_id).where(Alias.canonical_id == account_id))
    )


def assert_legacy_operation_allowed_in(session, account_id, subscription_id=None):
    if legacy_operation_in(session, account_id, subscription_id):
        raise DomainError(
            "legacy_operation_review",
            "Историческая операция требует проверки. Обратитесь в поддержку; ранее оплаченный доступ сохранён.",
            409,
            next_action="support",
        )


def fenced_in(session, sub, *, include_legacy=True):
    from .subscription_archive import is_retired_local_fixture
    from .subscription_archive import state_in as archive_state_in

    if is_retired_local_fixture(sub):
        return True
    try:
        if include_legacy and legacy_operation_in(session, sub.account_id, sub.id):
            return True
        if archive_state_in(session, sub) is not None:
            return True
        state = state_in(session, sub)
        return state is not None and state.phase != "canceled"
    except DomainError:
        return True


def assert_mutation_allowed_in(session, sub, *, include_legacy=True):
    if fenced_in(session, sub, include_legacy=include_legacy):
        raise DomainError(
            "subscription_cleanup_pending",
            "Удаление VPN-подписки требует проверки. Оплата временно недоступна.",
            409,
        )


def prepare_merge_in(session, owners):
    states = []
    for sub in session.scalars(
        select(Subscription)
        .where(Subscription.account_id.in_(owners))
        .order_by(Subscription.id)
        .with_for_update()
    ):
        state = state_in(session, sub)
        if state is None:
            continue
        if state.phase not in {"archived", "canceled"}:
            assert_mutation_allowed_in(session, sub)
        states.append((sub, state))
    return states


def transfer_merge_in(session, states, canonical_id):
    for sub, original in states:
        if original.owner_id == str(canonical_id):
            continue
        if sub.account_id != canonical_id:
            raise DomainError("retention_state_invalid", "Состояние удаления требует проверки.", 409)
        job = session.get(Job, original.job_id)
        if job.account_id != canonical_id:
            raise DomainError("retention_state_invalid", "Состояние удаления требует проверки.", 409)
        value = original.model_copy(update={"owner_id": str(canonical_id)}).model_dump()
        session.get(Setting, "retention:" + sub.id).value = value
        job.payload = value
        session.add(
            Audit(
                action="subscription_retention_merge",
                target=sub.id,
                reason="Подтверждённое объединение аккаунтов сохраняет архив VPN.",
                details={
                    "original_owner_id": original.owner_id,
                    "canonical_owner_id": str(canonical_id),
                    "phase": original.phase,
                },
            )
        )


@dataclass(frozen=True)
class RetentionDecision:
    eligible: bool
    reason: str
    effective_expiry: datetime | None = None
    deadline: datetime | None = None


class RetentionService:
    def __init__(self, commerce, settings):
        self.commerce, self.db, self.settings = commerce, commerce.db, settings
        self.panel = commerce.panel_provider

    def _owned(self, session, sub):
        if (
            self.panel is None
            or self.settings is None
            or not (self.commerce.local_test or getattr(self.commerce, "live", False))
        ):
            raise ValueError("retention_scope_denied")
        if getattr(self.commerce, "live", False):
            self.commerce.runtime_policy.require_current(session)
        return PanelEvents(self.db, self.panel, self.settings)._owned(session, sub)

    def _holds(self, session, sub, moment, job_id=None):
        if legacy_operation_in(session, sub.account_id, sub.id):
            return "legacy_operation_review"
        if panel_entitlements.local_status(session, sub) not in {
            "active",
            "expired",
        } or panel_entitlements.remote_status(session, sub):
            return "restricted"
        dispute = session.get(Setting, "dispute:" + sub.id)
        if dispute and dispute.value.get("enabled"):
            return "disputed"
        if panel_entitlements.remaining_segments(session, sub, moment) or panel_entitlements.paid_horizon(
            session, sub, moment
        ):
            return "paid_rights"
        if session.scalar(
            select(Agreement.id)
            .where(Agreement.subscription_id == sub.id, Agreement.state != "stopped")
            .limit(1)
        ):
            return "agreement_unstopped"
        orders = select(Order.id).where(Order.account_id == sub.account_id)
        if session.scalar(
            select(Order.id)
            .where(
                Order.account_id == sub.account_id,
                Order.status.not_in(["paid", "canceled", "expired", "refunded"]),
            )
            .limit(1)
        ):
            return "payment_unresolved"
        if session.scalar(
            select(Order.id)
            .where(Order.account_id == sub.account_id, Order.status == "paid", Order.fulfilled_at.is_(None))
            .limit(1)
        ):
            return "payment_unfulfilled"
        if session.scalar(
            select(ProviderOperation.id)
            .where(ProviderOperation.order_id.in_(orders), ProviderOperation.state != "reconciled")
            .limit(1)
        ):
            return "provider_unresolved"
        if session.scalar(
            select(RefundReview.id)
            .join(Payment, Payment.id == RefundReview.payment_id)
            .where(Payment.account_id == sub.account_id, RefundReview.resolved_at.is_(None))
            .limit(1)
        ):
            return "refund_unresolved"
        if session.scalar(select(Inbox.id).where(Inbox.state != "processed").limit(1)):
            return "inbox_unresolved"
        if session.scalar(
            select(Job.id)
            .where(
                Job.account_id == sub.account_id,
                Job.id != job_id if job_id else True,
                Job.state.in_(["pending", "in_flight", "review", "blocked"]),
            )
            .limit(1)
        ):
            return "job_unresolved"
        return None

    def _import_authorized(self, session, sub, proof):
        if not getattr(self.commerce, "live", False) or proof.get("origin") != "imported_project_identity":
            return False
        record = session.get(Setting, import_retention_key(sub.id), populate_existing=True)
        if record is None:
            return False
        try:
            permission = ImportRetentionAuthorization.model_validate(record.value)
        except (ValidationError, ValueError, TypeError, PanelReview):
            return False
        binding = proof.get("binding")
        return bool(
            isinstance(binding, dict)
            and sub.environment == "live"
            and permission.scope_id
            == proof.get("scope_id")
            == binding.get("scope_id")
            == self.commerce.runtime_policy.scope.scope_id
            and permission.subscription_id == sub.id
            and panel_entitlements.owned_account(session, sub, permission.account_id)
            and permission.origin_nonce == binding.get("origin_nonce")
            and permission.panel_user_id == binding.get("user_id") == str(sub.panel_user_id)
            and permission.binding_generation == binding.get("generation") == proof.get("generation")
            and permission.legacy_uuid == binding.get("uuid") == sub.panel_uuid
        )

    def decision_in(self, session, sub, moment):
        if legacy_operation_in(session, sub.account_id, sub.id):
            return RetentionDecision(False, "legacy_operation_review")
        if not policy_in(session).enabled:
            return RetentionDecision(False, "disabled")
        if fenced_in(session, sub):
            return RetentionDecision(False, "fenced")
        try:
            _, proof = self._owned(session, sub)
            if getattr(self.commerce, "live", False):
                try:
                    self.commerce.runtime_policy.require_current(session, "retention")
                except DomainError:
                    return RetentionDecision(False, "deployment_retention_disabled")
                if proof.get("origin") == "imported_project_identity" and not self._import_authorized(
                    session, sub, proof
                ):
                    return RetentionDecision(False, "import_requires_review")
        except (ValueError, PanelReview):
            return RetentionDecision(False, "scope_unverified")
        reason = self._holds(session, sub, moment)
        if reason:
            return RetentionDecision(False, reason)
        expiry = panel_entitlements.verified_service_horizon(session, sub, moment)
        if expiry is None:
            return RetentionDecision(False, "expiry_unverified")
        deadline = expiry + timedelta(days=7)
        return RetentionDecision(
            deadline <= moment, "due" if deadline <= moment else "not_due", expiry, deadline
        )

    def queue_decision_in(self, session, sub, moment):
        decision = self.decision_in(session, sub, moment)
        if decision.reason != "expiry_unverified":
            return decision
        # The ordinary decision already checked policy, scope, fences and financial holds.
        expiry = panel_entitlements.historical_due_horizon(session, sub, moment)
        if expiry is None:
            return decision
        deadline = expiry + timedelta(days=7)
        return RetentionDecision(
            deadline <= moment, "due_hint" if deadline <= moment else "not_due", expiry, deadline
        )

    def _save(self, session, sub, job, state, reason):
        row = session.get(Setting, "retention:" + sub.id)
        value = state.model_dump()
        if row is None:
            session.add(Setting(key="retention:" + sub.id, value=value))
        else:
            row.value = value
        job.payload = value
        session.add(
            Audit(
                action="subscription_retention",
                target=sub.id,
                reason="Изменение состояния удаления VPN-подписки.",
                details={"phase": state.phase, "code": reason},
            )
        )

    def schedule_due(self, moment=None, limit=50):
        moment = moment or now()
        with self.db.transaction() as session:
            if not policy_in(session).enabled:
                return 0
            candidates = list(
                session.execute(
                    select(Subscription.id, Subscription.account_id)
                    .where(Subscription.expires_at <= moment - timedelta(days=7))
                    .order_by(Subscription.id)
                )
            )
        total = 0
        for sid, owner in candidates:
            if total >= limit:
                break
            with self.db.transaction() as session:
                session.scalar(select(Account).where(Account.id == owner).with_for_update())
                sub = session.scalar(select(Subscription).where(Subscription.id == sid).with_for_update())
                if sub is None or sub.account_id != owner:
                    continue
                decision = self.queue_decision_in(session, sub, moment)
                if not decision.eligible:
                    continue
                binding, _ = self._owned(session, sub)
                sub.version += 1
                job = Job(
                    key=f"subscription-cleanup:{sid}:{sub.version}",
                    kind="subscription_cleanup",
                    account_id=owner,
                    payload={},
                    available_at=moment,
                )
                session.add(job)
                session.flush()
                state = RetentionState(
                    subscription_id=sid,
                    owner_id=str(owner),
                    environment=sub.environment,
                    contract=self.panel.contract,
                    panel_host=self.panel.api_host,
                    binding=binding,
                    target_version=sub.version,
                    expires_at=decision.effective_expiry.isoformat(),
                    deadline=decision.deadline.isoformat(),
                    job_id=job.id,
                    phase="pending",
                )
                self._save(session, sub, job, state, "scheduled")
                total += 1
        return total

    def projection_in(self, session, sub):
        try:
            state = state_in(session, sub)
        except DomainError:
            return {"retention_state": "review", "cleanup_at": None, "archived_at": None}
        if state and state.phase != "canceled":
            return {
                "retention_state": state.phase,
                "cleanup_at": state.deadline,
                "archived_at": state.archived_at,
            }
        expiry = panel_entitlements.verified_service_horizon(session, sub, now())
        deadline = expiry + timedelta(days=7) if expiry and policy_in(session).enabled else None
        if panel_entitlements.paid_horizon(session, sub, now()):
            deadline = None
        try:
            _, proof = self._owned(session, sub)
            if (
                getattr(self.commerce, "live", False)
                and proof.get("origin") == "imported_project_identity"
                and not self._import_authorized(session, sub, proof)
            ):
                deadline = None
            if deadline and self._holds(session, sub, deadline):
                deadline = None
        except (ValueError, PanelReview):
            deadline = None
        return {
            "retention_state": "none",
            "cleanup_at": deadline.isoformat() if deadline else None,
            "archived_at": None,
        }

    def run_jobs(self, limit=50):
        with self.db.transaction() as session:
            candidates = list(
                session.execute(
                    select(Job.id, Job.account_id, Job.payload["subscription_id"].as_string())
                    .where(
                        Job.kind == "subscription_cleanup",
                        Job.state.in_(["pending", "in_flight"]),
                        Job.available_at <= now(),
                    )
                    .order_by(Job.id)
                    .limit(limit)
                )
            )
        completed = 0
        for jid, owner, sid in candidates:
            lease = uuid4().hex
            claimed = False
            try:
                with self.db.transaction() as session:
                    sub, job, state = self._locked(session, owner, sid, jid)
                    if job.state not in {"pending", "in_flight"} or job.available_at > now():
                        continue
                    binding, _ = self._owned(session, sub)
                    self._verify_target(sub, job, state, binding)
                    if not policy_in(session).enabled and state.attempted_at is None:
                        job.state = "done"
                        self._save(
                            session,
                            sub,
                            job,
                            state.model_copy(update={"phase": "canceled", "lease": None}),
                            "disabled_before_attempt",
                        )
                        continue
                    reason = self._holds(session, sub, now(), jid)
                    if reason and state.attempted_at is None:
                        self._review(session, sub, job, state, reason)
                        continue
                    job.state, job.available_at = "in_flight", now() + timedelta(minutes=2)
                    state = state.model_copy(update={"phase": "in_flight", "lease": lease})
                    self._save(session, sub, job, state, "claimed")
                    claimed = True
                snapshot = self.panel.cleanup_snapshot(binding)
                if snapshot is None:
                    if state.attempted_at is None:
                        raise PanelReview("panel_missing_before_delete")
                    self._archive(owner, sid, jid, lease)
                    completed += 1
                    continue
                eligible_snapshot = True
                try:
                    self._verify_expiry(state, snapshot)
                except PanelReview:
                    eligible_snapshot = False
                with self.db.transaction() as session:
                    sub, job, current = self._locked(session, owner, sid, jid)
                    current_binding, _ = self._owned(session, sub)
                    self._verify_target(sub, job, current, current_binding, lease)
                    if current.attempted_at is not None and (
                        not eligible_snapshot
                        or not policy_in(session).enabled
                        or job.attempts >= 3
                        or self._holds(session, sub, now(), jid)
                    ):
                        self._defer(session, sub, job, current)
                        continue
                    renewed = (
                        current.attempted_at is None
                        and snapshot["status"] in {"ACTIVE", "EXPIRED"}
                        and instant(snapshot["expires_at"]) + timedelta(days=7) > now()
                    )
                    if renewed:
                        job.state = "done"
                        self._save(
                            session,
                            sub,
                            job,
                            current.model_copy(update={"phase": "canceled", "lease": None}),
                            "renewed_before_attempt",
                        )
                    elif not eligible_snapshot:
                        raise PanelReview("cleanup_expiry_changed")

                if renewed:
                    # Adopt a full scoped snapshot only after the unattempted fence is terminal.
                    PanelEvents(self.db, self.panel, self.settings).reconcile_owned(sid)
                    continue

                binding_items, snapshot_items = tuple(binding.items()), tuple(snapshot.items())

                def before_delete(
                    owner=owner,
                    sid=sid,
                    jid=jid,
                    lease=lease,
                    binding_items=binding_items,
                    snapshot_items=snapshot_items,
                ):
                    binding, snapshot = dict(binding_items), dict(snapshot_items)
                    with self.db.transaction() as session:
                        sub, job, current = self._locked(session, owner, sid, jid)
                        current_binding, current_proof = self._owned(session, sub)
                        self._verify_target(sub, job, current, current_binding, lease)
                        if current_binding != binding:
                            raise PanelReview("cleanup_target_changed")
                        if not policy_in(session).enabled or job.attempts >= 3:
                            if current.attempted_at is not None:
                                raise PanelUnavailable("cleanup_reconcile_only")
                            raise PanelReview("cleanup_disabled_before_attempt")
                        reason = self._holds(session, sub, now(), jid)
                        if reason:
                            if current.attempted_at is not None:
                                raise PanelUnavailable("cleanup_reconcile_only")
                            raise PanelReview("cleanup_" + reason)
                        self._verify_expiry(current, snapshot)
                        if getattr(self.commerce, "live", False):
                            self.commerce.runtime_policy.require_current(session, "retention")
                            self.commerce.runtime_policy.require_current(session, "panel_mutation")
                            if current_proof.get(
                                "origin"
                            ) == "imported_project_identity" and not self._import_authorized(
                                session, sub, current_proof
                            ):
                                if current.attempted_at is not None:
                                    raise PanelUnavailable("cleanup_reconcile_only")
                                raise PanelReview("cleanup_import_authorization_required")
                        current = current.model_copy(
                            update={"attempted_at": current.attempted_at or now().isoformat()}
                        )
                        job.attempts += 1
                        self._save(session, sub, job, current, "delete_attempted")

                try:
                    self.panel.delete_user(binding, before_delete=before_delete)
                except PanelReview:
                    # A previous recorded DELETE can finish between GET and this retry.
                    # Only another exact scoped GET can prove absence; DELETE404 is insufficient.
                    if self.panel.cleanup_snapshot(binding) is not None:
                        raise PanelUnavailable("panel_delete_not_observed") from None
                if self.panel.cleanup_snapshot(binding) is not None:
                    raise PanelUnavailable("panel_delete_not_observed")
                self._archive(owner, sid, jid, lease)
                completed += 1
            except (PanelReview, PanelUnavailable, ValueError, DomainError) as error:
                with self.db.transaction() as session:
                    session.scalar(select(Account).where(Account.id == owner).with_for_update())
                    sub = session.scalar(select(Subscription).where(Subscription.id == sid).with_for_update())
                    job = session.scalar(select(Job).where(Job.id == jid).with_for_update())
                    if (
                        sub is None
                        or job is None
                        or job.state == "done"
                        or sub.account_id != owner
                        or job.account_id != owner
                    ):
                        continue
                    if claimed and (job.state != "in_flight" or job.payload.get("lease") != lease):
                        continue
                    try:
                        current = state_in(session, sub)
                    except DomainError:
                        job.state, job.last_error = "review", "cleanup_state_invalid"
                        continue
                    if current is None or current.lease not in {None, lease}:
                        continue
                    if isinstance(error, PanelUnavailable):
                        self._defer(session, sub, job, current)
                    else:
                        self._review(session, sub, job, current, "cleanup_review_required")
        return completed

    def _defer(self, session, sub, job, state):
        job.state, job.available_at, job.last_error = (
            "pending",
            now() + timedelta(seconds=30),
            "cleanup_outcome_unknown",
        )
        self._save(
            session, sub, job, state.model_copy(update={"phase": "unknown", "lease": None}), "outcome_unknown"
        )

    def _locked(self, session, owner, sid, jid):
        session.scalar(select(Account).where(Account.id == owner).with_for_update())
        sub = session.scalar(select(Subscription).where(Subscription.id == sid).with_for_update())
        job = session.scalar(select(Job).where(Job.id == jid).with_for_update())
        if sub is None or job is None or sub.account_id != owner or job.account_id != owner:
            raise PanelReview("cleanup_owner_changed")
        session.scalar(select(Setting).where(Setting.key == "retention:" + sid).with_for_update())
        state = state_in(session, sub)
        if state is None:
            raise PanelReview("cleanup_state_missing")
        return sub, job, state

    def _verify_target(self, sub, job, state, binding, lease=None):
        if (
            state.job_id != job.id
            or state.target_version != sub.version
            or job.payload != state.model_dump()
            or state.binding.model_dump() != binding
            or state.contract != self.panel.contract
            or state.panel_host != self.panel.api_host
            or state.phase in {"canceled", "archived", "review"}
            or lease is not None
            and (state.lease != lease or job.state != "in_flight")
        ):
            raise PanelReview("cleanup_target_changed")

    def _verify_expiry(self, state, snapshot):
        if (
            snapshot["status"] not in {"ACTIVE", "EXPIRED"}
            or instant(snapshot["expires_at"]) != instant(state.expires_at)
            or instant(snapshot["expires_at"]) + timedelta(days=7) > now()
        ):
            raise PanelReview("cleanup_expiry_changed")

    def _archive(self, owner, sid, jid, lease):
        with self.db.transaction() as session:
            sub, job, state = self._locked(session, owner, sid, jid)
            binding, _ = self._owned(session, sub)
            self._verify_target(sub, job, state, binding, lease)
            if state.attempted_at is None:
                raise PanelReview("cleanup_completion_requires_attempt")
            state = state.model_copy(
                update={"phase": "archived", "archived_at": now().isoformat(), "lease": None}
            )
            job.state, job.last_error = "done", None
            self._save(session, sub, job, state, "absence_confirmed")
            from .subscription_archive import archive_absence_in

            archive_absence_in(session, self.commerce, sub, binding, reason="expiry_cleanup", proof_id=jid)

    def _review(self, session, sub, job, state, reason):
        job.state, job.last_error = "review", reason
        self._save(session, sub, job, state.model_copy(update={"phase": "review", "lease": None}), reason)
