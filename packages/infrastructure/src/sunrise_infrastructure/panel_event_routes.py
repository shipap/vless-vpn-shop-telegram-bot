"""Explicit audited project associations; webhook names are never scope authority."""

import hashlib
import json
import re
import time
from datetime import timedelta
from typing import Literal
from urllib.parse import urlparse
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
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import OperationalError
from sunrise_core.errors import DomainError

from .db import Audit, Job, Product, Setting, now
from .panel import PanelReview, PanelUnavailable

SYNC_JOB_KIND = "panel_route_sync"
SYNC_LEASE = timedelta(minutes=5)
SYNC_INTERVAL = timedelta(days=1)
MAX_SYNC_GROUPS = 11  # Two 10 s GETs per group plus one hosts GET fit the 240 s budget.


def _caption_text(value):
    value = re.sub(r"[\U0001F1E6-\U0001F1FF]{2}\ufe0f?", "", value)
    return re.sub(r"#(?=[0-9])", "", value)


class NodeRoute(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    node_uuid: str
    product_ids: list[str] = Field(min_length=1, max_length=100)
    safe_label: str = Field(min_length=1, max_length=80)

    @field_validator("node_uuid")
    @classmethod
    def uuid(cls, value):
        if str(UUID(value)) != value:
            raise ValueError("node_uuid_invalid")
        return value

    @field_validator("safe_label")
    @classmethod
    def label(cls, value):
        plain = _caption_text(value)
        if (
            value != value.strip()
            or not plain.strip()
            or not re.fullmatch(r"[\w ()—–-]+", plain, re.UNICODE)
            or re.search(r"[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-", plain)
        ):
            raise ValueError("public_node_label_invalid")
        return value

    @field_validator("product_ids")
    @classmethod
    def products(cls, value):
        if len(set(value)) != len(value) or any(
            not 1 <= len(item) <= 64 or item != item.strip() for item in value
        ):
            raise ValueError("node_products_invalid")
        return value


class InfraRoute(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    provider_name: str = Field(min_length=1, max_length=200)
    node_name: str = Field(min_length=1, max_length=200)
    safe_label: str = Field(min_length=1, max_length=80)
    _label = field_validator("safe_label")(NodeRoute.label.__func__)

    @field_validator("provider_name", "node_name")
    @classmethod
    def name(cls, value):
        if value != value.strip() or any(ord(character) < 32 for character in value):
            raise ValueError("infra_pair_invalid")
        return value


class PanelEventRoutes(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: StrictInt = Field(ge=1, le=1)
    environment: Literal["local_test", "sandbox", "live", "scoped_live"]
    panel_origin: str
    contract: Literal["3.4_id"]
    nodes: list[NodeRoute] = Field(max_length=100)
    infra: list[InfraRoute] = Field(max_length=100)

    @field_validator("panel_origin")
    @classmethod
    def origin(cls, value):
        parsed = urlparse(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or value != "https://" + parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("panel_origin_invalid")
        return value

    @model_validator(mode="after")
    def unique_routes(self):
        if len({node.node_uuid for node in self.nodes}) != len(self.nodes) or len(
            {(entry.provider_name, entry.node_name) for entry in self.infra}
        ) != len(self.infra):
            raise ValueError("panel_routes_ambiguous")
        return self


def registration_context(panel, settings):
    if (
        panel is None
        or getattr(panel, "contract", None) != "3.4_id"
        or str(panel.client.base_url) != "https://" + panel.api_host + "/api/"
    ):
        return None
    return {
        "version": 1,
        "environment": "live"
        if getattr(settings, "runtime_mode", "isolated") == "production"
        else "scoped_live"
        if getattr(settings, "runtime_mode", "isolated") == "scoped_test"
        else "local_test"
        if getattr(settings, "local_test", False)
        else "sandbox",
        "panel_origin": "https://" + panel.api_host,
        "contract": "3.4_id",
    }


def validate_scope(session, panel, settings, policy):
    context = registration_context(panel, settings)
    if context is None or {key: getattr(policy, key) for key in context} != context:
        raise DomainError(
            "panel_event_routes_scope_denied", "Проверьте версию и разрешённое окружение панели.", 409
        )
    allowed_nodes = set(getattr(settings, "panel_test_node_uuids", ())) & panel.nodes
    if getattr(panel, "runtime_policy", None) and panel.runtime_policy.production:
        panel.runtime_policy.require_current(session)
        allowed_nodes = set(panel.runtime_policy.scope.panel.node_uuids) & panel.nodes
    scoped = getattr(panel, "scoped_runtime_policy", None)
    if scoped:
        scoped.require_current(session)
        allowed_nodes &= set(scoped.scope.panel.node_uuids)
    products = set(session.scalars(select(Product.id).where(Product.available)))
    if any(
        node.node_uuid not in allowed_nodes or not set(node.product_ids) <= products for node in policy.nodes
    ):
        raise DomainError(
            "panel_event_routes_scope_denied", "Сервер или тариф не относится к разрешённому проекту.", 409
        )
    return policy


def policy_in(session, panel, settings):
    row = session.get(Setting, "panel_event_routes")
    if row is None:
        return None
    try:
        return validate_scope(session, panel, settings, PanelEventRoutes.model_validate(row.value))
    except (ValidationError, DomainError, ValueError):
        return None


def accessible_node_ids(response, squad):
    """Validate the complete official accessible-nodes response before intersecting admission."""
    if type(response) is not dict or response.get("squadUuid") != squad:
        raise ValueError("squad_response_identity")
    entries = response.get("accessibleNodes")
    if type(entries) is not list or len(entries) > 1000:
        raise ValueError("squad_response_size")
    values = []
    for entry in entries:
        value = entry.get("uuid") if type(entry) is dict else None
        if type(value) is not str:
            raise ValueError("squad_node_identity")
        try:
            if str(UUID(value)) != value:
                raise ValueError("squad_node_identity")
        except ValueError:
            raise ValueError("squad_node_identity") from None
        values.append(value)
    if len(values) != len(set(values)):
        raise ValueError("squad_nodes_ambiguous")
    return set(values)


def _uuid_set(values):
    if type(values) is not list or len(values) > 1000:
        raise ValueError("host_identity_unverified")
    if any(type(value) is not str or str(UUID(value)) != value for value in values):
        raise ValueError("host_identity_unverified")
    if len(values) != len(set(values)):
        raise ValueError("host_identity_ambiguous")
    return set(values)


def host_node_labels(accessible, details, hosts, allowed):
    """Resolve one public Host caption through every admitted squad's exact inbound chain."""
    if set(accessible) != set(details) or type(hosts) is not list or len(hosts) > 1000:
        raise ValueError("host_snapshot_unverified")
    nodes, inbounds = {}, {}
    for squad, response in accessible.items():
        _uuid_set([squad])
        accessible_node_ids(response, squad)
        for entry in response["accessibleNodes"]:
            node = entry["uuid"]
            if node not in allowed:
                continue
            profile, tags = entry.get("configProfileUuid"), entry.get("activeInbounds")
            _uuid_set([profile])
            if (
                type(tags) is not list or len(tags) > 1000
                or any(type(tag) is not str for tag in tags) or len(tags) != len(set(tags))
            ):
                raise ValueError("host_inbound_tags_unverified")
            nodes.setdefault(node, {})[squad] = (profile, set(tags))
        metadata = details[squad]
        entries = metadata.get("inbounds") if type(metadata) is dict else None
        info = metadata.get("info") if type(metadata) is dict else None
        if (
            type(metadata) is not dict or metadata.get("uuid") != squad
            or type(entries) is not list or len(entries) > 1000 or type(info) is not dict
            or type(info.get("inboundsCount")) is not int or info["inboundsCount"] != len(entries)
            or any(type(entry) is not dict for entry in entries)
        ):
            raise ValueError("host_squad_snapshot_unverified")
        _uuid_set([entry.get("uuid") for entry in entries])
        mapped = {}
        for entry in entries:
            _uuid_set([entry.get("profileUuid")])
            if type(entry.get("tag")) is not str:
                raise ValueError("host_inbound_tag_unverified")
            mapped[entry["uuid"]] = (entry["profileUuid"], entry["tag"])
        inbounds[squad] = mapped
    if any(type(host) is not dict for host in hosts):
        raise ValueError("host_snapshot_unverified")
    _uuid_set([host.get("uuid") for host in hosts])
    candidates = {node: set() for node in nodes}
    unsafe = set()
    for host in hosts:
        linked = _uuid_set(host.get("nodes")) & nodes.keys()
        if not linked:
            continue
        if type(host.get("isDisabled")) is not bool or type(host.get("isHidden")) is not bool:
            raise ValueError("host_flags_unverified")
        if host["isDisabled"] or host["isHidden"]:
            continue
        inbound, membership = host.get("inbound"), host.get("internalSquads")
        if type(inbound) is not dict or type(membership) is not dict:
            raise ValueError("host_eligibility_unverified")
        profile, identity = inbound.get("configProfileUuid"), inbound.get("configProfileInboundUuid")
        if profile is None or identity is None:
            continue
        _uuid_set([profile, identity] if profile != identity else [profile])
        mode, squads = membership.get("mode"), _uuid_set(membership.get("squads"))
        if mode not in {"EXCLUDE", "ALLOW_ONLY"}:
            raise ValueError("host_eligibility_unverified")
        for node in linked:
            eligible = True
            for squad, (node_profile, active_tags) in nodes[node].items():
                relation = inbounds[squad].get(identity)
                if (
                    node_profile != profile or relation is None or relation[0] != profile
                    or relation[1] not in active_tags or (squad in squads) != (mode == "ALLOW_ONLY")
                ):
                    eligible = False
                    break
            if not eligible:
                continue
            try:
                remark, address = host.get("remark"), host.get("address")
                if type(remark) is not str or type(address) is not str or (
                    address and address.casefold() in _caption_text(remark).casefold()
                ):
                    raise ValueError("host_caption_unverified")
                label = NodeRoute.model_validate({
                    "node_uuid": node, "product_ids": ["host-label"], "safe_label": remark,
                }).safe_label
            except ValueError:
                unsafe.add(node)
            else:
                candidates[node].add(label)
    result = {}
    for node, labels in candidates.items():
        if not labels or node in unsafe:
            continue
        try:
            result[node] = NodeRoute.model_validate({
                "node_uuid": node, "product_ids": ["host-label"], "safe_label": " — ".join(sorted(labels)),
            }).safe_label
        except ValueError:
            continue
    return result


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _sync_context(session, panel, settings, *, lock_products=False):
    context = registration_context(panel, settings)
    if not context or context["environment"] not in {"live", "scoped_live"}:
        raise ValueError("panel_route_sync_disabled")
    production = getattr(panel, "runtime_policy", None)
    scoped = getattr(panel, "scoped_runtime_policy", None)
    if production and production.production:
        production.require_current(session)
        allowed_nodes = set(production.scope.panel.node_uuids) & panel.nodes
        allowed_squads = set(production.scope.panel.internal_squads) & panel.squads
    elif scoped and scoped.scoped_payments:
        scoped.require_current(session)
        allowed_nodes = set(scoped.scope.panel.node_uuids) & set(settings.panel_test_node_uuids) & panel.nodes
        allowed_squads = set(scoped.scope.panel.internal_squads) & panel.squads
    else:
        raise ValueError("panel_route_sync_disabled")
    query = select(Product).where(Product.available).order_by(Product.id)
    products = [
        {"id": row.id, "version": row.version, "squad": row.internal_squad_uuid}
        for row in session.scalars(query.with_for_update() if lock_products else query)
    ]
    squads = {item["squad"] for item in products}
    if (
        len(squads) > MAX_SYNC_GROUPS
        or any(
            type(item["version"]) is not int
            or item["version"] < 1
            or type(item["squad"]) is not str
            or item["squad"] not in allowed_squads
            for item in products
        )
    ):
        raise ValueError("panel_route_sync_product_denied")
    return context, allowed_nodes, sorted(squads), products


def _route_value(session, panel, settings):
    row = session.get(Setting, "panel_event_routes")
    value = row.value if row else None
    if value is not None:
        parsed = PanelEventRoutes.model_validate(value)
        context = registration_context(panel, settings)
        if context is None or {key: getattr(parsed, key) for key in context} != context:
            raise ValueError("panel_route_sync_context_denied")
    return value


def sync_accessible_nodes(db, panel, settings, *, moment=None):
    """Refresh admitted node membership with a durable single-flight and final CAS.

    Returns current/ok/busy/error; every provider GET runs after the claim transaction.
    """
    moment = moment or now()
    token = uuid4().hex
    registration = registration_context(panel, settings)
    if registration is None or registration["environment"] not in {"live", "scoped_live"}:
        return {"status": "error", "error": "panel_route_sync_disabled"}
    job_key = "panel-route-sync:" + registration["environment"]
    try:
        with db.transaction() as session:
            context, allowed, squads, products = _sync_context(session, panel, settings)
            prior = _route_value(session, panel, settings)
            authority = getattr(panel, "runtime_policy", None) or getattr(
                panel, "scoped_runtime_policy", None
            )
            basis_sha = _digest({
                "context": context,
                "scope_id": authority.scope.scope_id,
                "allowed": sorted(allowed),
                "squads": squads,
                "products": products,
            })
            session.execute(
                insert(Job)
                .values(key=job_key, kind=SYNC_JOB_KIND, payload={}, state="pending", available_at=moment)
                .on_conflict_do_nothing(index_elements=[Job.key])
            )
            job = session.scalar(select(Job).where(Job.key == job_key).with_for_update())
            if job.kind != SYNC_JOB_KIND:
                raise ValueError("panel_route_sync_job_denied")
            previous = job.payload or {}
            lease_until = previous.get("lease_until")
            if job.state == "running" and lease_until and moment < _parse_time(lease_until):
                return {"status": "busy"}
            success_at = previous.get("success_at")
            if (
                job.state == "done"
                and success_at
                and moment < _parse_time(success_at) + SYNC_INTERVAL
            ):
                return {"status": "current"}
            if (
                job.state == "pending"
                and moment < job.available_at
            ):
                return {"status": "busy"}
            seed_query = select(Audit).where(
                Audit.action == "panel_event_routes_technical_init", Audit.target == "panel_event_routes"
            ).order_by(Audit.created_at.desc(), Audit.id.desc()).limit(1)
            technical_seed = session.scalar(seed_query) if prior else None
            legacy_auto = bool(
                technical_seed and type(technical_seed.details) is dict
                and technical_seed.details.get("policy_sha256") == _digest(prior)
            )
            seed_id = technical_seed.id if legacy_auto else None
            job.state = "running"
            job.payload = previous | {
                "token": token,
                "lease_until": (moment + SYNC_LEASE).isoformat(),
            }
            job.available_at = moment + SYNC_LEASE
        accessible, details, hosts = {}, {}, []
        reads = [(squad, "internal-squads/" + squad + "/accessible-nodes", accessible) for squad in squads]
        reads += [(squad, "internal-squads/" + squad, details) for squad in squads]
        if squads:
            reads.append((None, "hosts", None))
        started = time.monotonic()
        for squad, path, destination in reads:
            if time.monotonic() - started >= 240:
                raise ValueError("panel_route_sync_expired")
            with db.transaction() as session:
                fresh_context, fresh_allowed, fresh_squads, fresh_products = _sync_context(
                    session, panel, settings
                )
                if (fresh_context, fresh_allowed, fresh_squads, fresh_products) != (
                    context, allowed, squads, products
                ):
                    raise ValueError("panel_route_sync_basis_changed")
            response = panel._request("GET", path)
            if time.monotonic() - started >= 240:
                raise ValueError("panel_route_sync_expired")
            if destination is None:
                hosts = response
            else:
                destination[squad] = response
                if destination is accessible:
                    accessible_node_ids(response, squad)
        memberships = {squad: accessible_node_ids(response, squad) & allowed for squad, response in accessible.items()}
        captions = host_node_labels(accessible, details, hosts, allowed)
        by_node = {}
        for product in products:
            for node in memberships[product["squad"]]:
                by_node.setdefault(node, []).append(product["id"])
        old_labels = {
            node["node_uuid"]: node["safe_label"] for node in (prior or {}).get("nodes", [])
        }
        nodes, generated_labels, panel_labels = [], {}, {}
        derived = previous.get("generated_labels", {}) | previous.get("panel_labels", {})
        for index, (node, ids) in enumerate(sorted(by_node.items()), 1):
            automatic = node not in old_labels or legacy_auto or derived.get(node) == old_labels[node]
            label = captions.get(node, f"Сервер {index}") if automatic else old_labels[node]
            nodes.append(NodeRoute(node_uuid=node, product_ids=sorted(ids), safe_label=label).model_dump())
            if automatic:
                (panel_labels if node in captions else generated_labels)[node] = label
        value = PanelEventRoutes.model_validate(
            context | {"nodes": nodes, "infra": (prior or {}).get("infra", [])}
        ).model_dump()
        with db.transaction() as session:
            session.execute(text("SET LOCAL lock_timeout = '5s'"))
            session.execute(select(func.pg_advisory_xact_lock(617045501)))
            session.execute(select(func.pg_advisory_xact_lock(617045503)))
            session.execute(select(func.pg_advisory_xact_lock(617045504)))
            current = session.scalar(select(Job).where(Job.key == job_key).with_for_update())
            if (
                current is None
                or current.state != "running"
                or current.payload.get("token") != token
                or now() >= _parse_time(current.payload["lease_until"])
            ):
                return {"status": "busy"}
            latest_context, latest_allowed, latest_squads, latest_products = _sync_context(
                session, panel, settings, lock_products=True
            )
            route_row = session.scalar(
                select(Setting).where(Setting.key == "panel_event_routes").with_for_update()
            )
            latest_seed = session.scalar(seed_query.with_for_update()) if legacy_auto else None
            if (
                latest_context != context
                or latest_allowed != allowed
                or latest_squads != squads
                or latest_products != products
                or (route_row.value if route_row else None) != prior
                or legacy_auto and (
                    latest_seed is None or latest_seed.id != seed_id
                    or type(latest_seed.details) is not dict
                    or latest_seed.details.get("policy_sha256") != _digest(prior)
                )
            ):
                current.state = "pending"
                current.available_at = now() + timedelta(seconds=30)
                current.last_error = "panel_route_sync_conflict"
                return {"status": "error", "error": "panel_route_sync_conflict"}
            validate_scope(session, panel, settings, PanelEventRoutes.model_validate(value))
            if now() >= _parse_time(current.payload["lease_until"]):
                return {"status": "busy"}
            if route_row:
                route_row.value = value
            else:
                session.add(Setting(key="panel_event_routes", value=value))
            current.state = "done"
            current.last_error = None
            current.available_at = now() + SYNC_INTERVAL
            current.payload = {
                "success_at": now().isoformat(),
                "products_sha256": _digest(products),
                "basis_sha256": basis_sha,
                "routes_sha256": _digest(value),
                "generated_labels": generated_labels,
                "panel_labels": panel_labels,
            }
            if value != prior:
                session.add(
                    Audit(
                        actor_id=None,
                        action="panel_event_routes_auto_sync",
                        target="panel_event_routes",
                        reason="Автоматическая сверка доступных узлов разрешённых групп панели.",
                        details={"before_sha256": _digest(prior), "after_sha256": _digest(value)},
                    )
                )
        return {"status": "ok", "changed": value != prior}
    except (DomainError, PanelReview, PanelUnavailable, ValidationError, ValueError, OperationalError) as failure:
        if isinstance(failure, OperationalError) and getattr(failure.orig, "sqlstate", None) not in {
            "55P03", "40P01"
        }:
            raise
        error = "panel_unavailable" if isinstance(failure, (PanelUnavailable, OperationalError)) else "panel_route_sync_unverified"
        with db.transaction() as session:
            job = session.scalar(select(Job).where(Job.key == job_key).with_for_update())
            if job and job.state == "running" and job.payload.get("token") == token:
                job.attempts += 1
                job.state = "pending"
                job.last_error = error
                job.available_at = now() + timedelta(seconds=min(3600, 30 * 2 ** min(job.attempts, 6)))
        return {"status": "error", "error": error}


def _parse_time(value):
    from datetime import datetime

    return datetime.fromisoformat(value)
