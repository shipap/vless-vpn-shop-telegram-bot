"""Local project-node chronology and a single explicitly authorized owner CRM intent."""

import hashlib
import json
import re
from datetime import timedelta
from types import SimpleNamespace
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from .db import Account, Alias, Notification, Setting, StaffRole, Subscription, now, uid
from .panel import PanelReview, PanelUnavailable
from .panel_event_routes import policy_in, registration_context
from .panel_v3 import instant
from .staff_permissions import StaffPermissions

CRM_EVENT = "crm.infra_billing_node_payment_in_48hrs"


class NodeNews(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: StrictInt = Field(default=1, ge=1, le=1)
    id: str
    node_uuid: str
    panel_origin: str
    environment: Literal["local_test", "sandbox", "live", "scoped_live"]
    contract: Literal["3.4_id"] = "3.4_id"
    state: Literal["outage", "recovered"]
    occurred_at: str

    @field_validator("occurred_at")
    @classmethod
    def stamp(cls, value):
        instant(value)
        return value


class InfraIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: StrictInt = Field(default=1, ge=1, le=1)
    owner_id: str = Field(pattern=r"^[1-9][0-9]{0,18}$")
    notification_id: str
    panel_origin: str
    environment: Literal["local_test", "sandbox", "live"]
    provider_name: str
    node_name: str
    next_billing_at: str


def record_node_news_in(session, panel, settings, node_id, snapshot, events):
    policy = policy_in(session, panel, settings)
    route = next((route for route in policy.nodes if route.node_uuid == node_id), None) if policy else None
    if route is None:
        return
    # Signed scoped timestamps are historical event facts, not current node status.
    # The caller has independently fetched this exact mapped node; its observation
    # remains authoritative even when a delayed outage is added to chronology.
    if snapshot.get("status") not in {"CONNECTED", "DISCONNECTED", "DISABLED"}:
        return
    for _, _, kind, payload in sorted(events, key=lambda item: item[3]["timestamp"]):
        state = {
            "node.connection_lost": "outage",
            "node.disabled": "outage",
            "node.connection_restored": "recovered",
            "node.enabled": "recovered",
        }.get(kind)
        if state is None:
            continue
        stamp = instant(payload["timestamp"])
        if stamp < now() - timedelta(hours=24):
            continue
        key = "node_news:" + hashlib.sha256((node_id + kind + payload["timestamp"]).encode()).hexdigest()
        if session.get(Setting, key) is None:
            session.add(
                Setting(
                    key=key,
                    value=NodeNews(
                        id=uid(),
                        node_uuid=node_id,
                        panel_origin=policy.panel_origin,
                        environment=policy.environment,
                        state=state,
                        occurred_at=stamp.isoformat(),
                    ).model_dump(),
                )
            )


def node_news(commerce, account_id, *, before=None, limit=20, moment=None):
    from .panel_events import PanelEvents
    from .retention import fenced_in

    limit = max(1, min(int(limit), 100))
    cutoff = (moment or now()) - timedelta(hours=24)
    settings = commerce.public_settings or getattr(commerce.accounts, "settings", None)
    with commerce.db.transaction() as session:
        account = commerce._account(session, account_id)
        policy = policy_in(session, commerce.panel_provider, settings)
        if policy is None:
            return {"items": [], "next_cursor": None}
        products = set()
        for sub in session.scalars(select(Subscription).where(Subscription.account_id == account.id)):
            if getattr(commerce, "scoped_payments", False) and sub.environment != "scoped_live":
                continue
            try:
                if not fenced_in(session, sub):
                    PanelEvents(commerce.db, commerce.panel_provider, settings)._owned(session, sub)
                    if sub.product_id:
                        products.add(sub.product_id)
            except ValueError:
                continue
        routes = {entry.node_uuid: entry for entry in policy.nodes if products & set(entry.product_ids)}
        items = []
        expired_ids = set()
        for row in session.scalars(select(Setting).where(Setting.key.like("node_news:%"))):
            try:
                item = NodeNews.model_validate(row.value)
                if (
                    item.node_uuid in routes
                    and item.panel_origin == policy.panel_origin
                    and item.environment == policy.environment
                    and item.contract == policy.contract
                ):
                    if instant(item.occurred_at) < cutoff:
                        expired_ids.add(item.id)
                    else:
                        items.append(item)
            except (ValidationError, ValueError, PanelReview):
                continue
        items.sort(key=lambda entry: (instant(entry.occurred_at), entry.id), reverse=True)
        if before:
            index = next((index for index, item in enumerate(items) if item.id == before), None)
            if index is None:
                if before in expired_ids:
                    return {"items": [], "next_cursor": None}
                from sunrise_core.errors import missing

                raise missing()
            items = items[index + 1 :]
        selected = items[:limit]
        return {
            "items": [
                {
                    "id": item.id,
                    "title": routes[item.node_uuid].safe_label,
                    "state": item.state,
                    "occurred_at": item.occurred_at,
                }
                for item in selected
            ],
            "next_cursor": selected[-1].id if len(items) > limit else None,
        }


def cleanup_node_news(db, panel, settings, *, moment=None, after=None, limit=200):
    """Delete one bounded page of expired news from this admitted panel only."""
    cutoff = (moment or now()) - timedelta(hours=24)
    limit = max(1, min(int(limit), 200))
    context = registration_context(panel, settings)
    if context is None or context["environment"] not in {"live", "scoped_live"}:
        return 0, None
    with db.transaction() as session:
        production = getattr(panel, "runtime_policy", None)
        scoped = getattr(panel, "scoped_runtime_policy", None)
        if production and production.production:
            production.require_current(session)
            allowed = set(production.scope.panel.node_uuids) & panel.nodes
        elif scoped and scoped.scoped_payments:
            scoped.require_current(session)
            allowed = set(scoped.scope.panel.node_uuids) & set(settings.panel_test_node_uuids) & panel.nodes
        else:
            return 0, None
        query = select(Setting).where(Setting.key.like("node_news:%"))
        if after:
            query = query.where(Setting.key > after)
        rows = list(session.scalars(query.order_by(Setting.key).limit(limit).with_for_update()))
        deleted = 0
        for row in rows:
            if not re.fullmatch(r"node_news:[a-f0-9]{64}", row.key):
                continue
            try:
                item = NodeNews.model_validate(row.value)
                if (
                    item.node_uuid in allowed
                    and item.panel_origin == context["panel_origin"]
                    and item.environment == context["environment"]
                    and item.contract == context["contract"]
                    and instant(item.occurred_at) < cutoff
                ):
                    session.delete(row)
                    deleted += 1
            except (ValidationError, ValueError, PanelReview):
                continue
        return deleted, rows[-1].key if len(rows) == limit else None


def owner_account_in(session, settings):
    """Select one explicit current canonical owner, never infer a grant through an alias."""
    from .staff_permissions import telegram_admin_ids

    if not telegram_admin_ids(settings):
        return None
    try:
        with session.begin_nested():
            permissions = StaffPermissions(SimpleNamespace(db=None, settings=settings))
            owners = []
            candidates = session.scalars(
                select(Account)
                .join(StaffRole, StaffRole.account_id == Account.id)
                .where(StaffRole.role == "owner")
                .order_by(Account.id)
                .with_for_update(of=Account, nowait=True)
                .execution_options(populate_existing=True)
            )
            for account in candidates:
                # Archive already owns the customer row. Never wait for a
                # different owner row that a sorted identity merge may hold.
                grant = session.get(
                    StaffRole, account.id, populate_existing=True, with_for_update={"nowait": True}
                )
                alias = session.get(Alias, account.id, populate_existing=True)
                if (
                    grant
                    and grant.role == "owner"
                    and alias is None
                    and permissions.role_in(session, account) == "owner"
                ):
                    owners.append(account)
            # Successful locks remain until the outer transaction commits,
            # including the Notification account FK check performed by flush.
            return owners[0] if len(owners) == 1 else None
    except OperationalError as error:
        if getattr(error.orig, "sqlstate", None) == "55P03":
            raise PanelUnavailable("owner_notice_busy") from None
        raise


def crm_intent_in(session, panel, settings, events, notifications):
    policy = policy_in(session, panel, settings)
    if policy is None:
        return {"status": "denied"}
    mapped = []
    for _, _, _, payload in events:
        route = next(
            (
                route
                for route in policy.infra
                if route.provider_name == payload["provider_name"] and route.node_name == payload["node_name"]
            ),
            None,
        )
        if route is None:
            return {"status": "denied"}
        mapped.append((route, payload))
    owner = owner_account_in(session, settings)
    if owner is None:
        return {"status": "error", "error": "owner_notice_unavailable"}
    for route, payload in mapped:
        key = (
            "infra_notice:"
            + hashlib.sha256(
                json.dumps(
                    [
                        policy.panel_origin,
                        policy.environment,
                        route.provider_name,
                        route.node_name,
                        payload["next_billing_at"],
                    ],
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
        )
        row = session.scalar(select(Setting).where(Setting.key == key).with_for_update())
        if row is not None:
            try:
                intent = InfraIntent.model_validate(row.value)
                alias = session.get(Alias, int(intent.owner_id))
                canonical = alias.canonical_id if alias else int(intent.owner_id)
                notice = session.get(Notification, intent.notification_id)
                if (
                    canonical != owner.id
                    or notice is None
                    or notice.account_id != owner.id
                    or notice.kind != "infra_billing_due"
                    or notice.audience != "owner"
                ):
                    raise ValueError
            except (ValidationError, ValueError):
                return {"status": "error", "error": "panel_review_required"}
            continue
        notice = notifications.emit_in(
            session,
            owner.id,
            key,
            "infra_billing_due",
            "Оплата сервера через 48 часов",
            f"Сервер: {route.safe_label}\nСрок оплаты UTC по событию панели: {payload['next_billing_at']}\nСумма и валюта: неизвестна. Автоматическая оплата не выполняется.",
            channel="auto",
            owner_only=True,
            allow_push=False,
        )
        session.add(
            Setting(
                key=key,
                value=InfraIntent(
                    owner_id=str(owner.id),
                    notification_id=notice.id,
                    panel_origin=policy.panel_origin,
                    environment=policy.environment,
                    provider_name=route.provider_name,
                    node_name=route.node_name,
                    next_billing_at=payload["next_billing_at"],
                ).model_dump(),
            )
        )
    return {"status": "ok"}
