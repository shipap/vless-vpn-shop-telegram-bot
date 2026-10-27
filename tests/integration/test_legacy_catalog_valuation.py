"""Verified imported access uses catalog valuation, never fabricated old payments."""

import json
from datetime import datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from sunrise_core.errors import DomainError
from sunrise_infrastructure.billing import Billing, VerifiedPayment
from sunrise_infrastructure.db import (
    Account,
    Alias,
    Job,
    Order,
    Payment,
    Product,
    Quote,
    RefundReview,
    Segment,
    Setting,
    Subscription,
    now,
)
from sunrise_infrastructure.panel import panel_expiry
from sunrise_infrastructure.panel_events import PanelEvents
from sunrise_infrastructure.panel_v3 import RemnawaveProduction, production_binding_key

from tests.integration.test_production_import_authority import imported_cleanup as imported_cleanup
from tests.legal_fixture import accept_test_legal


@pytest.fixture
def legacy_catalog(imported_cleanup):
    db, commerce, state, sid, _ = imported_cleanup
    expiry = now() + timedelta(days=15)
    with db.transaction() as session:
        sub = session.get(Subscription, sid)
        aid = sub.account_id
        session.get(Account, aid).legacy_consent = {
            "production_access": {
                "version": 1,
                "account_id": str(aid),
                "state": "active",
                "source": "legacy_user_is_active",
                "source_account_id": str(aid),
            }
        }
        sub.status, sub.devices_limit = "active", 4
        standard = session.get(Product, "standard")
        standard.price, standard.extra_device_price = Decimal("100"), Decimal("50")
        standard.included_devices = 4
        standard.internal_squad_uuid = commerce.runtime_policy.scope.panel.internal_squads[0]
        standard.presentation = {"base_period_days": 30}
        for product in session.scalars(select(Product)):
            if product.id != standard.id:
                product.internal_squad_uuid = None
        session.add(
            Product(
                id="legacy-premium",
                code="legacy-premium",
                name="Старший тестовый тариф",
                tier_level=2,
                price=Decimal("300"),
                currency="RUB",
                included_devices=4,
                extra_device_price=Decimal("50"),
                internal_squad_uuid="33333333-3333-4333-8333-333333333333",
            )
        )
        session.get(Setting, "discounts").value = {"cap": "0", "bonus_cap": "0", "secondary": []}
        gates = session.get(Setting, "production_runtime_gates")
        gates.value = gates.value | {"invoice_create": True, "financial_apply": True}
        session.add(
            Setting(
                key="payment_methods",
                value={
                    "version": 1,
                    "minimum_subscription_amount": "1.00",
                    "methods": [
                        {
                            "id": "sbp",
                            "label": "СБП",
                            "provider": "rollypay",
                            "provider_method": "sbp",
                            "discount_percent": "0",
                            "enabled": True,
                            "currencies": ["RUB"],
                            "mapping_contract": "synthetic",
                        }
                    ],
                },
            )
        )
    state["user"].update(status="ACTIVE", expireAt=expiry.isoformat(), hwidDeviceLimit=4)
    accept_test_legal(commerce, aid)
    assert (
        PanelEvents(db, commerce.panel_provider, commerce.public_settings).reconcile_owned(sid)["status"]
        == "ok"
    )
    return db, commerce, state, sid, aid, expiry


def quote(context, action="devices", product="standard", period=30, **kwargs):
    _, commerce, _, sid, aid, _ = context
    return commerce.quote(aid, product, period, action, sid, payment_method="sbp", **kwargs)


def fulfill(context, quote_id, key="legacy-device"):
    db, commerce, _, _, aid, _ = context
    order = commerce.create_order(aid, quote_id, key)
    with db.transaction() as session:
        amount = session.get(Order, order["id"]).amount
    if amount > 0:
        Billing(commerce, "synthetic-terminal", "live").accept(
            VerifiedPayment(
                provider="rollypay",
                provider_id="synthetic:" + order["id"],
                terminal="synthetic-terminal",
                environment="live",
                order_id=order["id"],
                amount=amount,
                currency="RUB",
                paid_at=now(),
            )
        )
    return order


def test_imported_devices_quote_is_fixed_catalog_price_and_does_not_create_history(legacy_catalog):
    db, _, _, _, _, _ = legacy_catalog
    result = quote(legacy_catalog, extra_devices=1)
    assert result["total"] == "50.00"
    with db.transaction() as session:
        assert list(session.scalars(select(Segment))) == []
        assert list(session.scalars(select(Order))) == []
        assert list(session.scalars(select(Payment))) == []
        basis = session.get(Quote, result["id"]).snapshot["basis"]
        assert basis["legacy_catalog"]["origin"] == "legacy_catalog"


@pytest.fixture
def legacy_null_catalog(legacy_catalog):
    from sunrise_infrastructure.panel_entitlements import override_key

    db, _, state, sid, _, expiry = legacy_catalog
    evidence = {
        "kind": "explicit_null_preserved",
        "present": True,
        "source": "authenticated_panel_snapshot",
        "until": expiry.isoformat(),
    }
    with db.transaction() as session:
        session.get(Subscription, sid).devices_limit = None
        root = session.get(Setting, production_binding_key(sid))
        root.value = root.value | {"capacity_evidence": evidence}
        marker = session.get(Setting, override_key(sid))
        marker.value = marker.value | {"devices_limit": None, "capacity_evidence": evidence}
        session.add(Setting(key="legacy_null_device_limit", value={"mode": "catalog_included"}))
    state["user"]["hwidDeviceLimit"] = None
    return legacy_catalog


def test_null_catalog_devices_acceptance_and_next_slot_preserve_expiry_and_identity(legacy_null_catalog):
    db, _, _, sid, _, expiry = legacy_null_catalog
    first = quote(legacy_null_catalog, extra_devices=1)
    assert first["total"] == "50.00"
    fulfill(legacy_null_catalog, first["id"], "null-first-device")
    complete_target(legacy_null_catalog)
    second = quote(legacy_null_catalog, extra_devices=1)
    assert second["total"] == "50.00"
    fulfill(legacy_null_catalog, second["id"], "null-second-device")
    with db.transaction() as session:
        sub = session.get(Subscription, sid)
        assert sub.devices_limit == 6 and sub.expires_at == expiry
        assert sub.panel_user_id == 8 and sub.panel_uuid == "22222222-2222-4222-8222-222222222222"
        segments = list(session.scalars(select(Segment).where(Segment.subscription_id == sid)))
        assert len(segments) == 1 and segments[0].paid_value == Decimal("100")


def test_null_catalog_upgrade_uses_valued_devices_without_comparing_null(legacy_null_catalog):
    result = quote(legacy_null_catalog, action="upgrade", product="legacy-premium")
    assert result["total"] == "100.00" and result["devices_limit"] == 4


def test_imported_devices_acceptance_preserves_identity_expiry_and_is_idempotent(legacy_catalog):
    db, commerce, _, sid, aid, expiry = legacy_catalog
    result = quote(legacy_catalog, extra_devices=1)
    order = fulfill(legacy_catalog, result["id"])
    with db.transaction() as session:
        sub = session.get(Subscription, sid)
        assert (sub.devices_limit, sub.expires_at, sub.panel_user_id, sub.panel_uuid) == (
            5,
            expiry,
            8,
            "22222222-2222-4222-8222-222222222222",
        )
        anchor = session.scalar(select(Segment).where(Segment.subscription_id == sid))
        assert anchor.kind == "legacy_catalog"
        assert anchor.order_id is None
        assert anchor.paid_value == Decimal("50")
        job = session.scalar(select(Job).where(Job.key == "provision:" + order["id"]))
        assert job.payload["devices_limit"] == 5
        assert job.payload["expires_at"] == expiry.isoformat()
        commerce._fulfill(
            session,
            session.get(Account, aid),
            session.get(Order, order["id"]),
            session.get(Quote, result["id"]),
        )
    with db.transaction() as session:
        assert len(list(session.scalars(select(Segment)))) == 1
        assert session.get(Subscription, sid).devices_limit == 5


def test_imported_upgrade_uses_catalog_remainder(legacy_catalog):
    result = quote(legacy_catalog, action="upgrade", product="legacy-premium")
    assert result["total"] == "100.00"


def test_absence_of_paid_basis_is_not_import_authority(legacy_catalog):
    db, _, _, sid, _, _ = legacy_catalog
    with db.transaction() as session:
        proof = session.get(Setting, production_binding_key(sid))
        proof.value = proof.value | {"origin": "new_project_identity"}
    with pytest.raises(DomainError) as failure:
        quote(legacy_catalog, extra_devices=1)
    assert failure.value.code == "valuation_unknown"


@pytest.mark.parametrize("period", [90, 365])
def test_devices_charge_ignores_requested_period(legacy_catalog, period):
    assert quote(legacy_catalog, period=period, extra_devices=2)["total"] == "100.00"


@pytest.mark.parametrize("changed", ["source", "comparable", "periods"])
def test_legacy_catalog_quote_rejects_every_relevant_catalog_change(legacy_catalog, changed):
    db, commerce, _, _, aid, _ = legacy_catalog
    result = quote(legacy_catalog, extra_devices=1)
    with db.transaction() as session:
        if changed == "periods":
            setting = session.get(Setting, "periods")
            setting.value = [item | {"discount_percent": "1"} for item in setting.value]
        else:
            product = session.get(Product, "standard" if changed == "source" else "legacy-premium")
            product.price += Decimal("1")
            product.version += 1
    with pytest.raises(DomainError) as failure:
        commerce.create_order(aid, result["id"], "stale-catalog")
    assert failure.value.code == "quote_stale"
    with db.transaction() as session:
        assert list(session.scalars(select(Segment))) == []
        assert list(session.scalars(select(Order))) == []


@pytest.mark.parametrize("kind", ["paid", "promo_gift", "refunded"])
def test_bad_new_or_gift_basis_cannot_be_replaced_by_import_catalog(legacy_catalog, kind):
    db, _, _, sid, _, expiry = legacy_catalog
    with db.transaction() as session:
        session.add(
            Segment(
                subscription_id=sid,
                product_id="standard",
                starts_at=now() - timedelta(days=1),
                ends_at=expiry,
                devices=4,
                paid_value=Decimal("0"),
                price_basis={},
                kind=kind,
            )
        )
    with pytest.raises(DomainError) as failure:
        quote(legacy_catalog, extra_devices=1)
    assert failure.value.code in {"valuation_unknown", "valuation_currency_mismatch"}
    with db.transaction() as session:
        assert session.scalar(select(Segment.id).where(Segment.kind == "legacy_catalog")) is None


@pytest.mark.parametrize("base_days,comparable_days,expected", [(60, 30, "125.00"), (30, 60, "25.00")])
def test_catalog_month_uses_configured_base_duration(legacy_catalog, base_days, comparable_days, expected):
    db, _, _, _, _, _ = legacy_catalog
    with db.transaction() as session:
        session.get(Product, "standard").presentation = {"base_period_days": base_days}
        session.get(Product, "legacy-premium").presentation = {"base_period_days": comparable_days}
    result = quote(legacy_catalog, action="upgrade", product="legacy-premium")
    assert result["total"] == expected


def test_finite_imported_devices_preserve_future_actual_paid_terms(legacy_catalog):
    db, _, _, sid, _, expiry = legacy_catalog
    future_end = expiry + timedelta(days=30)
    with db.transaction() as session:
        future = Segment(
            subscription_id=sid,
            product_id="standard",
            starts_at=expiry,
            ends_at=future_end,
            devices=4,
            paid_value=Decimal("9"),
            price_basis={
                "currency": "RUB",
                "period_days": 30,
                "products": {"standard": "9.00"},
                "device_terms": {"standard": {"included": 4, "price": "3", "rules": []}},
            },
        )
        session.add(future)
        session.flush()
        fid = future.id
        session.get(Subscription, sid).expires_at = future_end
    result = quote(legacy_catalog, extra_devices=1)
    assert result["total"] == "50.00"
    fulfill(legacy_catalog, result["id"])
    with db.transaction() as session:
        future = session.get(Segment, fid)
        assert (future.starts_at, future.ends_at, future.devices) == (expiry, future_end, 5)
        assert future.price_basis["device_terms"]["standard"]["price"] == "3"
        assert session.get(Subscription, sid).expires_at == future_end


def test_downgrade_catalog_value_extends_access_without_invented_payment(legacy_catalog):
    db, _, state, sid, _, _ = legacy_catalog
    state["user"]["activeInternalSquads"] = [{"uuid": "33333333-3333-4333-8333-333333333333"}]
    result = quote(legacy_catalog, action="downgrade")
    assert result["total"] == "0.00"
    fulfill(legacy_catalog, result["id"], "legacy-downgrade")
    with db.transaction() as session:
        anchor = session.scalar(select(Segment).where(Segment.subscription_id == sid))
        assert anchor.paid_value == 0
        assert anchor.kind == "legacy_catalog"
        assert (anchor.ends_at - anchor.starts_at).total_seconds() == pytest.approx(45 * 86400, abs=10)
        assert list(session.scalars(select(Payment))) == []


def complete_target(context):
    """Keep the synthetic provider aligned with a completed durable target."""
    db, commerce, state, sid, _, _ = context
    with db.transaction() as session:
        sub = session.get(Subscription, sid)
        for job in session.scalars(select(Job).where(Job.kind == "provision", Job.state == "pending")):
            job.state = "done"
            job.payload = job.payload | {
                "panel_binding": session.get(Setting, production_binding_key(sid)).value["binding"]
            }
        sub.status = "active"
        target = session.scalar(
            select(Job.payload).where(
                Job.kind == "provision", Job.payload["version"].as_integer() == sub.version
            )
        )
        state["user"].update(
            expireAt=panel_expiry(target["expires_at"]),
            hwidDeviceLimit=target["devices_limit"],
            activeInternalSquads=[{"uuid": session.get(Product, sub.product_id).internal_squad_uuid}],
        )
    assert (
        PanelEvents(db, commerce.panel_provider, commerce.public_settings).reconcile_owned(sid)["status"]
        == "ok"
    )


def test_later_device_purchase_keeps_frozen_legacy_and_actual_increments(legacy_catalog):
    db, _, _, sid, _, _ = legacy_catalog
    first = quote(legacy_catalog, extra_devices=1)
    fulfill(legacy_catalog, first["id"])
    complete_target(legacy_catalog)
    with db.transaction() as session:
        session.get(Product, "standard").price = Decimal("999")
        original = session.scalar(select(Segment).where(Segment.subscription_id == sid))
        original_products = dict(original.price_basis["products"])
    second = quote(legacy_catalog, extra_devices=1)
    assert second["total"] == "50.00"
    fulfill(legacy_catalog, second["id"], "second-legacy-device")
    with db.transaction() as session:
        anchors = list(session.scalars(select(Segment).where(Segment.subscription_id == sid)))
        assert len(anchors) == 1
        assert anchors[0].paid_value == Decimal("100")
        assert anchors[0].price_basis["products"]["standard"] != "999"
        assert Decimal(anchors[0].price_basis["products"]["standard"]) > Decimal(
            original_products["standard"]
        )


def test_manual_capacity_change_after_paid_device_does_not_guess_saved_supplement(legacy_catalog):
    _, _, state, _, _, _ = legacy_catalog
    fulfill(legacy_catalog, quote(legacy_catalog, extra_devices=1)["id"])
    complete_target(legacy_catalog)
    state["user"]["hwidDeviceLimit"] = 6
    with pytest.raises(DomainError) as failure:
        quote(legacy_catalog, extra_devices=1)
    assert failure.value.code == "valuation_unknown"


def test_discounted_device_splits_only_actual_new_value_across_legacy_and_future(legacy_catalog):
    db, _, state, sid, _, _ = legacy_catalog
    expiry = now() + timedelta(days=1)
    state["user"]["expireAt"] = expiry.isoformat()
    with db.transaction() as session:
        methods = session.get(Setting, "payment_methods")
        methods.value = methods.value | {"methods": [methods.value["methods"][0] | {"discount_percent": "4"}]}
        session.get(Setting, "discounts").value = {"cap": "100", "bonus_cap": "0", "secondary": []}
        future = Segment(
            subscription_id=sid,
            product_id="standard",
            starts_at=expiry,
            ends_at=expiry + timedelta(days=365),
            devices=4,
            paid_value=Decimal("99"),
            price_basis={
                "currency": "RUB",
                "period_days": 365,
                "products": {"standard": "99.00"},
                "device_terms": {"standard": {"included": 4, "price": "3", "rules": []}},
            },
        )
        session.add(future)
        session.flush()
        fid = future.id
    result = quote(legacy_catalog, extra_devices=1)
    assert result["total"] == "48.00"
    with db.transaction() as session:
        basis = session.get(Quote, result["id"]).snapshot["basis"]
        allocations = basis["device_segment_adjustments"]
        assert sum(Decimal(item["charge"]) for item in allocations) == Decimal("48")
        # The legacy capital100 and original actual future99 are never discounted again.
        source = allocations[0]
        source_duration = Decimal(source["basis"]["device_adjustments"][-1]["remaining_seconds"])
        assert (
            Decimal(source["basis"]["products"]["standard"])
            == Decimal("100") + Decimal(source["charge"]) * 30 * 86400 / source_duration
        )
        future_basis = allocations[1]
        assert Decimal(future_basis["basis"]["products"]["standard"]) == Decimal("99") + Decimal(
            future_basis["charge"]
        )
    fulfill(legacy_catalog, result["id"])
    with db.transaction() as session:
        anchor = session.scalar(select(Segment).where(Segment.kind == "legacy_catalog"))
        future = session.get(Segment, fid)
        assert anchor.paid_value >= 0
        assert (future.paid_value - Decimal("99")) + anchor.paid_value == Decimal("48")


def test_accepted_payment_binding_conflict_remains_durable_reconciliation(legacy_catalog):
    db, commerce, _, sid, aid, _ = legacy_catalog
    result = quote(legacy_catalog, extra_devices=1)
    order = commerce.create_order(aid, result["id"], "legacy-bound-conflict")
    with db.transaction() as session:
        proof = session.get(Setting, production_binding_key(sid))
        proof.value = proof.value | {"origin": "new_project_identity"}
    Billing(commerce, "synthetic-terminal", "live").accept(
        VerifiedPayment(
            provider="rollypay",
            provider_id="synthetic-conflict",
            terminal="synthetic-terminal",
            environment="live",
            order_id=order["id"],
            amount=Decimal("50"),
            currency="RUB",
            paid_at=now(),
        )
    )
    with db.transaction() as session:
        assert session.get(Order, order["id"]).status == "reconciliation_required"
        assert session.scalar(select(Payment).where(Payment.order_id == order["id"])).status == "paid"
        assert session.get(Subscription, sid).devices_limit == 4
        assert list(session.scalars(select(Segment))) == []


def test_refunded_future_history_is_not_shifted_by_legacy_downgrade(legacy_catalog):
    db, _, state, sid, _, expiry = legacy_catalog
    state["user"]["activeInternalSquads"] = [{"uuid": "33333333-3333-4333-8333-333333333333"}]
    start, end = expiry + timedelta(days=1), expiry + timedelta(days=2)
    with db.transaction() as session:
        refunded = Segment(
            subscription_id=sid,
            product_id="standard",
            starts_at=start,
            ends_at=end,
            devices=4,
            paid_value=Decimal("99"),
            price_basis={},
            kind="refunded",
        )
        session.add(refunded)
        session.flush()
        rid = refunded.id
    fulfill(legacy_catalog, quote(legacy_catalog, action="downgrade")["id"], "future-refund-history")
    with db.transaction() as session:
        refunded = session.get(Segment, rid)
        assert (refunded.starts_at, refunded.ends_at, refunded.kind) == (start, end, "refunded")


def test_future_gift_is_not_converted_to_catalog_paid_access(legacy_catalog):
    db, _, _, sid, _, expiry = legacy_catalog
    with db.transaction() as session:
        session.add(
            Segment(
                subscription_id=sid,
                product_id="standard",
                starts_at=expiry,
                ends_at=expiry + timedelta(days=10),
                devices=4,
                paid_value=Decimal(0),
                price_basis={},
                kind="promo_gift",
            )
        )
    with pytest.raises(DomainError) as failure:
        quote(legacy_catalog, action="upgrade", product="legacy-premium")
    assert failure.value.code == "valuation_unknown"


def test_manual_capacity_uses_normalized_catalog_monthly_values(legacy_catalog):
    db, _, state, _, _, _ = legacy_catalog
    with db.transaction() as session:
        session.get(Product, "legacy-premium").presentation = {"base_period_days": 60}
    first = quote(legacy_catalog, action="upgrade", product="legacy-premium")
    fulfill(legacy_catalog, first["id"])
    complete_target(legacy_catalog)
    state["user"]["hwidDeviceLimit"] = 5
    result = quote(legacy_catalog, action="downgrade")
    with db.transaction() as session:
        basis = session.get(Quote, result["id"]).snapshot["basis"]
        assert Decimal(basis["products"]["standard"]) == Decimal("150")
        assert Decimal(basis["products"]["legacy-premium"]) == Decimal("175")


def test_fixed_device_increases_actual_panel_limit_below_catalog_included(legacy_catalog):
    db, _, state, sid, _, _ = legacy_catalog
    state["user"]["hwidDeviceLimit"] = 2
    result = quote(legacy_catalog, extra_devices=1)
    assert result["total"] == "50.00"
    with db.transaction() as session:
        assert session.get(Quote, result["id"]).snapshot["devices_limit"] == 3
    fulfill(legacy_catalog, result["id"])
    with db.transaction() as session:
        assert session.get(Subscription, sid).devices_limit == 3


def test_parallel_quotes_cannot_add_two_legacy_anchors(legacy_catalog):
    db, commerce, _, _, aid, _ = legacy_catalog
    first = quote(legacy_catalog, extra_devices=1)
    second = quote(legacy_catalog, extra_devices=1)
    fulfill(legacy_catalog, first["id"])
    complete_target(legacy_catalog)
    with pytest.raises(DomainError) as failure:
        commerce.create_order(aid, second["id"], "parallel-legacy")
    assert failure.value.code == "quote_stale"
    with db.transaction() as session:
        assert len(list(session.scalars(select(Segment)))) == 1


def test_verified_alias_retains_import_proof_for_catalog_valuation(legacy_catalog):
    db, _, _, sid, aid, _ = legacy_catalog
    old_owner = 9007199254740995
    with db.transaction() as session:
        session.add(Alias(old_id=old_owner, canonical_id=aid))
        proof = session.get(Setting, production_binding_key(sid))
        proof.value = proof.value | {"account_id": str(old_owner)}
        marker = session.get(Setting, "panel_entitlement_override:" + sid)
        marker.value = marker.value | {"account_id": str(old_owner)}
    assert quote(legacy_catalog, extra_devices=1)["total"] == "50.00"


def test_paid_renewal_wins_over_legacy_catalog_after_boundary(legacy_catalog):
    db, commerce, state, sid, _, _ = legacy_catalog
    fulfill(legacy_catalog, quote(legacy_catalog, extra_devices=1)["id"])
    complete_target(legacy_catalog)
    renewed = quote(legacy_catalog, action="renew")
    renewal_order = fulfill(legacy_catalog, renewed["id"], "new-actual-renewal")
    with db.transaction() as session:
        paid = session.scalar(select(Segment).where(Segment.order_id == renewal_order["id"]))
        assert paid.kind == "paid"
        paid.starts_at = now() - timedelta(seconds=1)
        anchor = session.scalar(select(Segment).where(Segment.kind == "legacy_catalog"))
        anchor.ends_at = paid.starts_at
        for job in session.scalars(select(Job).where(Job.kind == "provision")):
            job.state = "done"
        sub = session.get(Subscription, sid)
        sub.status = "active"
        state["user"].update(expireAt=paid.ends_at.isoformat(), hwidDeviceLimit=5)
        session.get(Product, "standard").price = Decimal("999")
        paid_id = paid.id
        actual_value = paid.price_basis["products"]["standard"]
    commerce.apply_segment_boundaries()
    complete_target(legacy_catalog)
    result = quote(legacy_catalog, action="upgrade", product="legacy-premium")
    with db.transaction() as session:
        basis = session.get(Quote, result["id"]).snapshot["basis"]
        assert basis["segment_id"] == paid_id
        assert "legacy_catalog" not in basis
        assert basis["products"]["standard"] == actual_value


def test_full_addon_refund_stays_visible_and_does_not_revoke_legacy_catalog(legacy_catalog):
    db, commerce, _, sid, _, _ = legacy_catalog
    order = fulfill(legacy_catalog, quote(legacy_catalog, extra_devices=1)["id"])
    Billing(commerce, "synthetic-terminal", "live").refund(
        "synthetic:" + order["id"], "synthetic-refund", Decimal("50"), now()
    )
    with db.transaction() as session:
        anchor = session.scalar(select(Segment).where(Segment.subscription_id == sid))
        assert anchor.kind == "legacy_catalog"
        assert session.scalar(select(RefundReview)) is not None
        assert session.get(Order, order["id"]).status == "refund_review"
        assert session.scalar(select(Payment).where(Payment.order_id == order["id"])).status == "refunded"


def test_actual_durable_provision_retry_preserves_imported_uuid_and_single_anchor(legacy_catalog):
    db, commerce, state, sid, _, expiry = legacy_catalog
    state["user"]["uuid"] = "22222222-2222-4222-8222-222222222222"
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == "/api/hwid/devices/8":
            return httpx.Response(200, json={"response": {"devices": [], "total": 0}})
        if request.method == "PATCH":
            assert request.url.path == "/api/users"
            body = json.loads(request.content)
            body["activeInternalSquads"] = [{"uuid": value} for value in body["activeInternalSquads"]]
            state["user"].update(body)
            if state.pop("lose_update", False):
                raise httpx.ReadTimeout("synthetic accepted update response lost")
        else:
            assert request.method == "GET" and request.url.path == "/api/users/8"
        return httpx.Response(200, json={"response": state["user"]})

    panel = RemnawaveProduction(
        runtime_policy=commerce.runtime_policy,
        token="synthetic-only",  # noqa: S106
        transport=httpx.MockTransport(handler),
    )
    old_panel = commerce.panel_provider
    commerce.panel_provider = panel
    try:
        result = quote(legacy_catalog, extra_devices=1)
        order = fulfill(legacy_catalog, result["id"])
        state["lose_update"] = True
        assert commerce.provisioning.run_jobs() == 0
        with db.transaction() as session:
            job = session.scalar(select(Job).where(Job.key == "provision:" + order["id"]))
            assert job.state in {"pending", "in_flight"}
            job.available_at = now() - timedelta(seconds=1)
        assert commerce.provisioning.run_jobs() == 1
        assert commerce.provisioning.run_jobs() == 0
        with db.transaction() as session:
            sub = session.get(Subscription, sid)
            assert sub.devices_limit == 5
            assert sub.panel_uuid == "22222222-2222-4222-8222-222222222222"
            assert sub.expires_at == expiry
            assert len(list(session.scalars(select(Segment)))) == 1
            assert len(list(session.scalars(select(Payment)))) == 1
        assert all(method != "POST" for method, _ in calls)
        assert state["user"]["hwidDeviceLimit"] == 5
        assert abs((datetime.fromisoformat(state["user"]["expireAt"]) - expiry).total_seconds()) < 1
    finally:
        commerce.panel_provider = old_panel
        panel.client.close()


def test_device_after_downgrade_does_not_repeat_the_historical_future_shift(legacy_catalog):
    db, _, state, sid, _, expiry = legacy_catalog
    state["user"]["activeInternalSquads"] = [{"uuid": "33333333-3333-4333-8333-333333333333"}]
    with db.transaction() as session:
        future = Segment(
            subscription_id=sid,
            product_id="standard",
            starts_at=expiry,
            ends_at=expiry + timedelta(days=30),
            devices=4,
            paid_value=Decimal("9"),
            price_basis={
                "currency": "RUB",
                "period_days": 30,
                "products": {"standard": "9"},
                "device_terms": {"standard": {"included": 4, "price": "3", "rules": []}},
            },
        )
        session.add(future)
        session.flush()
        fid = future.id
    fulfill(legacy_catalog, quote(legacy_catalog, action="downgrade")["id"], "legacy-downgrade-first")
    complete_target(legacy_catalog)
    with db.transaction() as session:
        future = session.get(Segment, fid)
        original = future.starts_at, future.ends_at
    result = quote(legacy_catalog, extra_devices=1)
    with db.transaction() as session:
        assert session.get(Quote, result["id"]).snapshot["basis"]["future_shift_seconds"] == 0
    fulfill(legacy_catalog, result["id"], "legacy-downgrade-device")
    with db.transaction() as session:
        future = session.get(Segment, fid)
        assert (future.starts_at, future.ends_at) == original


@pytest.mark.parametrize("previous_action", ["renew", "devices"])
def test_missing_actual_paid_segment_never_bootstraps_catalog_again(legacy_catalog, previous_action):
    db, _, state, sid, _, _ = legacy_catalog
    result = quote(
        legacy_catalog,
        action=previous_action,
        **({"extra_devices": 1} if previous_action == "devices" else {}),
    )
    fulfill(legacy_catalog, result["id"], "actual-before-damage")
    complete_target(legacy_catalog)
    with db.transaction() as session:
        for segment in session.scalars(select(Segment).where(Segment.subscription_id == sid)):
            session.delete(segment)
        original = session.get(Quote, result["id"])
        original.snapshot = original.snapshot | {"starts_at": (now() - timedelta(days=1)).isoformat()}
    # A fresh manual snapshot still cannot manufacture the lost actual new payment basis.
    state["user"]["hwidDeviceLimit"] = 6
    with pytest.raises(DomainError) as failure:
        quote(legacy_catalog, extra_devices=1)
    assert failure.value.code == "valuation_unknown"


def test_actual_future_renewal_does_not_block_legacy_devices_after_provisioning(legacy_catalog):
    db, _, _, sid, _, _ = legacy_catalog
    renewal = fulfill(legacy_catalog, quote(legacy_catalog, action="renew")["id"], "legacy-actual-future")
    complete_target(legacy_catalog)
    result = quote(legacy_catalog, extra_devices=1)
    assert result["total"] == "50.00"
    order = fulfill(legacy_catalog, result["id"], "devices-after-actual-future")
    with db.transaction() as session:
        future = session.scalar(select(Segment).where(Segment.order_id == renewal["id"]))
        assert future.kind == "paid" and future.devices == 5
        assert session.get(Subscription, sid).expires_at == future.ends_at
        job = session.scalar(select(Job).where(Job.key == "provision:" + order["id"]))
        assert datetime.fromisoformat(job.payload["expires_at"]) == future.ends_at


def test_arbitrary_manual_overlap_cannot_double_value_future_paid_time(legacy_catalog):
    db, _, state, sid, _, expiry = legacy_catalog
    with db.transaction() as session:
        session.add(
            Segment(
                subscription_id=sid,
                product_id="standard",
                starts_at=expiry,
                ends_at=expiry + timedelta(days=30),
                devices=4,
                paid_value=Decimal("9"),
                price_basis={"currency": "RUB", "period_days": 30, "products": {"standard": "9"}},
            )
        )
    state["user"]["expireAt"] = (expiry + timedelta(days=30)).isoformat()
    with pytest.raises(DomainError) as failure:
        quote(legacy_catalog, action="upgrade", product="legacy-premium")
    assert failure.value.code == "valuation_unknown"


@pytest.mark.parametrize("change_identity", [False, True], ids=["short-rotation", "foreign-nonce"])
def test_approved_short_rotation_preserves_legacy_aggregate_horizon(legacy_catalog, change_identity):
    db, commerce, _, sid, _, _ = legacy_catalog
    fulfill(legacy_catalog, quote(legacy_catalog, extra_devices=1)["id"])
    complete_target(legacy_catalog)
    fulfill(legacy_catalog, quote(legacy_catalog, action="renew")["id"], "legacy-renew-before-rotation")
    complete_target(legacy_catalog)
    with db.transaction() as session:
        sub = session.get(Subscription, sid)
        original_end = sub.expires_at
        proof = session.get(Setting, production_binding_key(sid))
        binding = proof.value["binding"] | {"short_uuid": "rotatedsyntheticshortuuid456"}
        if change_identity:
            binding["origin_nonce"] = "c" * 32
        sub.short_uuid = binding["short_uuid"]
        sub.connection_url = "https://sub.synthetic.test/" + sub.short_uuid
        proof.value = proof.value | {"binding": binding, "connection_url": sub.connection_url}
        target = commerce._provision_payload(session, sub, origin="paid")
        if change_identity:
            assert datetime.fromisoformat(target["expires_at"]) < original_end
        else:
            assert panel_expiry(target["expires_at"]) == panel_expiry(original_end.isoformat())


@pytest.mark.parametrize("paid_basis", [False, True], ids=["legacy-catalog", "actual-paid"])
@pytest.mark.parametrize("method_percent,expected", [("4", "292"), ("0", "300")])
def test_upgrade_discount_changes_only_new_paid_increment(
    legacy_catalog, paid_basis, method_percent, expected
):
    db, _, _, sid, _, expiry = legacy_catalog
    with db.transaction() as session:
        if paid_basis:
            session.add(
                Segment(
                    subscription_id=sid,
                    product_id="standard",
                    starts_at=now() - timedelta(days=1),
                    ends_at=expiry,
                    devices=4,
                    paid_value=Decimal("100"),
                    price_basis={
                        "currency": "RUB",
                        "period_days": 30,
                        "months": 1,
                        "products": {"standard": "100", "legacy-premium": "300"},
                        "device_price_factor": "1",
                        "device_pricing_included": True,
                        "device_terms": {
                            pid: {"included": 4, "price": "50", "rules": []}
                            for pid in ["standard", "legacy-premium"]
                        },
                    },
                )
            )
        methods = session.get(Setting, "payment_methods")
        methods.value = methods.value | {
            "methods": [methods.value["methods"][0] | {"discount_percent": method_percent}]
        }
        session.get(Setting, "discounts").value = {"cap": "100", "bonus_cap": "0", "secondary": []}
    result = quote(legacy_catalog, action="upgrade", product="legacy-premium")
    with db.transaction() as session:
        snapshot = session.get(Quote, result["id"]).snapshot
        basis = snapshot["basis"]
        assert Decimal(basis["products"]["standard"]) == Decimal("100")
        assert abs(Decimal(basis["products"]["legacy-premium"]) - Decimal(expected)) < Decimal(".01")
        assert basis["device_price_factor"] == "1"
    order = fulfill(legacy_catalog, result["id"], "upgrade-actual-increment")
    with db.transaction() as session:
        segment = session.scalar(select(Segment).where(Segment.subscription_id == sid))
        assert segment.paid_value == (Decimal("100") if paid_basis else Decimal(0)) + Decimal(result["total"])
        assert session.get(Order, order["id"]).status == "paid"
