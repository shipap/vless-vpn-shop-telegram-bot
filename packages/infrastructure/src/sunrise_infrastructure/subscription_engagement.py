"""Проверенное первое подключение и один устойчивый intent помощи для собственного пользователя."""

from datetime import timedelta
from decimal import Decimal
from typing import Literal
from uuid import uuid4

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
    Inbox,
    Job,
    NotificationDelivery,
    Order,
    Payment,
    ProviderOperation,
    Quote,
    RefundReview,
    Setting,
    Subscription,
    now,
)
from .panel import PanelReview, PanelUnavailable
from .panel_v3 import instant


class CreationIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: StrictInt = Field(default=1, ge=1, le=1)
    subscription_id: str = Field(min_length=1, max_length=64)
    owner_id: str = Field(pattern=r"^[1-9][0-9]{0,18}$")
    environment: Literal["local_test", "sandbox", "live", "scoped_live"]
    contract: Literal["3.4_id"] = "3.4_id"
    panel_host: str = Field(min_length=1, max_length=253)
    origin_nonce: str = Field(pattern=r"^[a-f0-9]{32}$")
    username: str = Field(min_length=1, max_length=100)
    short_uuid: str = Field(pattern=r"^[A-Za-z0-9_-]{16,64}$")
    job_id: str = Field(min_length=1, max_length=64)
    order_id: str = Field(min_length=1, max_length=64)
    attempted_at: str

    @field_validator("attempted_at")
    @classmethod
    def timestamp(cls, value):
        instant(value)
        return value


class EngagementState(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: StrictInt = Field(default=1, ge=1, le=1)
    subscription_id: str = Field(min_length=1, max_length=64)
    owner_id: str = Field(pattern=r"^[1-9][0-9]{0,18}$")
    environment: Literal["local_test", "sandbox", "live", "scoped_live"]
    contract: Literal["3.4_id"] = "3.4_id"
    panel_host: str = Field(min_length=1, max_length=253)
    user_id: str = Field(pattern=r"^[1-9][0-9]{0,15}$")
    origin_nonce: str = Field(pattern=r"^[a-f0-9]{32}$")
    created_at: str | None = None
    creation_job_id: str | None = None
    creation_order_id: str | None = None
    ever_connected_at: str | None = None
    first_connected_at: str | None = None
    known_never_connected: bool = False
    observed_at: str | None = None
    observed_expiry: str | None = None
    observed_status: Literal["ACTIVE", "EXPIRED", "DISABLED", "LIMITED"] | None = None
    help_intent_id: str | None = None
    connected_intent_id: str | None = None
    traffic_intent_cycle: str | None = None
    traffic_intent_threshold: StrictInt | None = Field(default=None, ge=1, le=100)

    @field_validator(
        "created_at", "ever_connected_at", "first_connected_at", "observed_at", "observed_expiry"
    )
    @classmethod
    def timestamp(cls, value):
        if value is not None:
            instant(value)
        return value

    @field_validator("user_id", "owner_id")
    @classmethod
    def identifier(cls, value, info):
        maximum = 9007199254740991 if info.field_name == "user_id" else 9223372036854775807
        if int(value) > maximum:
            raise ValueError("engagement_identity_invalid")
        return value

    @model_validator(mode="after")
    def creation(self):
        values = (self.created_at, self.creation_job_id, self.creation_order_id)
        if any(v is not None for v in values) and not all(v is not None for v in values):
            raise ValueError("engagement_creation_invalid")
        return self


def state_in(session, sub):
    row = session.get(Setting, "engagement:" + sub.id)
    if row is None:
        return None
    state = EngagementState.model_validate(row.value)
    from .panel_v3 import production_binding_key

    record = session.get(
        Setting, production_binding_key(sub.id) if sub.environment == "live" else "panel_test:" + sub.id
    )
    proof = record.value if record and isinstance(record.value, dict) else {}
    binding = panel_entitlements.registered_binding(session, sub)
    if (
        state.subscription_id != sub.id
        or state.owner_id != str(sub.account_id)
        or state.environment != sub.environment
        or proof.get("origin")
        not in (
            {"new_project_identity", "imported_project_identity"}
            if sub.environment == "live"
            else {"new_test_identity"}
        )
        or proof.get("panel_host") != state.panel_host
        or proof.get("contract") != state.contract
        or not binding
        or binding.get("user_id") != state.user_id
        or binding.get("origin_nonce") != state.origin_nonce
    ):
        raise ValueError("engagement_scope_invalid")
    return state


def merge_states_in(session, subscriptions):
    return [(sub, state) for sub in subscriptions if (state := state_in(session, sub)) is not None]


def transfer_merge_in(session, states, canonical_id):
    for sub, state in states:
        if sub.account_id != canonical_id:
            raise ValueError("engagement_owner_invalid")
        session.get(Setting, "engagement:" + sub.id).value = state.model_copy(
            update={"owner_id": str(canonical_id)}
        ).model_dump()
        for job in session.scalars(
            select(Job).where(
                Job.kind == "subscription_engagement", Job.payload["subscription_id"].as_string() == sub.id
            )
        ):
            job.payload = job.payload | {"owner_id": str(canonical_id)}


class SubscriptionEngagement:
    def __init__(self, commerce, notifications=None):
        from .notifications import NotificationService

        self.commerce, self.db, self.panel = commerce, commerce.db, commerce.panel_provider
        self.settings = commerce.public_settings or getattr(commerce.accounts, "settings", None)
        self.notifications = notifications or NotificationService(self.db, self.settings)

    def _save(self, session, state):
        key = "engagement:" + state.subscription_id
        row = session.get(Setting, key)
        if row is None:
            session.add(Setting(key=key, value=state.model_dump()))
        else:
            row.value = state.model_dump()

    def _creation_in(self, session, sub, proof, metadata):
        try:
            intent = CreationIntent.model_validate(proof.get("creation_intent"))
            order = session.get(Order, intent.order_id)
            quote = session.get(Quote, order.quote_id) if order else None
            job = session.get(Job, intent.job_id)
            created = instant(metadata["created_at"])
            if (
                intent.subscription_id != sub.id
                or not panel_entitlements.owned_account(session, sub, intent.owner_id)
                or intent.environment != sub.environment
                or intent.panel_host != self.panel.api_host
                or intent.origin_nonce != proof["binding"]["origin_nonce"]
                or intent.username != proof["binding"]["username"]
                or not job
                or job.kind != "provision"
                or job.state != "done"
                or job.last_error
                or job.account_id != sub.account_id
                or job.payload.get("order_id") != intent.order_id
                or job.payload.get("creation_intent") != intent.model_dump()
                or not order
                or order.account_id != sub.account_id
                or order.subscription_id != sub.id
                or order.status != "paid"
                or order.fulfilled_at is None
                or not quote
                or quote.snapshot.get("action") != "purchase"
                or quote.snapshot.get("subscription_id") is not None
                or abs((created - instant(intent.attempted_at)).total_seconds()) > 300
                or created > now()
                or session.scalar(
                    select(Payment.id).where(Payment.order_id == order.id, Payment.status == "paid")
                )
                is None
            ):
                return {}
            return {
                "created_at": created.isoformat(),
                "creation_job_id": job.id,
                "creation_order_id": order.id,
            }
        except (ValidationError, PanelReview, KeyError, TypeError, ValueError):
            return {}

    def observe_in(self, session, sub, snapshot, *, first_event=False, threshold_event=False):
        from .panel_events import PanelEvents

        if getattr(self.panel, "contract", None) != "3.4_id":
            return
        binding, proof = PanelEvents(self.db, self.panel, self.settings)._owned(session, sub)
        row = session.scalar(select(Setting).where(Setting.key == "engagement:" + sub.id).with_for_update())
        try:
            state = state_in(session, sub) if row else None
        except (ValidationError, ValueError, PanelReview):
            return
        metadata = snapshot.get("connection_metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
        if state is None:
            state = EngagementState(
                subscription_id=sub.id,
                owner_id=str(sub.account_id),
                environment=sub.environment,
                panel_host=self.panel.api_host,
                user_id=binding["user_id"],
                origin_nonce=binding["origin_nonce"],
                **self._creation_in(session, sub, proof, metadata),
            )
        never = all(
            field in metadata for field in ("first_connected_at", "online_at", "lifetime_traffic_bytes")
        ) and (
            metadata["first_connected_at"] is None
            and metadata["online_at"] is None
            and metadata["lifetime_traffic_bytes"] == "0"
        )
        if state.created_at and metadata.get("created_at") != state.created_at:
            never = False
        connected = bool(
            metadata.get("first_connected_at")
            or metadata.get("online_at")
            or int(metadata.get("lifetime_traffic_bytes", "0")) > 0
        )
        newly_connected = connected and state.ever_connected_at is None
        if newly_connected:
            if (state.known_never_connected or first_event) and state.connected_intent_id is None:
                item = self.notifications.emit_in(
                    session,
                    sub.account_id,
                    "first-connected:" + sub.id + ":" + state.origin_nonce,
                    "connection_success",
                    "Подключение выполнено",
                    "Первое подключение к подписке подтверждено.",
                    target="/#subscriptions",
                    channel="telegram",
                    allow_push=False,
                    actions=[self.action(sub.id, "connect", "Инструкция подключения")],
                )
                state = state.model_copy(update={"connected_intent_id": item.id})
            state = state.model_copy(
                update={
                    "ever_connected_at": metadata.get("first_connected_at")
                    or metadata.get("online_at")
                    or now().isoformat(),
                    "first_connected_at": metadata.get("first_connected_at"),
                }
            )
            self._cancel_help_in(session, state)
        state = state.model_copy(
            update={
                "known_never_connected": never and state.ever_connected_at is None,
                "observed_at": now().isoformat(),
                "observed_expiry": snapshot.get("expires_at"),
                "observed_status": snapshot.get("status"),
            }
        )
        self._save(session, state)
        if threshold_event and snapshot.get("status") in {"ACTIVE", "LIMITED"}:
            from .retention import fenced_in

            used, limit, threshold = (
                snapshot.get("traffic_bytes"),
                snapshot.get("traffic_limit_bytes"),
                snapshot.get("traffic_threshold"),
            )
            cycle = snapshot.get("traffic_reset_at") or state.created_at
            if (
                type(used) is int
                and type(limit) is int
                and limit > 0
                and type(threshold) is int
                and 1 <= threshold <= 100
                and snapshot.get("traffic_cycle_known") is True
                and cycle
                and Decimal(used) * 100 >= Decimal(limit) * threshold
                and not fenced_in(session, sub)
                and panel_entitlements.local_status(session, sub) in {"active", "limited"}
                and (state.traffic_intent_cycle != cycle or (state.traffic_intent_threshold or 0) < threshold)
            ):
                self.notifications.emit_in(
                    session,
                    sub.account_id,
                    "traffic-threshold:"
                    + sub.id
                    + ":"
                    + state.origin_nonce
                    + ":"
                    + cycle
                    + ":"
                    + str(threshold),
                    "traffic_threshold",
                    "Трафик подписки",
                    f"Использовано {used} из {limit} байт. Достигнут порог {threshold}% для тарифа с ограничением трафика.",
                    channel="telegram",
                    allow_push=False,
                )
                state = state.model_copy(
                    update={"traffic_intent_cycle": cycle, "traffic_intent_threshold": threshold}
                )
                self._save(session, state)
        if state.created_at and state.ever_connected_at is None:
            key = "engagement-check:" + sub.id + ":" + state.origin_nonce
            if session.scalar(select(Job.id).where(Job.key == key)) is None:
                session.add(
                    Job(
                        key=key,
                        kind="subscription_engagement",
                        account_id=sub.account_id,
                        available_at=instant(state.created_at) + timedelta(hours=24),
                        payload={
                            "subscription_id": sub.id,
                            "owner_id": str(sub.account_id),
                            "origin_nonce": state.origin_nonce,
                            "user_id": state.user_id,
                        },
                    )
                )
        if self._help_due_in(session, sub, state, now()) and state.help_intent_id is None:
            item = self.notifications.emit_in(
                session,
                sub.account_id,
                "connection-help:" + sub.id + ":" + state.origin_nonce,
                "connection_help",
                "Помощь с подключением",
                "Подписка создана больше суток назад, но первого подключения пока нет. Откройте инструкцию подключения. Если нужна помощь, обратитесь в поддержку.",
                target="/#subscriptions",
                channel="telegram",
                allow_push=False,
                actions=[self.action(sub.id, "connect", "Инструкция подключения")],
            )
            self._save(session, state.model_copy(update={"help_intent_id": item.id}))

    @staticmethod
    def action(sid, operation, label, quote_id=None):
        return {
            "label": label,
            "target": "/#subscriptions",
            "operation": operation,
            "metadata": {"subscription_id": sid, **({"quote_id": quote_id} if quote_id else {})},
        }

    def _cancel_help_in(self, session, state):
        if state.help_intent_id:
            for delivery in session.scalars(
                select(NotificationDelivery)
                .where(
                    NotificationDelivery.notification_id == state.help_intent_id,
                    NotificationDelivery.state.in_(["pending", "retry", "in_flight"]),
                )
                .with_for_update()
            ):
                delivery.state, delivery.last_error = "cancelled", "connection_help_resolved"

    def _help_due_in(self, session, sub, state, moment):
        from .retention import fenced_in

        if (
            state is None
            or state.created_at is None
            or not state.known_never_connected
            or state.ever_connected_at is not None
            or state.observed_status != "ACTIVE"
            or state.observed_at is None
            or not timedelta(0) <= moment - instant(state.observed_at) <= timedelta(minutes=10)
            or state.observed_expiry is None
            or instant(state.observed_expiry) <= moment
            or instant(state.created_at) + timedelta(hours=24) > moment
            or panel_entitlements.local_status(session, sub) != "active"
            or panel_entitlements.remote_status(session, sub)
            or panel_entitlements.pending_target(session, sub)
            or fenced_in(session, sub)
            or panel_entitlements.current_segment(session, sub, moment) is None
        ):
            return False
        try:
            self.commerce._assert_subscription_operations(session, sub)
        except DomainError:
            return False
        scoped = getattr(self.panel, "scoped_runtime_policy", None)
        if scoped:
            scoped.require_current(session)
            scoped.require_subscription_in(session, sub)
        order = session.get(Order, state.creation_order_id)
        if not order or order.status != "paid" or order.fulfilled_at is None:
            return False
        if scoped:
            try:
                scoped.require_paid_service_in(session, order)
            except DomainError:
                return False
        unresolved_inbox = select(Inbox.id).where(Inbox.state != "processed")
        if scoped:
            unresolved_inbox = unresolved_inbox.where(
                Inbox.payload["order_id"].as_string().in_(
                    select(Order.id).where(
                        Order.account_id == sub.account_id, Order.environment == "scoped_live"
                    )
                )
            )
        if (
            session.scalar(
                select(Order.id)
                .where(
                    Order.account_id == sub.account_id,
                    *((Order.environment == "scoped_live",) if scoped else ()),
                    (Order.status.not_in(["paid", "canceled", "expired", "refunded"]))
                    | ((Order.status == "paid") & Order.fulfilled_at.is_(None)),
                )
                .limit(1)
            )
            is not None
        ):
            return False
        if (
            session.scalar(
                select(RefundReview.id)
                .join(Payment, Payment.id == RefundReview.payment_id)
                .where(
                    Payment.account_id == sub.account_id,
                    *((Payment.environment == "scoped_live",) if scoped else ()),
                    RefundReview.resolved_at.is_(None),
                )
                .limit(1)
            )
            is not None
            or session.scalar(unresolved_inbox.limit(1)) is not None
        ):
            return False
        return (
            session.scalar(
                select(ProviderOperation.id)
                .where(
                    ProviderOperation.order_id.in_(
                        select(Order.id).where(
                            Order.account_id == sub.account_id,
                            *((Order.environment == "scoped_live",) if scoped else ()),
                        )
                    ),
                    ProviderOperation.state != "reconciled",
                )
                .limit(1)
            )
            is None
        )

    def help_notice_current_in(self, session, sub, notice_id):
        """Read-only current proof for a previously emitted connection-help intent."""
        try:
            scoped = getattr(self.panel, "scoped_runtime_policy", None)
            if scoped:
                scoped.require_current(session)
                scoped.require_subscription_in(session, sub)
            state = state_in(session, sub)
            return bool(
                state
                and state.help_intent_id == notice_id
                and self._help_due_in(session, sub, state, now())
            )
        except (DomainError, PanelReview, ValidationError, ValueError):
            return False

    def projection_in(self, session, sub):
        try:
            state = state_in(session, sub)
        except (ValidationError, ValueError, PanelReview):
            state = None
        progress, warning = None, False
        record = panel_entitlements.verified_observation(session, sub)
        from .retention import fenced_in

        if (
            record
            and not fenced_in(session, sub)
            and panel_entitlements.facts(session, sub)["panel_sync_state"] == "fresh"
            and timedelta(0) <= now() - record.observed_at <= timedelta(minutes=10)
            and record.status in {"ACTIVE", "LIMITED"}
        ):
            used, limit = record.traffic_bytes, record.traffic_limit_bytes
            if (
                type(used) is int
                and type(limit) is int
                and 0 <= used <= 9007199254740991
                and 0 < limit <= 9007199254740991
            ):
                progress = str((Decimal(used) * 100 / Decimal(limit)).quantize(Decimal("0.01")))
                threshold = record.payload.get("traffic_threshold")
                warning = (
                    type(threshold) is int
                    and 1 <= threshold <= 100
                    and Decimal(used) * 100 >= Decimal(limit) * threshold
                )
        return {
            "connection_help_due": self._help_due_in(session, sub, state, now()),
            "ever_connected": bool(state and state.ever_connected_at),
            "first_connected_at": state.first_connected_at if state else None,
            "traffic_progress_percent": progress,
            "traffic_warning": warning,
        }

    def run_due(self, limit=50):
        with self.db.transaction() as session:
            candidates = list(
                session.execute(
                    select(Job.id, Job.account_id, Job.payload)
                    .where(
                        Job.kind == "subscription_engagement",
                        Job.state.in_(["pending", "in_flight"]),
                        Job.available_at <= now(),
                    )
                    .order_by(Job.available_at, Job.id)
                    .limit(limit)
                )
            )
        completed = 0
        for jid, owner, payload in candidates:
            sid, lease = payload.get("subscription_id"), uuid4().hex
            claimed = False
            try:
                with self.db.transaction() as session:
                    session.scalar(select(Account).where(Account.id == owner).with_for_update())
                    sub = session.scalar(select(Subscription).where(Subscription.id == sid).with_for_update())
                    job = session.scalar(select(Job).where(Job.id == jid).with_for_update())
                    if (
                        not sub
                        or sub.account_id != owner
                        or not job
                        or job.account_id != owner
                        or job.available_at > now()
                        or job.state not in {"pending", "in_flight"}
                    ):
                        continue
                    state = state_in(session, sub)
                    from .panel_events import PanelEvents
                    from .retention import fenced_in

                    binding, _ = PanelEvents(self.db, self.panel, self.settings)._owned(session, sub)
                    if (
                        state is None
                        or state.created_at is None
                        or payload
                        != {
                            "subscription_id": sid,
                            "owner_id": str(owner),
                            "origin_nonce": state.origin_nonce,
                            "user_id": state.user_id,
                        }
                    ):
                        raise PanelReview("engagement_scope_invalid")
                    if (
                        state.ever_connected_at
                        or fenced_in(session, sub)
                        or panel_entitlements.local_status(session, sub) != "active"
                        or panel_entitlements.remote_status(session, sub)
                    ):
                        job.state = "done"
                        continue
                    if panel_entitlements.pending_target(session, sub):
                        job.available_at = now() + timedelta(minutes=2)
                        continue
                    version = sub.version
                    job.state, job.available_at = "in_flight", now() + timedelta(minutes=2)
                    job.attempts += 1
                    job.last_error = lease
                    claimed = True
                snapshot = self.panel.engagement_snapshot(binding)
                with self.db.transaction() as session:
                    session.scalar(select(Account).where(Account.id == owner).with_for_update())
                    sub = session.scalar(select(Subscription).where(Subscription.id == sid).with_for_update())
                    job = session.scalar(select(Job).where(Job.id == jid).with_for_update())
                    if (
                        not sub
                        or sub.account_id != owner
                        or not job
                        or job.account_id != owner
                        or job.last_error != lease
                    ):
                        continue
                    current, _ = PanelEvents(self.db, self.panel, self.settings)._owned(session, sub)
                    if current != binding or sub.version != version or fenced_in(session, sub):
                        raise PanelUnavailable("engagement_scope_changed")
                    self.observe_in(session, sub, snapshot)
                    job.state, job.last_error = "done", None
                    completed += 1
            except (PanelReview, PanelUnavailable, ValidationError, ValueError, DomainError):
                with self.db.transaction() as session:
                    session.scalar(select(Account).where(Account.id == owner).with_for_update())
                    sub = session.scalar(select(Subscription).where(Subscription.id == sid).with_for_update())
                    job = session.scalar(select(Job).where(Job.id == jid).with_for_update())
                    if (
                        not sub
                        or sub.account_id != owner
                        or not job
                        or job.account_id != owner
                        or job.state == "done"
                    ):
                        continue
                    if claimed:
                        if job.state != "in_flight" or job.last_error != lease or job.payload != payload:
                            continue
                        try:
                            current, _ = PanelEvents(self.db, self.panel, self.settings)._owned(session, sub)
                        except ValueError:
                            continue
                        if current != binding or sub.version != version:
                            continue
                    elif job.state != "pending" or job.payload != payload or job.attempts != 0:
                        continue
                    job.state = "review" if job.attempts == 0 else "done" if job.attempts >= 3 else "pending"
                    job.available_at = now() + timedelta(seconds=30)
                    job.last_error = "connection_evidence_unavailable"
        return completed
