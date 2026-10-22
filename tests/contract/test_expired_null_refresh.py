"""Реальный adopt на синтетических импортированных правах без БД и сети."""

from datetime import timedelta

import pytest
from sunrise_core.errors import DomainError
from sunrise_infrastructure import panel_entitlements as entitlements
from sunrise_infrastructure.db import PanelObservation, Product
from sunrise_infrastructure.panel_v3 import production_binding_key

from tests.contract.test_production_imported_entitlements import imported_null as imported_null


@pytest.fixture
def refreshing(imported_null):
    _, session, sub, future, evidence, rows, _, clock = imported_null
    product = session.get(Product, sub.product_id)
    session.scalars = lambda query: [product] if query.column_descriptions[0].get("entity") is Product else []
    sub.expires_at = future.starts_at
    observed = [None]
    session.scalar = lambda query: (
        observed[0] if query.column_descriptions[0].get("entity") is PanelObservation else None
    )
    binding = rows[production_binding_key(sub.id)].value["binding"]
    snapshot = {
        "expires_at": future.starts_at.isoformat(),
        "devices_limit": None,
        "internal_squad_uuids": [product.internal_squad_uuid],
        "status": "ACTIVE",
    }

    def refresh():
        version, fact = entitlements.adopt(session, sub, snapshot, binding)
        observed[0] = PanelObservation(
            subscription_id=sub.id,
            kind="user",
            status=snapshot["status"],
            observed_at=clock[0],
            last_error=None,
            payload={
                **snapshot,
                **fact,
                "source": "remnawave",
                "binding": binding,
                "environment": sub.environment,
                "account_id": str(sub.account_id),
                "subscription_version": version,
            },
        )
        return entitlements.facts(session, sub)

    return session, sub, future, evidence, rows, clock, snapshot, refresh, observed


def test_expiry_adopt_preserves_matching_history_without_current_null_authority(refreshing):
    session, sub, future, evidence, rows, clock, snapshot, refresh, _ = refreshing
    clock[0] = future.starts_at + timedelta(seconds=1)
    snapshot["status"] = "EXPIRED"
    assert refresh()["renewal_pricing_state"] == "ready"
    assert refresh()["renewal_pricing_state"] == "ready"
    assert entitlements.imported_null_capacity(session, sub, current_only=False) == evidence
    assert entitlements.imported_null_capacity(session, sub) is None
    assert rows[entitlements.override_key(sub.id)].value["capacity_evidence"] == evidence
    entitlements.require_pricing(session, sub)


def test_finite_interlude_never_resurrects_null_evidence(refreshing):
    session, sub, _, _, rows, _, snapshot, refresh, _ = refreshing
    snapshot["devices_limit"] = 6
    refresh()
    snapshot["devices_limit"] = None
    result = refresh()
    assert sub.devices_limit == 6
    assert "capacity_evidence" not in rows[entitlements.override_key(sub.id)].value
    assert entitlements.imported_null_capacity(session, sub, current_only=False) is None
    assert result["renewal_pricing_state"] == "blocked"
    assert result["renewal_device_limit_required"] is False


@pytest.mark.parametrize("expired", [False, True])
def test_damaged_null_can_only_quote_new_finite_renewal_after_expiry(refreshing, expired):
    session, sub, future, _, rows, clock, snapshot, refresh, _ = refreshing
    marker = rows[entitlements.override_key(sub.id)]
    marker.value = {key: value for key, value in marker.value.items() if key != "capacity_evidence"}
    if expired:
        clock[0] = future.starts_at + timedelta(seconds=1)
        snapshot["status"] = "EXPIRED"
    result = refresh()
    assert result["renewal_pricing_state"] == "blocked"
    assert result["renewal_pricing_error"] == "panel_capacity_unpriced"
    assert result["renewal_device_limit_required"] is expired
    assert entitlements.imported_null_capacity(session, sub, current_only=False) is None
    with pytest.raises(DomainError) as failure:
        entitlements.require_pricing(session, sub)
    assert failure.value.code == "panel_capacity_unpriced"
    if expired:
        entitlements.require_pricing(session, sub, finite_renewal=True)
    else:
        with pytest.raises(DomainError):
            entitlements.require_pricing(session, sub, finite_renewal=True)


@pytest.mark.parametrize("changed", ["root", "owner", "binding", "future", "restricted"])
def test_expired_finite_renewal_requires_current_owned_unrestricted_import_without_paid_rights(
    refreshing, changed
):
    session, sub, future, _, rows, clock, snapshot, refresh, _ = refreshing
    clock[0] = future.starts_at + timedelta(seconds=1)
    snapshot["status"] = "EXPIRED"
    marker = rows[entitlements.override_key(sub.id)]
    marker.value = {key: value for key, value in marker.value.items() if key != "capacity_evidence"}
    if changed == "root":
        rows[production_binding_key(sub.id)].value["capacity_evidence"]["present"] = False
    elif changed == "owner":
        rows[production_binding_key(sub.id)].value["account_id"] = "999"
    elif changed == "binding":
        rows[production_binding_key(sub.id)].value["binding"] = {"user_id": "99"}
    elif changed == "restricted":
        snapshot["status"] = "DISABLED"
    elif changed == "future":
        product = session.get(Product, sub.product_id)
        future.starts_at = clock[0] + timedelta(days=1)
        session.scalars = lambda query: (
            [product]
            if query.column_descriptions[0].get("entity") is Product
            else [future]
            if query.column_descriptions[0].get("entity").__name__ == "Segment"
            else []
        )
    result = refresh()
    assert result["renewal_device_limit_required"] is False
    with pytest.raises(DomainError):
        entitlements.require_pricing(session, sub, finite_renewal=True)
