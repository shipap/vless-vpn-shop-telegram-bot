"""Каталожный базовый лимит для подтверждённой старой подписки с NULL в панели."""

from copy import deepcopy
from decimal import Decimal

import pytest
from sunrise_core.errors import DomainError
from sunrise_infrastructure import panel_entitlements as entitlements
from sunrise_infrastructure.db import PanelObservation, Product, Segment, Setting
from sunrise_infrastructure.device_pricing import fixed_remaining_terms
from sunrise_infrastructure.legacy_catalog_valuation import (
    accepted_basis,
    catalog_projection,
    require_accepted_import,
)
from sunrise_infrastructure.panel_v3 import production_binding_key

from tests.contract.test_production_imported_entitlements import imported_null as imported_null


@pytest.fixture
def null_catalog(imported_null):
    _, session, sub, future, evidence, rows, _, clock = imported_null
    product = session.get(Product, sub.product_id)
    product.code = "synthetic"
    product.version = 1
    product.tier_level = 1
    product.price = Decimal("100")
    product.currency = "RUB"
    product.extra_device_price = Decimal("50")
    product.presentation = {"base_period_days": 30}
    rows["legacy_null_device_limit"] = Setting(
        key="legacy_null_device_limit", value={"mode": "catalog_included"}
    )
    sub.expires_at = future.starts_at
    record = PanelObservation(
        subscription_id=sub.id,
        kind="user",
        status="ACTIVE",
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
            "expires_at": sub.expires_at.isoformat(),
        },
    )
    session.scalars = lambda query: [product] if query.column_descriptions[0].get("entity") is Product else []
    session.scalar = lambda query: (
        record if query.column_descriptions[0].get("entity") is PanelObservation else None
    )
    return session, sub, product, evidence, rows, clock


@pytest.mark.parametrize("included,unit", [(4, "50"), (6, "17")])
def test_imported_null_uses_configured_catalog_baseline_without_rewriting_access(
    null_catalog, included, unit
):
    session, sub, product, _, rows, clock = null_catalog
    product.included_devices, product.extra_device_price = included, Decimal(unit)
    original = deepcopy(rows[entitlements.override_key(sub.id)].value)
    segments = entitlements.valuation_segments(session, sub, clock[0])
    assert len(segments) == 1
    active = segments[0]
    assert active.devices == included
    assert active.ends_at == sub.expires_at
    assert sub.devices_limit is None
    assert rows[entitlements.override_key(sub.id)].value == original
    charge, adjustments = fixed_remaining_terms(segments, clock[0], product, 1)
    assert charge == Decimal(unit)
    assert adjustments[0]["devices"] == included + 1
    assert active.order_id is None and active.kind == "legacy_catalog"
    assert active.price_basis["legacy_catalog"]["capacity_basis"] == {
        "origin": "catalog_included_for_imported_null",
        "panel_devices_limit": None,
        "included_devices": included,
    }


def test_null_without_matching_explicit_capacity_proof_cannot_use_catalog(null_catalog):
    session, sub, _, _, rows, clock = null_catalog
    root = rows[production_binding_key(sub.id)]
    root.value = root.value | {"capacity_evidence": root.value["capacity_evidence"] | {"present": False}}
    assert catalog_projection(session, sub, clock[0]) is None


@pytest.mark.parametrize(
    "policy", [None, {}, {"mode": "unlimited"}, {"mode": "catalog_included", "extra": True}]
)
def test_null_catalog_baseline_requires_explicit_instance_policy(null_catalog, policy):
    session, sub, _, _, rows, clock = null_catalog
    if policy is None:
        rows.pop("legacy_null_device_limit")
    else:
        rows["legacy_null_device_limit"].value = policy
    assert catalog_projection(session, sub, clock[0]) is None


def test_accepted_null_quote_rejects_policy_revocation(null_catalog):
    session, sub, _, _, rows, clock = null_catalog
    active = catalog_projection(session, sub, clock[0])
    assert active is not None
    rows.pop("legacy_null_device_limit")
    with pytest.raises(DomainError) as denied:
        require_accepted_import(session, sub, active.price_basis | {"segment_id": active.id})
    assert denied.value.code == "valuation_unknown"


@pytest.mark.parametrize("included", [None, True, 0, 101])
def test_invalid_catalog_baseline_cannot_create_device_value(null_catalog, included):
    session, sub, product, _, _, clock = null_catalog
    product.included_devices = included
    assert catalog_projection(session, sub, clock[0]) is None


def test_any_started_financial_or_gift_history_still_blocks_null_bootstrap(null_catalog):
    session, sub, _, _, _, clock = null_catalog
    session.scalar = lambda query: (
        "existing-segment" if query.column_descriptions[0].get("entity") is Segment else None
    )
    assert catalog_projection(session, sub, clock[0]) is None


def test_quote_null_provenance_rejects_changed_finite_access_before_acceptance(null_catalog):
    session, sub, _, _, rows, clock = null_catalog
    active = catalog_projection(session, sub, clock[0])
    assert active is not None
    basis = active.price_basis | {"segment_id": active.id}
    marker = rows[entitlements.override_key(sub.id)]
    marker.value = marker.value | {"devices_limit": 9}
    with pytest.raises(DomainError) as denied:
        require_accepted_import(session, sub, basis)
    assert denied.value.code == "valuation_unknown"


def test_finite_catalog_baseline_is_not_replaced_by_included_devices(null_catalog):
    session, sub, _, _, rows, clock = null_catalog
    sub.devices_limit = 7
    marker = rows[entitlements.override_key(sub.id)]
    marker.value = marker.value | {"devices_limit": 7}
    active = catalog_projection(session, sub, clock[0])
    assert active.devices == 7
    assert "capacity_basis" not in active.price_basis["legacy_catalog"]


def test_following_device_purchase_preserves_frozen_base_and_first_actual_supplement(null_catalog):
    session, sub, product, _, _, clock = null_catalog
    first = catalog_projection(session, sub, clock[0])
    charge, adjustments = fixed_remaining_terms([first], clock[0], product, 1)
    first.devices = adjustments[0]["devices"]
    first.price_basis = accepted_basis(adjustments[0]["basis"])
    frozen = deepcopy(first.price_basis)
    second_charge, second = fixed_remaining_terms([first], clock[0], product, 1)
    assert charge == second_charge == Decimal("50")
    assert first.devices == 5 and second[0]["devices"] == 6
    assert first.ends_at == sub.expires_at
    assert first.price_basis == frozen
    assert len(second[0]["basis"]["device_adjustments"]) == 2
