"""Verified current access facts are separate from immutable purchased periods."""

import hashlib
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

from sqlalchemy import select
from sunrise_core.errors import DomainError

from .db import Alias, Job, Order, PanelObservation, Product, Quote, Segment, Setting, now
from .panel import PanelReview, panel_expiry
from .panel_v3 import instant


def bound(sub):
    return sub.panel_uuid is not None or sub.panel_user_id is not None


def override_key(sid):
    key = "panel_entitlement_override:" + sid
    return (
        key
        if len(key) <= 80
        else "panel_entitlement_override:" + hashlib.sha256(sid.encode()).hexdigest()[:40]
    )


def window_key(sid):
    key = "panel_effective_windows:" + sid
    return (
        key if len(key) <= 80 else "panel_effective_windows:" + hashlib.sha256(sid.encode()).hexdigest()[:40]
    )


def restriction_key(sid):
    return "panel_remote_restriction:" + hashlib.sha256(sid.encode()).hexdigest()[:40]


def effective_end(session, sub, segment):
    row = session.get(Setting, window_key(sub.id))
    if not row:
        return segment.ends_at
    value = row.value if isinstance(row.value, dict) else {}
    entries = value.get("segments")
    if not isinstance(entries, dict):
        return segment.starts_at
    if segment.id not in entries:
        return segment.ends_at
    entry = entries[segment.id]
    # A stale/missing binding must never erase an already verified shortening and resurrect rights.
    if (
        value.get("subscription_id") != sub.id
        or value.get("environment") != sub.environment
        or not isinstance(entry, dict)
        or entry.get("origin") != "verified_panel_snapshot"
    ):
        return segment.starts_at
    try:
        return min(segment.ends_at, instant(entry["effective_ends_at"]))
    except (KeyError, ValueError, TypeError, PanelReview):
        return segment.starts_at


def remaining_segments(session, sub, moment=None):
    moment = moment or now()
    return [item for item in segments(session, sub) if effective_end(session, sub, item) > moment]


def record_window(session, sub, segment, expiry, binding):
    row = session.get(Setting, window_key(sub.id))
    entries = dict(row.value.get("segments", {})) if row else {}
    if expiry >= segment.ends_at and segment.id not in entries:
        return
    entries[segment.id] = {
        "effective_ends_at": min(expiry, segment.ends_at).isoformat(),
        "origin": "verified_panel_snapshot",
        "observed_at": now().isoformat(),
    }
    value = {
        "subscription_id": sub.id,
        "account_id": str(sub.account_id),
        "environment": sub.environment,
        "binding": binding,
        "version": sub.version + 1,
        "segments": entries,
    }
    if row:
        row.value = value
    else:
        session.add(Setting(key=window_key(sub.id), value=value))


def rebase_rotation(session, sub, previous, current, version, job_id):
    """Only called in the verified own rotation completion transaction; never creates access metadata."""
    if (
        sub.version < version
        or not isinstance(previous, dict)
        or not isinstance(current, dict)
        or {key: value for key, value in previous.items() if key != "short_uuid"}
        != {key: value for key, value in current.items() if key != "short_uuid"}
    ):
        return
    for key in (override_key(sub.id), window_key(sub.id), restriction_key(sub.id)):
        row = session.get(Setting, key)
        if not row:
            continue
        value = row.value
        if not isinstance(value, dict):
            continue
        if (
            value.get("binding") != previous
            or value.get("subscription_id") != sub.id
            or value.get("environment") != sub.environment
            or type(value.get("version")) is not int
            or not 1 <= value["version"] <= version
            or not owned_account(session, sub, value.get("account_id"))
        ):
            continue
        row.value = value | {"binding": current, "rotation_job_id": job_id, "rotation_version": version}
    record = observation(session, sub)
    value = record.payload if record and isinstance(record.payload, dict) else {}
    if (
        record
        and record.subscription_id == sub.id
        and not record.last_error
        and value.get("source") == "remnawave"
        and value.get("binding") == previous
        and value.get("environment") == sub.environment
        and owned_account(session, sub, value.get("account_id"))
        and type(value.get("subscription_version")) is int
        and 1 <= value["subscription_version"] <= version
        and isinstance(value.get("binding_rotations", []), list)
    ):
        record.payload = value | {
            "binding": current,
            "binding_rotations": value.get("binding_rotations", [])
            + [{"previous": previous, "current": current, "version": version, "job_id": job_id}],
        }


def segments(session, sub):
    return list(
        session.scalars(
            select(Segment)
            .where(Segment.subscription_id == sub.id, Segment.kind != "refunded")
            .order_by(Segment.starts_at)
        )
    )


def current_segment(session, sub, moment=None):
    moment = moment or now()
    return max(
        (item for item in remaining_segments(session, sub, moment) if item.starts_at <= moment),
        key=lambda item: (item.starts_at, item.id),
        default=None,
    )


def paid_horizon(session, sub, moment=None):
    moment = moment or now()
    # A manually shortened CURRENT period is effective; untouched FUTURE purchases remain contracted.
    return max((item.ends_at for item in segments(session, sub) if item.starts_at > moment), default=None)


def verified_service_horizon(session, sub, moment: datetime):
    """Проверенный фактический срок и отдельно оплаченные будущие периоды."""
    dispute = session.get(Setting, "dispute:" + sub.id)
    if (
        local_status(session, sub) not in {"active", "expired"}
        or (dispute and isinstance(dispute.value, dict) and dispute.value.get("enabled"))
        or remote_status(session, sub)
    ):
        return None
    if bound(sub):
        value = facts(session, sub)
        record = verified_observation(session, sub)
        if value["panel_sync_state"] != "fresh" or record is None:
            return None
        expiry = record.payload.get("expires_at")
        if not expiry:
            return None
        try:
            expiry = instant(expiry)
        except PanelReview:
            return None
    elif sub.environment == "local_test" and sub.expires_at is not None:
        paid = session.scalar(
            select(Order.id)
            .join(Segment, Segment.order_id == Order.id)
            .where(
                Segment.subscription_id == sub.id,
                Segment.kind != "refunded",
                Order.account_id == sub.account_id,
                Order.status == "paid",
                Order.fulfilled_at.is_not(None),
                Order.environment == sub.environment,
            )
            .limit(1)
        )
        if paid is None:
            return None
        expiry = sub.expires_at
    else:
        return None
    horizon = paid_horizon(session, sub, moment)
    return max(expiry, horizon) if horizon is not None else expiry


def historical_due_horizon(session, sub, moment: datetime):
    """Owned historical expiry schedules a scoped check; it authorizes no current rights/action."""
    dispute = session.get(Setting, "dispute:" + sub.id)
    record = verified_observation(session, sub)
    if (
        not bound(sub)
        or record is None
        or record.status not in {"ACTIVE", "EXPIRED"}
        or local_status(session, sub) not in {"active", "expired"}
        or remote_status(session, sub)
        or (dispute and isinstance(dispute.value, dict) and dispute.value.get("enabled"))
    ):
        return None
    try:
        expiry = instant(record.payload.get("expires_at"))
    except (PanelReview, TypeError, ValueError):
        return None
    future = paid_horizon(session, sub, moment)
    return max(expiry, future) if future is not None else expiry


def pending_target(session, sub):
    return (
        sub.status in {"provisioning_pending", "revocation_pending"}
        or session.scalar(
            select(Job.id)
            .where(
                Job.kind.in_(["provision", "credential_rotate"]),
                Job.payload["subscription_id"].as_string() == sub.id,
                Job.state.in_(["pending", "in_flight", "review", "blocked"]),
                (Job.kind == "credential_rotate") | (Job.payload["version"].as_integer() == sub.version),
            )
            .limit(1)
        )
        is not None
    )


def override(session, sub, moment=None):
    moment = moment or now()
    row = session.get(Setting, override_key(sub.id))
    binding = registered_binding(session, sub)
    if (
        not row
        or not isinstance(row.value, dict)
        or binding is None
        or row.value.get("binding") != binding
        or not owned_account(session, sub, row.value.get("account_id"))
        or row.value.get("subscription_id") != sub.id
        or row.value.get("environment") != sub.environment
        or type(row.value.get("version")) is not int
        or not 1 <= row.value["version"] <= sub.version
    ):
        return None
    active = current_segment(session, sub, moment)
    if active and active.id != row.value.get("segment_id"):
        return None
    # No active paid period: preserve a verified manual extension, unless a new paid boundary passed.
    if not active and any(
        item.starts_at > instant(row.value["adopted_at"]) and item.starts_at <= moment
        for item in segments(session, sub)
    ):
        return None
    return row.value


def applied_target(session, sub):
    jobs = list(
        session.scalars(
            select(Job).where(
                Job.kind == "provision",
                Job.state == "done",
                Job.last_error.is_(None),
                Job.account_id == sub.account_id,
                Job.payload["subscription_id"].as_string() == sub.id,
                Job.payload["version"].as_integer() <= sub.version,
            )
        )
    )
    jobs = [
        item
        for item in jobs
        if type(item.payload.get("version")) is int and item.payload.get("environment") == sub.environment
    ]
    return max(jobs, key=lambda item: item.payload["version"]).payload if jobs else None


def imported_baseline(session, sub, moment=None):
    """Root-proven current access remains separate from future finite paid periods."""
    if sub.environment != "live" or registered_binding(session, sub) is None:
        return None
    from .panel_v3 import production_binding_key

    record = session.get(Setting, production_binding_key(sub.id))
    marker = override(session, sub, moment)
    if not record or record.value.get("origin") != "imported_project_identity" or not marker:
        return None
    return marker


def imported_null_capacity(session, sub, moment=None, *, current_only=True):
    from .panel_v3 import explicit_null_evidence, production_binding_key

    moment = moment or now()
    marker = imported_baseline(session, sub, moment)
    if not marker or sub.devices_limit is not None or marker.get("devices_limit") is not None:
        return None
    record = session.get(Setting, production_binding_key(sub.id))
    evidence = record.value.get("capacity_evidence")
    if (
        not explicit_null_evidence(evidence)
        or marker.get("capacity_evidence") != evidence
        or current_only
        and instant(evidence["until"]) <= moment
    ):
        return None
    return evidence


def at_unapplied_boundary(session, sub):
    active = current_segment(session, sub)
    target = applied_target(session, sub)
    return bool(
        active
        and target
        and target.get("active_segment_id") not in {None, active.id}
        and override(session, sub) is None
    )


def mapped_product(session, squads):
    if not squads:
        return {"panel_product_state": "unknown", "panel_product_id": None}
    products = []
    matched_squads = set()
    for product in session.scalars(select(Product).where(Product.available.is_(True))):
        try:
            configured = str(UUID(product.internal_squad_uuid)) if product.internal_squad_uuid else None
        except (ValueError, TypeError, AttributeError):
            continue
        if configured in squads:
            products.append(product)
            matched_squads.add(configured)
    state = (
        "ambiguous"
        if len(products) > 1
        else "unknown"
        if matched_squads != set(squads) or not products
        else "matched"
    )
    return {"panel_product_state": state, "panel_product_id": products[0].id if state == "matched" else None}


def adopt(session, sub, snapshot, binding):
    """Caller holds account then subscription locks and rechecks the pre-HTTP version/proof."""
    moment = now()
    remember_restriction(session, sub, snapshot, binding)
    active = current_segment(session, sub, moment)
    marker = override(session, sub, moment)
    preserved_null = imported_null_capacity(session, sub, moment)
    target = applied_target(session, sub)
    expected_expiry = (marker or {}).get("expires_at") or (target or {}).get("expires_at")
    if expected_expiry is None:
        expected_expiry = active.ends_at.isoformat() if active else None
    expected_capacity = (marker or {}).get(
        "devices_limit", (target or {}).get("devices_limit", sub.devices_limit)
    )
    expiry, capacity = instant(snapshot["expires_at"]), snapshot["devices_limit"]
    product_fact = mapped_product(session, snapshot["internal_squad_uuids"])
    cached_product = session.get(Product, sub.product_id) if sub.product_id else None
    expected_squads = (
        marker["internal_squad_uuids"]
        if marker
        else (
            [(target or {}).get("internal_squad_uuid")]
            if (target or {}).get("internal_squad_uuid")
            else [cached_product.internal_squad_uuid]
            if cached_product and cached_product.internal_squad_uuid
            else []
        )
    )
    changed = expected_expiry is None or abs((expiry - instant(expected_expiry)).total_seconds()) > 0.001
    expected_status = (marker or {}).get("status", (target or {}).get("panel_status", "ACTIVE"))
    changed = changed or capacity != expected_capacity or snapshot["status"] != expected_status
    changed = changed or snapshot["internal_squad_uuids"] != sorted(expected_squads)
    changed = (
        changed
        or product_fact["panel_product_id"] is not None
        and product_fact["panel_product_id"] != sub.product_id
    )
    if changed:
        anchor = active or next(
            (item for item in reversed(segments(session, sub)) if item.starts_at <= moment), None
        )
        value = {
            "binding": binding,
            "segment_id": anchor.id if anchor else None,
            "expires_at": expiry.isoformat(),
            "devices_limit": capacity,
            "adopted_at": moment.isoformat(),
            "version": sub.version + 1,
            "status": snapshot["status"],
            "subscription_id": sub.id,
            "environment": sub.environment,
            "account_id": str(sub.account_id),
            "internal_squad_uuids": snapshot["internal_squad_uuids"],
            **product_fact,
            **({"capacity_evidence": preserved_null} if capacity is None and preserved_null else {}),
        }
        row = session.get(Setting, override_key(sub.id))
        if row:
            row.value = value
        else:
            session.add(Setting(key=override_key(sub.id), value=value))
        horizon = paid_horizon(session, sub, moment)
        sub.expires_at = max(expiry, horizon) if horizon else expiry
        if type(capacity) is int and 1 <= capacity <= 100:
            sub.devices_limit = capacity
        if product_fact["panel_product_id"] is not None:
            sub.product_id = product_fact["panel_product_id"]
        if anchor is not None:
            record_window(session, sub, anchor, expiry, binding)
        sub.version += 1
    return sub.version, product_fact


def observation(session, sub):
    return session.scalar(
        select(PanelObservation)
        .where(PanelObservation.kind == "user", PanelObservation.subscription_id == sub.id)
        .order_by(PanelObservation.observed_at.desc())
        .limit(1)
    )


def owned_account(session, sub, owner):
    if owner == str(sub.account_id):
        return True
    if (
        not isinstance(owner, str)
        or not owner.isascii()
        or not owner.isdecimal()
        or not 1 <= len(owner) <= 19
        or owner.startswith("0")
        or int(owner) > 9223372036854775807
    ):
        return False
    alias = session.get(Alias, int(owner))
    return bool(alias and alias.canonical_id == sub.account_id)


def registered_binding(session, sub):
    if sub.environment == "live":
        from .panel_v3 import production_binding_key

        proof = session.get(Setting, production_binding_key(sub.id))
        marker = session.get(Setting, "production_admission")
        value = proof.value if proof and isinstance(proof.value, dict) else {}
        binding = value.get("binding")
        if (
            not marker
            or not isinstance(marker.value, dict)
            or value.get("scope_id") != marker.value.get("scope_id")
            or value.get("origin") not in {"new_project_identity", "imported_project_identity"}
            or value.get("subscription_id") != sub.id
            or value.get("environment") != "live"
            or value.get("contract") != "3.4_id"
            or not owned_account(session, sub, value.get("account_id"))
            or not isinstance(binding, dict)
            or binding.get("scope_id") != value.get("scope_id")
            or type(value.get("generation")) is not int
            or value["generation"] <= 0
            or binding.get("generation") != value["generation"]
            or sub.panel_user_id is None
            or binding.get("user_id") != str(sub.panel_user_id)
            or binding.get("uuid") != sub.panel_uuid
            or binding.get("short_uuid") != sub.short_uuid
            or value.get("connection_url") != sub.connection_url
        ):
            return None
        return binding
    proof = session.get(Setting, "panel_test:" + sub.id)
    value = proof.value if proof and isinstance(proof.value, dict) else {}
    binding = value.get("binding")
    if sub.environment == "scoped_live":
        marker = session.get(Setting, "scoped_test_admission")
        intent = value.get("creation_intent", {})
        from .db import Order

        order = session.get(Order, intent.get("order_id")) if intent.get("order_id") else None
        account_marker = session.get(Setting, "scoped_test_account:" + str(sub.account_id))
        if (
            not marker
            or marker.value.get("synthetic_only") is not True
            or not account_marker
            or account_marker.value.get("scope_id") != marker.value.get("scope_id")
            or account_marker.value.get("account_id") != str(sub.account_id)
            or value.get("origin") != "new_test_identity"
            or value.get("contract") != "3.4_id"
            or intent.get("environment") != "scoped_live"
            or intent.get("subscription_id") != sub.id
            or not owned_account(session, sub, intent.get("owner_id"))
            or not isinstance(binding, dict)
            or intent.get("origin_nonce") != binding.get("origin_nonce")
            or not order
            or order.environment != "scoped_live"
            or order.account_id != sub.account_id
            or order.subscription_id != sub.id
            or order.status != "paid"
            or order.fulfilled_at is None
            or value.get("connection_url") != sub.connection_url
        ):
            return None
    if (
        sub.environment not in {"local_test", "sandbox", "scoped_live"}
        or value.get("subscription_id") != sub.id
        or value.get("environment") != sub.environment
        or not owned_account(session, sub, value.get("account_id"))
        or not isinstance(binding, dict)
        or binding.get("short_uuid") != sub.short_uuid
        or (
            sub.panel_uuid is not None
            and (sub.panel_user_id is not None or binding.get("uuid") != sub.panel_uuid)
        )
        or (
            sub.panel_uuid is None
            and (sub.panel_user_id is None or binding.get("user_id") != str(sub.panel_user_id))
        )
    ):
        return None
    return binding


def verified_observation(session, sub):
    record = observation(session, sub)
    binding = registered_binding(session, sub)
    if (
        not record
        or not binding
        or record.last_error
        or record.subscription_id != sub.id
        or not isinstance(record.payload, dict)
        or record.payload.get("source") != "remnawave"
        or record.payload.get("binding") != binding
        or record.payload.get("environment") != sub.environment
        or not owned_account(session, sub, record.payload.get("account_id"))
        or type(record.payload.get("subscription_version")) is not int
        or not 1 <= record.payload["subscription_version"] <= sub.version
    ):
        return None
    return record


def successful_job(session, sub):
    jobs = [
        job
        for job in session.scalars(
            select(Job).where(
                Job.kind == "provision",
                Job.state == "done",
                Job.last_error.is_(None),
                Job.payload["subscription_id"].as_string() == sub.id,
            )
        )
        if isinstance(job.payload, dict)
        and type(job.payload.get("version")) is int
        and 1 <= job.payload["version"] <= sub.version
    ]
    return max(jobs, key=lambda job: (job.payload["version"], job.id), default=None)


def local_origin_target(session, sub, mock_only=False, observed=None):
    jobs = [
        job
        for job in session.scalars(
            select(Job).where(Job.kind == "provision", Job.payload["subscription_id"].as_string() == sub.id)
        )
        if isinstance(job.payload, dict)
        and type(job.payload.get("version")) is int
        and 1 <= job.payload["version"] <= sub.version
    ]
    job = max(jobs, key=lambda item: (item.payload["version"], item.id), default=None)
    binding = registered_binding(session, sub) if bound(sub) else None
    if (
        not job
        or job.last_error
        or job.account_id != sub.account_id
        or job.payload.get("environment") != sub.environment
    ):
        return None
    if job.state in {"pending", "in_flight"}:
        if job.payload["version"] != sub.version - 1:
            return None
    elif job.state != "done" or not (
        mock_only and sub.environment == "local_test" or observed == job.payload
    ):
        return None
    if bound(sub):
        if binding is None or (
            observed != job.payload
            and (job.payload.get("panel_binding") or job.payload.get("origin_binding")) != binding
        ):
            return None
    elif not mock_only or job.payload.get("panel_binding") or job.payload.get("origin_binding"):
        return None
    return job.payload


def own_snapshot_target(session, sub, snapshot, binding, rotations=()):
    if binding is None or binding != registered_binding(session, sub) or not isinstance(snapshot, dict):
        return None
    job = successful_job(session, sub)
    if not job or job.account_id != sub.account_id or job.payload.get("environment") != sub.environment:
        return None
    target = job.payload
    expected_binding = target.get("panel_binding")
    if expected_binding != binding:
        if not isinstance(rotations, (list, tuple)):
            return None
        start = next(
            (
                index
                for index, link in enumerate(rotations)
                if isinstance(link, dict)
                and link.get("previous") == expected_binding
                and type(link.get("version")) is int
                and link["version"] >= target["version"]
            ),
            None,
        )
        if start is None:
            return None
        for link in rotations[start:]:
            if not isinstance(link, dict) or link.get("previous") != expected_binding:
                return None
            rotated = session.get(Job, link.get("job_id"))
            if (
                not rotated
                or not isinstance(rotated.payload, dict)
                or rotated.kind != "credential_rotate"
                or rotated.state != "done"
                or rotated.last_error
                or rotated.account_id != sub.account_id
                or rotated.payload.get("subscription_id") != sub.id
                or rotated.payload.get("environment") != sub.environment
                or rotated.payload.get("version") != link.get("version")
                or type(link.get("version")) is not int
                or not target["version"] <= link["version"] <= sub.version
                or rotated.payload.get("panel_binding") != link.get("current")
                or not isinstance(link.get("current"), dict)
                or not isinstance(expected_binding, dict)
                or {k: v for k, v in expected_binding.items() if k != "short_uuid"}
                != {k: v for k, v in link["current"].items() if k != "short_uuid"}
            ):
                return None
            expected_binding = link["current"]
    try:
        matches = (
            expected_binding == binding
            and snapshot.get("status") in {"ACTIVE", "DISABLED", "LIMITED", "EXPIRED"}
            and snapshot.get("status") == target.get("panel_status", "ACTIVE")
            and snapshot.get("devices_limit") == target.get("devices_limit")
            and (
                type(snapshot.get("devices_limit")) is int
                and 1 <= snapshot["devices_limit"] <= 100
                or "devices_limit" in snapshot
                and snapshot["devices_limit"] is None
                and target.get("preserve_null_capacity") is True
                and target.get("capacity_evidence") is not None
                and target["capacity_evidence"] == imported_null_capacity(session, sub)
            )
            and snapshot.get("internal_squad_uuids") == [target.get("internal_squad_uuid")]
            and abs(
                (
                    instant(snapshot["expires_at"]) - instant(panel_expiry(target["expires_at"]))
                ).total_seconds()
            )
            <= 0.001
        )
    except (KeyError, TypeError, ValueError, PanelReview):
        return None
    return target if matches else None


def observed_target(session, sub):
    record = verified_observation(session, sub)
    return (
        own_snapshot_target(
            session,
            sub,
            record.payload | {"status": record.status},
            record.payload["binding"],
            record.payload.get("binding_rotations", []),
        )
        if record
        else None
    )


def remember_restriction(session, sub, snapshot, binding):
    row = session.get(Setting, restriction_key(sub.id))
    if snapshot["status"] in {"ACTIVE", "EXPIRED"}:
        if row:
            session.delete(row)
        return
    prior = observation(session, sub)
    rotations = (
        prior.payload.get("binding_rotations", []) if prior and isinstance(prior.payload, dict) else []
    )
    if snapshot["status"] not in {"DISABLED", "LIMITED"} or own_snapshot_target(
        session, sub, snapshot, binding, rotations
    ):
        return
    value = {
        "subscription_id": sub.id,
        "account_id": str(sub.account_id),
        "environment": sub.environment,
        "binding": binding,
        "version": sub.version,
        "status": snapshot["status"],
    }
    if row:
        row.value = value
    else:
        session.add(Setting(key=restriction_key(sub.id), value=value))


def remote_status(session, sub):
    row = session.get(Setting, restriction_key(sub.id))
    value = row.value if row and isinstance(row.value, dict) else {}
    binding = registered_binding(session, sub)
    if (
        binding is not None
        and value.get("subscription_id") == sub.id
        and value.get("environment") == sub.environment
        and owned_account(session, sub, value.get("account_id"))
        and value.get("binding") == binding
        and type(value.get("version")) is int
        and 1 <= value["version"] <= sub.version
        and value.get("status") in {"DISABLED", "LIMITED"}
    ):
        return value["status"]
    record = verified_observation(session, sub)
    return (
        record.status
        if record and record.status in {"DISABLED", "LIMITED"} and not observed_target(session, sub)
        else None
    )


def local_status(session, sub):
    restriction = session.get(Setting, "restriction:" + sub.id)
    dispute = session.get(Setting, "dispute:" + sub.id)
    if dispute and dispute.value.get("enabled"):
        return "disputed"
    if restriction and restriction.value.get("suspended"):
        return "suspended"
    if sub.status == "suspension_pending":
        return "suspended"
    if sub.status == "revocation_pending":
        return "revoked"
    if sub.status != "provisioning_pending":
        return sub.status
    binding = registered_binding(session, sub) if bound(sub) else None
    jobs = list(
        session.scalars(
            select(Job).where(
                Job.kind == "provision",
                Job.account_id == sub.account_id,
                Job.payload["subscription_id"].as_string() == sub.id,
                Job.payload["version"].as_integer() == sub.version,
                Job.state.in_(["pending", "in_flight"]),
            )
        )
    )
    known = [
        job.payload.get("desired_subscription_status")
        for job in jobs
        if job.payload.get("environment") == sub.environment
        and job.payload.get("target_origin")
        in {"paid", "paid_boundary", "gift", "owner", "refund", "refund_review"}
        and (job.payload.get("panel_binding") or job.payload.get("origin_binding")) == binding
        and (not bound(sub) or binding is not None)
    ]
    return known[0] if known and all(status == known[0] for status in known) else "suspended"


def facts(session, sub, result=None):
    record = observation(session, sub)
    payload = record.payload if record and isinstance(record.payload, dict) else {}
    state = "unknown"
    if record:
        state = (
            "fresh"
            if (
                verified_observation(session, sub) is not None
                and record.observed_at > now() - timedelta(minutes=10)
                and payload.get("subscription_version") == sub.version
            )
            else "stale"
        )
    if bound(sub) and pending_target(session, sub):
        state = "pending"
    if result and result.get("status") in {"pending", "denied", "busy", "error"}:
        state = {"pending": "pending", "denied": "denied", "busy": "stale", "error": "stale"}[
            result["status"]
        ]
    capacity = payload.get("devices_limit")
    product_fact = mapped_product(session, payload.get("internal_squad_uuids", []))
    product = (
        session.get(Product, product_fact["panel_product_id"]) if product_fact["panel_product_id"] else None
    )
    product_verified = (
        product
        and product.id == payload.get("panel_product_id") == sub.product_id
        and product_fact["panel_product_state"] == "matched"
    )
    priced = (type(capacity) is int and 1 <= capacity <= 100) or (
        "devices_limit" in payload
        and capacity is None
        and imported_null_capacity(session, sub, current_only=False) is not None
    )
    priced = priced and bool(product_verified and product.available)
    if remote_status(session, sub):
        priced = False
    return {
        "expires_at": payload.get("expires_at") or (sub.expires_at.isoformat() if sub.expires_at else None),
        "paid_horizon": (horizon.isoformat() if (horizon := paid_horizon(session, sub)) else None),
        "panel_sync_state": state,
        "panel_sync_error": (result or {}).get("error") or (record.last_error if record else None),
        "panel_device_limit": capacity,
        **product_fact,
        "renewal_pricing_state": "ready" if (not bound(sub) or state == "fresh" and priced) else "blocked",
    }


def require_pricing(session, sub):
    if not bound(sub):
        return
    value = facts(session, sub)
    if value["panel_sync_state"] != "fresh":
        raise DomainError(
            "panel_sync_required",
            "Не удалось проверить данные панели. Обновите профиль перед продлением.",
            409,
        )
    record = observation(session, sub)
    if remote_status(session, sub):
        raise DomainError(
            "panel_access_restricted",
            "Панель ограничила доступ. Перед изменением подписки обратитесь к владельцу.",
            409,
        )
    product = session.get(Product, value["panel_product_id"]) if value["panel_product_id"] else None
    if (
        value["panel_product_state"] != "matched"
        or not product
        or not product.available
        or product.id != sub.product_id
        or product.id != record.payload.get("panel_product_id")
    ):
        raise DomainError(
            "panel_product_unverified",
            "Тариф панели не сопоставлен с действующим каталогом однозначно. Владелец должен проверить группы и условия тарифа.",
            409,
        )
    if value["renewal_pricing_state"] != "ready":
        raise DomainError(
            "panel_capacity_unpriced",
            "Лимит панели не позволяет рассчитать цену. Владелец должен задать конечное число устройств и условия тарифа.",
            409,
        )


def valuation_segments(session, sub, moment=None):
    """Project verified panel access onto saved paid prices without rewriting history."""
    require_pricing(session, sub)
    moment = moment or now()
    remaining = remaining_segments(session, sub, moment)
    marker = override(session, sub)
    if not marker:
        if any(effective_end(session, sub, item) != item.ends_at for item in remaining):
            raise DomainError("valuation_unknown", "Не найдена проверенная стоимость остатка периода.", 409)
        return remaining
    source = session.get(Segment, marker.get("segment_id")) if marker.get("segment_id") else None
    if not source or source.subscription_id != sub.id or source.kind != "paid" or source.starts_at > moment:
        raise DomainError("valuation_unknown", "Не найдены подтверждённые оплаченные условия.", 409)
    basis = deepcopy(source.price_basis)
    if (
        not isinstance(basis, dict)
        or not isinstance(basis.get("products"), dict)
        or not basis.get("currency")
        or type(basis.get("period_days")) is not int
        or basis["period_days"] <= 0
    ):
        raise DomainError("valuation_unknown", "Не найдены сопоставимые оплаченные условия.", 409)
    expiry = instant(marker["expires_at"])
    if expiry <= moment:
        raise DomainError("no_paid_segment", "Нет действующего оплаченного периода.", 409)
    future = [item for item in remaining if item.starts_at > moment and item.id != source.id]
    if future and expiry > future[0].starts_at:
        # Do not value the same time twice or silently shorten separately purchased access.
        raise DomainError(
            "valuation_unknown",
            "Изменение панели пересекает будущий оплаченный период. Нужна проверка владельцем.",
            409,
        )
    original = deepcopy(basis)
    if marker["devices_limit"] != source.devices:
        from sunrise_core.devices import device_surcharge

        try:
            if basis.get("device_adjustments"):
                # Fixed remaining-term supplements have distinct time-normalized rates;
                # a monthly device delta cannot recover their original per-slot value.
                raise ValueError()
            months = Decimal(str(basis["months"]))
            factor = basis.get("device_price_factor")
            if factor is None:
                # Historical tier changes retain the purchase order_id but may have
                # rescaled this basis. Its original invoice cannot prove the new rate.
                if basis.get("segment_id") is not None:
                    raise ValueError()
                order = session.get(Order, source.order_id) if source.order_id else None
                quote = session.get(Quote, order.quote_id) if order else None
                paid = quote.snapshot if quote else {}
                if (
                    paid.get("action") not in {"purchase", "renew"}
                    or paid.get("legacy_promo_grant")
                    or paid.get("minimum_external_required")
                    or (paid.get("selected_secondary") or {}).get("reward_type") == "fixed_rub"
                ):
                    raise ValueError()
                # Ordinary period/loyalty discounts exclude devices. A payment-method
                # discount rescales their saved full-period basis.
                amount = Decimal(paid["base_amount"]) - Decimal(paid["discount_amount"])
                before_method = amount + Decimal(paid.get("payment_method_discount_amount", "0"))
                factor = amount / before_method
            factor = Decimal(str(factor))
            if not months.is_finite() or months <= 0 or not factor.is_finite() or not 0 < factor <= 1:
                raise ValueError()
        except (ValueError, TypeError, KeyError, ArithmeticError):
            raise DomainError(
                "valuation_unknown", "Не найдены точные оплаченные условия устройств.", 409
            ) from None
        basis["device_price_factor"] = str(factor)
        prices = {}
        for product_id, value in basis["products"].items():
            terms = basis.get("device_terms", {}).get(product_id)
            try:
                if not terms or type(terms.get("included")) is not int:
                    raise ValueError()
                if source.devices > terms["included"] and not basis.get("device_pricing_included"):
                    raise ValueError()
                previous = device_surcharge(
                    max(0, source.devices - terms["included"]), terms.get("rules"), terms.get("price")
                )
                current = device_surcharge(
                    max(0, marker["devices_limit"] - terms["included"]),
                    terms.get("rules"),
                    terms.get("price"),
                )
                amount = Decimal(value) + (current - previous) * months * factor
                if amount.is_finite() and amount > 0:
                    prices[product_id] = str(amount)
            except (ValueError, TypeError, KeyError, ArithmeticError):
                continue
        basis["products"] = prices
        basis["device_pricing_included"] = True
    if marker.get("panel_product_id") not in basis["products"]:
        raise DomainError("valuation_unknown", "Не найдена оплаченная ценовая база текущего тарифа.", 409)
    basis["panel_revaluation"] = {
        "source_segment_id": source.id,
        "source_order_id": source.order_id,
        "source_ends_at": source.ends_at.isoformat(),
        "source_devices": source.devices,
        "source_product_id": source.product_id,
        "source_basis": {key: value for key, value in original.items() if key != "panel_revaluation"},
        "expires_at": expiry.isoformat(),
        "devices_limit": marker["devices_limit"],
        "product_id": marker["panel_product_id"],
        "subscription_version": sub.version,
    }
    return [
        SimpleNamespace(
            id=source.id,
            order_id=source.order_id,
            kind=source.kind,
            starts_at=source.starts_at,
            ends_at=expiry,
            devices=marker["devices_limit"],
            product_id=marker["panel_product_id"],
            price_basis=basis,
        ),
        *future,
    ]


def require_valuation(session, sub):
    if bound(sub):
        valuation_segments(session, sub)


def accept_valuation(session, sub, basis):
    """A newly accepted paid change supersedes only its reviewed manual access projection."""
    if not basis.get("panel_revaluation"):
        return
    marker = session.get(Setting, override_key(sub.id))
    if marker:
        session.delete(marker)
    window = session.get(Setting, window_key(sub.id))
    if window:
        entries = dict(window.value.get("segments", {}))
        entries.pop(basis["segment_id"], None)
        if entries:
            window.value = window.value | {"segments": entries}
        else:
            session.delete(window)
