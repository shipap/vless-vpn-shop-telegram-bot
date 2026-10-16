"""Owner-approved current panel facts priced on saved paid terms, never today's price."""

from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sunrise_core.errors import DomainError
from sunrise_infrastructure.db import Job, Order, Product, Quote, Segment, Subscription, now

from tests.integration.test_panel_28_workflows import panel28_workflow as panel28_workflow
from tests.integration.test_panel_entitlement_freshness import entitlement as entitlement


def saved_terms(context):
    db, _, _, _, state, sid, pid, segment_id, _ = context
    expiry = now() + timedelta(days=15)
    with db.transaction() as session:
        source = session.get(Segment, segment_id)
        source.price_basis = {
            "currency": "RUB",
            "period_days": 30,
            "months": 1,
            "device_price_factor": "1",
            "products": {pid: "37.00", "alternate": "111.00"},
            "device_pricing_included": True,
            "device_terms": {
                key: {"included": 4, "price": "5.00", "rules": []} for key in [pid, "alternate"]
            },
        }
        # The catalog can change without repricing the last payment.
        session.get(Product, pid).price = Decimal("999.00")
    state["user"].update(expireAt=expiry.isoformat(), hwidDeviceLimit=6)
    return expiry


def test_upgrade_uses_current_panel_duration_and_saved_comparable_prices(entitlement):
    db, _, commerce, _, _, sid, pid, segment_id, original_end = entitlement
    expiry = saved_terms(entitlement)
    result = commerce.quote(301, "alternate", 30, "upgrade", sid)
    assert result["total"] == "37.00"  # (121 - 47) * 15/30; current catalog is irrelevant.
    with db.transaction() as session:
        original = session.get(Segment, segment_id)
        assert (original.ends_at, original.devices, original.product_id) == (original_end, 4, pid)
        snapshot = session.get(Quote, result["id"]).snapshot
        assert snapshot["devices_limit"] == 6
        assert snapshot["resulting_expires_at"] == expiry.isoformat()
        assert snapshot["basis"]["products"] == {pid: "47.00", "alternate": "121.00"}


def test_devices_fulfill_current_panel_target_without_reapplying_old_override(entitlement):
    db, _, commerce, _, _, sid, pid, _, _ = entitlement
    expiry = saved_terms(entitlement)
    quote = commerce.quote(301, pid, 30, "devices", sid, extra_devices=1)
    assert quote["total"] == "5.00"
    order = commerce.create_order(301, quote["id"], "current-panel-device")
    commerce.test_pay(301, order["id"])
    with db.transaction() as session:
        sub = session.get(Subscription, sid)
        assert (sub.devices_limit, sub.expires_at) == (7, expiry)
        job = session.scalar(select(Job).where(Job.key == "provision:" + order["id"]))
        assert job.payload["devices_limit"] == 7
        assert job.payload["expires_at"] == expiry.isoformat()
    # Repeated payment handling must not add another device or period.
    commerce.test_pay(301, order["id"])
    with db.transaction() as session:
        assert session.get(Subscription, sid).devices_limit == 7


def test_unknown_paid_terms_remain_blocked_after_refresh(entitlement):
    db, _, commerce, _, _, sid, _, segment_id, _ = entitlement
    saved_terms(entitlement)
    with db.transaction() as session:
        session.get(Segment, segment_id).price_basis = {}
    with pytest.raises(DomainError) as failure:
        commerce.quote(301, "alternate", 30, "upgrade", sid)
    assert failure.value.code == "valuation_unknown"


def test_manual_extension_can_use_last_paid_segment_after_its_original_end(entitlement):
    db, _, commerce, _, _, sid, _, segment_id, _ = entitlement
    expiry = saved_terms(entitlement)
    with db.transaction() as session:
        source = session.get(Segment, segment_id)
        source.starts_at = now() - timedelta(days=31)
        source.ends_at = now() - timedelta(days=1)
    result = commerce.quote(301, "alternate", 30, "upgrade", sid)
    assert result["total"] == "37.00"
    with db.transaction() as session:
        assert session.get(Quote, result["id"]).snapshot["resulting_expires_at"] == expiry.isoformat()


def test_panel_shortening_and_device_purchase_preserve_future_paid_segment(entitlement):
    db, _, commerce, _, _, sid, pid, _, original_end = entitlement
    saved_terms(entitlement)
    with db.transaction() as session:
        future = Segment(
            subscription_id=sid,
            product_id=pid,
            starts_at=original_end,
            ends_at=original_end + timedelta(days=30),
            devices=4,
            paid_value=Decimal("37.00"),
            price_basis={
                "currency": "RUB",
                "period_days": 30,
                "products": {pid: "37.00"},
                "device_terms": {pid: {"included": 4, "price": "5.00", "rules": []}},
            },
        )
        session.add(future)
        session.flush()
        future_id = future.id
    quote = commerce.quote(301, pid, 30, "devices", sid, extra_devices=1)
    order = commerce.create_order(301, quote["id"], "panel-device-future")
    commerce.test_pay(301, order["id"])
    with db.transaction() as session:
        future = session.get(Segment, future_id)
        assert (future.starts_at, future.ends_at, future.devices) == (
            original_end,
            original_end + timedelta(days=30),
            5,
        )
        assert session.get(Subscription, sid).expires_at == original_end + timedelta(days=30)


def test_downgrade_uses_panel_current_tier_and_frozen_device_terms(entitlement):
    db, _, commerce, _, state, sid, pid, _, _ = entitlement
    saved_terms(entitlement)
    alternate_squad = "30000000-0000-4000-8000-000000000009"
    with db.transaction() as session:
        session.get(Product, "alternate").internal_squad_uuid = alternate_squad
    state["user"]["activeInternalSquads"] = [{"uuid": alternate_squad}]
    quote = commerce.quote(301, pid, 30, "downgrade", sid)
    assert quote["total"] == "0.00"
    with db.transaction() as session:
        snapshot = session.get(Quote, quote["id"]).snapshot
        start = datetime.fromisoformat(snapshot["starts_at"])
        end = datetime.fromisoformat(snapshot["resulting_expires_at"])
        assert abs((end - start).total_seconds() - 3336510) < 10  # 15d * 121/47


@pytest.mark.parametrize(
    "days,months,device_factor,expected",
    [(90, 3, "1", "126.00"), (365, 12, "1", "504.00"), (90, 3, "0.8", "123.00")],
)
def test_panel_device_revaluation_preserves_paid_period_months_and_device_factor(
    entitlement, days, months, device_factor, expected
):
    db, _, commerce, _, state, sid, pid, segment_id, _ = entitlement
    saved_terms(entitlement)
    state["user"]["expireAt"] = (now() + timedelta(days=days / 2)).isoformat()
    with db.transaction() as session:
        segment = session.get(Segment, segment_id)
        basis = dict(segment.price_basis)
        basis["period_days"], basis["months"] = days, months
        basis["device_price_factor"] = device_factor
        basis["products"] = {pid: str(Decimal(37) * months), "alternate": str(Decimal(111) * months)}
        terms = dict(basis["device_terms"])
        terms["alternate"] = terms["alternate"] | {"price": "10.00"}
        basis["device_terms"] = terms
        segment.price_basis = basis
    quote = commerce.quote(301, "alternate", 30, "upgrade", sid)
    assert quote["total"] == expected


@pytest.mark.parametrize("changed_basis", [False, True])
def test_legacy_normal_basis_derives_device_factor_only_for_original_paid_basis(entitlement, changed_basis):
    db, _, commerce, _, _, sid, pid, segment_id, _ = entitlement
    saved_terms(entitlement)
    with db.transaction() as session:
        quote = Quote(
            account_id=301,
            subscription_id=sid,
            expires_at=now(),
            snapshot={
                "action": "purchase",
                "base_amount": "37.00",
                "discount_amount": "1.48",
                "payment_method_discount_amount": "1.48",
            },
        )
        session.add(quote)
        session.flush()
        order = Order(
            account_id=301,
            quote_id=quote.id,
            idempotency_key="historic-paid",
            amount=Decimal("35.52"),
            currency="RUB",
            status="paid",
        )
        session.add(order)
        session.flush()
        source = session.get(Segment, segment_id)
        source.order_id = order.id
        basis = dict(source.price_basis)
        basis.pop("device_price_factor")
        basis["products"] = {pid: "35.52", "alternate": "106.56"}
        if changed_basis:
            basis["segment_id"] = source.id
            basis["products"] = {pid: "31.968", "alternate": "95.904"}
        source.price_basis = basis
    if changed_basis:
        with pytest.raises(DomainError) as rejected:
            commerce.quote(301, "alternate", 30, "upgrade", sid)
        assert rejected.value.code == "valuation_unknown"
        return
    quote = commerce.quote(301, "alternate", 30, "upgrade", sid)
    assert quote["total"] == "35.52"


def test_manual_capacity_reduction_does_not_guess_fixed_slot_paid_value(entitlement):
    db, _, commerce, _, state, sid, _, segment_id, _ = entitlement
    saved_terms(entitlement)
    state["user"]["hwidDeviceLimit"] = 4
    with db.transaction() as session:
        source = session.get(Segment, segment_id)
        source.devices = 5
        source.price_basis = source.price_basis | {
            "device_adjustments": [
                {
                    "pricing": "fixed_remaining_term_v9",
                    "before_devices": 4,
                    "after_devices": 5,
                    "charge": "5.00",
                    "remaining_seconds": "1296000",
                }
            ],
        }
    with pytest.raises(DomainError) as rejected:
        commerce.quote(301, "alternate", 30, "upgrade", sid)
    assert rejected.value.code == "valuation_unknown"


def test_expired_current_panel_period_cannot_shift_a_separately_paid_future(entitlement):
    db, _, commerce, _, state, sid, pid, _, original_end = entitlement
    saved_terms(entitlement)
    state["user"]["expireAt"] = (now() - timedelta(days=1)).isoformat()
    with db.transaction() as session:
        session.add(
            Segment(
                subscription_id=sid,
                product_id=pid,
                starts_at=original_end,
                ends_at=original_end + timedelta(days=30),
                devices=4,
                paid_value=Decimal("37.00"),
                price_basis={"currency": "RUB", "period_days": 30, "products": {pid: "37.00"}},
            )
        )
    with pytest.raises(DomainError) as rejected:
        commerce.quote(301, "alternate", 30, "upgrade", sid)
    assert rejected.value.code == "no_paid_segment"


def test_manual_fact_change_after_projection_invalidates_order(entitlement):
    _, _, commerce, _, state, sid, _, _, _ = entitlement
    saved_terms(entitlement)
    quote = commerce.quote(301, "alternate", 30, "upgrade", sid)
    state["user"]["hwidDeviceLimit"] = 8
    with pytest.raises(DomainError) as rejected:
        commerce.create_order(301, quote["id"], "manual-projection-stale")
    assert rejected.value.code == "quote_stale"
