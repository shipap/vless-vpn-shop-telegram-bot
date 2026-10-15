from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sunrise_infrastructure import commerce, panel_entitlements
from sunrise_infrastructure.db import Job, Product, Segment, Setting, Subscription, now
from sunrise_infrastructure.panel_v3 import production_binding_key


@pytest.fixture
def imported_null(monkeypatch):
    moment = now()
    boundary = moment + timedelta(days=2)
    sub = Subscription(
        id="synthetic-import",
        account_id=9007199254740993,
        product_id="synthetic",
        environment="live",
        status="active",
        devices_limit=None,
        expires_at=boundary + timedelta(days=30),
        version=2,
        panel_uuid="22222222-2222-4222-8222-222222222222",
        panel_user_id=8,
        short_uuid="syntheticshortuuid456",
        connection_url="https://sub.synthetic.test/syntheticshortuuid456",
    )
    squad = "11111111-1111-4111-8111-111111111111"
    product = Product(id="synthetic", available=True, internal_squad_uuid=squad, included_devices=4)
    binding = {
        "scope_id": "synthetic-project",
        "generation": 1,
        "origin_nonce": "b" * 32,
        "uuid": sub.panel_uuid,
        "user_id": "8",
        "short_uuid": sub.short_uuid,
        "username": "preserved_customer",
    }
    evidence = {
        "kind": "explicit_null_preserved",
        "present": True,
        "source": "authenticated_panel_snapshot",
        "until": boundary.isoformat(),
    }
    rows = {
        "production_admission": Setting(key="production_admission", value={"scope_id": "synthetic-project"}),
        production_binding_key(sub.id): Setting(
            key=production_binding_key(sub.id),
            value={
                "scope_id": "synthetic-project",
                "generation": 1,
                "origin": "imported_project_identity",
                "contract": "3.4_id",
                "subscription_id": sub.id,
                "account_id": str(sub.account_id),
                "environment": "live",
                "binding": binding,
                "connection_url": sub.connection_url,
                "capacity_evidence": evidence,
            },
        ),
        panel_entitlements.override_key(sub.id): Setting(
            key=panel_entitlements.override_key(sub.id),
            value={
                "subscription_id": sub.id,
                "account_id": str(sub.account_id),
                "environment": "live",
                "binding": binding,
                "version": 1,
                "segment_id": None,
                "adopted_at": moment.isoformat(),
                "expires_at": boundary.isoformat(),
                "devices_limit": None,
                "status": "ACTIVE",
                "internal_squad_uuids": [squad],
                "panel_product_state": "matched",
                "panel_product_id": product.id,
                "capacity_evidence": evidence,
            },
        ),
    }
    segment = Segment(
        id="future-paid",
        subscription_id=sub.id,
        order_id="genuine-paid",
        product_id=product.id,
        kind="paid",
        starts_at=boundary,
        ends_at=sub.expires_at,
        devices=4,
        paid_value=Decimal(1000),
        price_basis={"panel_mappings": {product.id: squad}},
    )
    jobs = []
    clock = [moment]

    def scalars(query):
        entity = query.column_descriptions[0].get("entity")
        if entity is Segment:
            return [segment]
        if entity is Product:
            return [product]
        if entity is Job:
            return (
                [job.payload for job in jobs]
                if query.column_descriptions[0].get("name") == "payload"
                else jobs
            )
        return []

    session = SimpleNamespace(
        get=lambda model, key, **_: (
            rows.get(key) if model is Setting else product if model is Product else None
        ),
        scalar=lambda _: None,
        scalars=scalars,
        add=jobs.append,
        delete=lambda row: rows.pop(row.key),
        execute=lambda _: SimpleNamespace(all=lambda: [(sub, segment)]),
    )

    @contextmanager
    def transaction():
        yield session

    service = commerce.Commerce(SimpleNamespace(transaction=transaction), local_test=True)
    service.runtime_policy = SimpleNamespace(
        production=True,
        scope=SimpleNamespace(scope_id="synthetic-project"),
        require_current=lambda *args: None,
    )
    service._lock_accounts_in = lambda session, ids: {identifier: identifier for identifier in ids}
    monkeypatch.setattr(commerce, "now", lambda: clock[0])
    monkeypatch.setattr(panel_entitlements, "now", lambda: clock[0])
    return service, session, sub, segment, evidence, rows, jobs, clock


def test_common_renewal_payload_preserves_null_then_finite_boundary_is_queued_once(imported_null):
    service, session, sub, segment, evidence, rows, jobs, clock = imported_null
    payload = service._provision_payload(session, sub, origin="paid")
    assert payload["devices_limit"] is None and payload["preserve_null_capacity"] is True
    assert payload["capacity_evidence"] == evidence
    assert payload["expires_at"] == segment.ends_at.isoformat()
    assert payload["panel_status"] == "ACTIVE"
    assert sub.devices_limit is None and segment.devices == 4 and segment.paid_value == Decimal(1000)
    clock[0] = segment.starts_at
    service.apply_segment_boundaries()
    assert sub.devices_limit == 4
    assert len(jobs) == 1 and jobs[0].kind == "provision"
    assert jobs[0].payload["devices_limit"] == 4
    assert "preserve_null_capacity" not in jobs[0].payload
    assert jobs[0].payload["expires_at"] == segment.ends_at.isoformat()
    service.apply_segment_boundaries()
    assert len(jobs) == 1


@pytest.mark.parametrize("delta", [{"present": False}, {"source": "unverified"}, {"kind": "unlimited"}])
def test_unproven_null_capacity_never_authorizes_preservation(imported_null, delta):
    _, session, sub, _, _, rows, _, _ = imported_null
    row = rows[production_binding_key(sub.id)]
    row.value = row.value | {"capacity_evidence": row.value["capacity_evidence"] | delta}
    assert panel_entitlements.imported_null_capacity(session, sub) is None


def test_expired_imported_null_proof_allows_renewal_pricing_without_current_null_preservation(imported_null):
    from sunrise_infrastructure.db import PanelObservation

    _, session, sub, segment, _, rows, _, clock = imported_null
    clock[0] = segment.starts_at + timedelta(seconds=1)
    # No new paid period exists yet for this expired imported subscription.
    product = session.get(Product, sub.product_id)
    record = PanelObservation(
        subscription_id=sub.id,
        kind="user",
        status="EXPIRED",
        observed_at=clock[0],
        last_error=None,
        payload={
            "source": "remnawave",
            "binding": rows[production_binding_key(sub.id)].value["binding"],
            "environment": "live",
            "account_id": str(sub.account_id),
            "subscription_version": sub.version,
            "panel_product_id": product.id,
            "internal_squad_uuids": [product.internal_squad_uuid],
            "devices_limit": None,
            "expires_at": segment.starts_at.isoformat(),
        },
    )
    session.scalars = lambda query: [product] if query.column_descriptions[0].get("entity") is Product else []
    session.scalar = lambda query: (
        record if query.column_descriptions[0].get("entity") is PanelObservation else None
    )
    assert panel_entitlements.imported_null_capacity(session, sub) is None
    panel_entitlements.require_pricing(session, sub)
    assert panel_entitlements.facts(session, sub)["renewal_pricing_state"] == "ready"


@pytest.mark.parametrize("allowed", [True, False])
def test_paid_boundary_acquires_canonical_accounts_before_subscriptions_and_off_preserves_rights(
    imported_null, allowed
):
    from sunrise_core.errors import DomainError

    service, session, sub, segment, _, _, jobs, clock = imported_null
    clock[0] = segment.starts_at
    locks = []

    def require_current(*args):
        if not allowed:
            raise DomainError("production_stage_denied", "Операция недоступна.", 503)

    def execute(query):
        if query._for_update_arg is not None:
            locks.append("Subscription")
        return SimpleNamespace(all=lambda: [(sub, segment)])

    def lock_accounts(tx, ids):
        locks.append("Account")
        assert ids == {sub.account_id}
        return {sub.account_id: sub.account_id}

    session.execute = execute
    service._lock_accounts_in = lock_accounts
    service.runtime_policy.require_current = require_current
    service.apply_segment_boundaries()
    if allowed:
        assert locks == ["Account", "Subscription"] and len(jobs) == 1
    else:
        assert locks == [] and jobs == [] and sub.devices_limit is None and sub.version == 2
