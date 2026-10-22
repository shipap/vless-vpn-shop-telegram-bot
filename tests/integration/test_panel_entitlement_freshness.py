"""Synthetic owned panel facts; financial history remains immutable."""

from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sunrise_core.errors import DomainError
from sunrise_core.refunds import VerifiedEntitlementUsage
from sunrise_infrastructure.billing import Billing
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.customer_operations import CustomerOperations
from sunrise_infrastructure.db import Job, Payment, Product, Quote, Segment, Setting, Subscription, now
from sunrise_infrastructure.panel import PanelUnavailable

from tests.integration.test_panel_28_workflows import TG
from tests.integration.test_panel_28_workflows import panel28_workflow as panel28_workflow
from tests.integration.test_panel_auto_provisioning import SQUAD
from tests.integration.test_payment_pipeline import SyntheticProvider, enqueue
from tests.integration.test_v11_panel_events import owned_panel as owned_panel
from tests.legal_fixture import accept_test_legal


@pytest.fixture
def entitlement(panel28_workflow):
    db, panel, commerce, events, _, state, sid, accounts = panel28_workflow
    accounts.settings.dashboard_url = "https://dashboard.example.test"
    commerce.public_settings = accounts.settings
    expiry = datetime.fromisoformat(state["user"]["expireAt"])
    with db.transaction() as tx:
        for item in tx.scalars(select(Product)):
            item.internal_squad_uuid = SQUAD if item.code == "standard" else None
        product = tx.scalar(select(Product).where(Product.code == "standard"))
        product.price, product.extra_device_price = Decimal("37.00"), Decimal("5.00")
        product.included_devices = 4
        segment = Segment(
            subscription_id=sid,
            product_id=product.id,
            starts_at=now() - timedelta(days=1),
            ends_at=expiry,
            devices=4,
            paid_value=Decimal("37.00"),
            price_basis={"currency": "RUB", "period_days": 30, "products": {product.id: "37.00"}},
        )
        tx.add(segment)
        tx.flush()
        sub = tx.get(Subscription, sid)
        sub.product_id, sub.expires_at = product.id, expiry
        job = tx.scalar(select(Job).where(Job.kind == "provision"))
        job.payload = job.payload | {"active_segment_id": segment.id, "product_id": product.id}
        tx.get(Setting, "discounts").value = {"cap": "0", "bonus_cap": "0", "secondary": []}
        product_id, segment_id = product.id, segment.id
        tx.add(
            Product(
                id="alternate",
                code="alternate",
                name="Другой тестовый тариф",
                tier_level=2,
                price=Decimal("111.00"),
                currency="RUB",
                included_devices=4,
                extra_device_price=Decimal("5.00"),
            )
        )
    return db, panel, commerce, events, state, sid, product_id, segment_id, expiry


def renewal(context):
    return context[2].quote(301, context[6], 30, "renew", context[5])


def test_own_list_and_selected_checkout_refresh_only_owned_subscription(entitlement):
    from sunrise_infrastructure.db import Account

    db, _, commerce, _, state, sid, product_id, _, _ = entitlement
    with db.transaction() as tx:
        other = Account(email="foreign-owned@synthetic.test", email_verified=True)
        tx.add(other)
        tx.flush()
        tx.add(
            Subscription(
                id="foreign-owned",
                account_id=other.id,
                product_id=product_id,
                panel_uuid="33333333-3333-4333-8333-333333333333",
                environment="local_test",
            )
        )
    initial = len(state["calls"])
    commerce.dashboard(301, refresh_panel=True)
    list_calls = state["calls"][initial:]
    assert list_calls and all("33333333-3333-4333-8333-333333333333" not in str(call) for call in list_calls)
    initial = len(state["calls"])
    commerce.dashboard(301, refresh_panel=True, refresh_subscription_id=sid)
    assert state["calls"][initial:] == list_calls
    initial = len(state["calls"])
    with pytest.raises(DomainError) as denied:
        commerce.dashboard(301, refresh_panel=True, refresh_subscription_id="foreign-owned")
    assert denied.value.status == 404 and len(state["calls"]) == initial


def test_concurrent_selected_refresh_is_bounded_to_one_snapshot(entitlement):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    _, _, commerce, _, state, sid, _, _, _ = entitlement
    started, release = Event(), Event()

    def pause(_):
        started.set()
        assert release.wait(15)

    state["next_read_hook"] = pause
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(commerce._refresh_panel, 301, sid)
        try:
            assert started.wait(15)
            calls = len(state["calls"])
            assert commerce._refresh_panel(301, sid) == {sid: {"status": "busy"}}
            assert len(state["calls"]) == calls
        finally:
            release.set()
        assert future.result(timeout=15)[sid]["status"] == "ok"


@pytest.mark.parametrize("capacity,total", [(12, "77.00"), (2, "37.00")])
def test_manual_panel_limit_and_extension_drive_profile_and_renewal(entitlement, capacity, total):
    db, _, commerce, _, state, sid, _, segment_id, old_expiry = entitlement
    expiry = old_expiry + timedelta(days=10)
    state["user"].update(hwidDeviceLimit=capacity, expireAt=expiry.isoformat())
    profile = commerce.dashboard(301, refresh_panel=True)["subscriptions"][0]
    assert profile["expires_at"] == expiry.isoformat()
    assert profile["devices_limit"] == profile["panel_device_limit"] == capacity
    assert profile["panel_sync_state"] == "fresh"
    quote = renewal(entitlement)
    assert quote["total"] == total
    with db.transaction() as tx:
        from sunrise_infrastructure.db import Quote

        snapshot = tx.get(Quote, quote["id"]).snapshot
        assert snapshot["devices_limit"] == capacity
        assert datetime.fromisoformat(snapshot["starts_at"]) == expiry
        segment = tx.get(Segment, segment_id)
        assert (segment.ends_at, segment.devices, segment.paid_value) == (old_expiry, 4, Decimal("37.00"))
    commerce.apply_segment_boundaries()
    with db.transaction() as tx:
        assert tx.get(Subscription, sid).devices_limit == capacity
        assert not list(tx.scalars(select(Job).where(Job.state == "pending")))


@pytest.mark.parametrize("limit", [0, None])
def test_unlimited_or_global_fallback_is_observed_but_never_guessed_for_price(entitlement, limit):
    state = entitlement[4]
    state["user"]["hwidDeviceLimit"] = limit
    profile = entitlement[2].dashboard(301, refresh_panel=True)["subscriptions"][0]
    assert profile["panel_device_limit"] == limit
    assert profile["renewal_pricing_state"] == "blocked"
    with pytest.raises(DomainError) as failure:
        renewal(entitlement)
    assert failure.value.code == "panel_capacity_unpriced"


def test_provider_outage_keeps_cached_profile_and_blocks_renewal(entitlement, monkeypatch):
    _, panel, commerce, _, _, _, _, _, expiry = entitlement
    assert commerce.dashboard(301, refresh_panel=True)["subscriptions"][0]["panel_sync_state"] == "fresh"

    def unavailable(_):
        raise PanelUnavailable("synthetic outage")

    monkeypatch.setattr(panel, "snapshot", unavailable)
    profile = commerce.dashboard(301, refresh_panel=True)["subscriptions"][0]
    assert profile["expires_at"] == expiry.isoformat()
    assert profile["panel_sync_state"] == "stale"
    with pytest.raises(DomainError) as failure:
        renewal(entitlement)
    assert failure.value.code == "panel_sync_required"


def test_changed_panel_fact_invalidates_old_quote_before_order_creation(entitlement):
    db, _, commerce, _, state, sid, _, _, _ = entitlement
    quote = renewal(entitlement)
    state["user"]["hwidDeviceLimit"] = 12
    with pytest.raises(DomainError) as failure:
        commerce.create_order(301, quote["id"], "changed-panel-fact")
    assert failure.value.code == "quote_stale"
    with db.transaction() as tx:
        assert tx.get(Subscription, sid).devices_limit == 12


def test_paid_pending_target_and_version_race_are_not_overwritten(entitlement):
    db, _, _, events, state, sid, _, _, expiry = entitlement

    def paid_target(_):
        with db.transaction() as tx:
            sub = tx.get(Subscription, sid)
            sub.version += 1
            sub.devices_limit, sub.expires_at, sub.status = (
                8,
                expiry + timedelta(days=30),
                "provisioning_pending",
            )
            tx.add(
                Job(
                    key="synthetic-paid-race",
                    kind="provision",
                    account_id=301,
                    payload={"subscription_id": sid, "version": sub.version},
                )
            )

    state["next_read_hook"] = paid_target
    state["user"]["hwidDeviceLimit"] = 12
    assert events.reconcile_owned(sid)["status"] == "pending"
    with db.transaction() as tx:
        sub = tx.get(Subscription, sid)
        assert (sub.devices_limit, sub.expires_at) == (8, expiry + timedelta(days=30))


def test_healthy_future_paid_horizon_is_not_a_manual_shortening(entitlement):
    db, _, commerce, events, _, sid, product_id, _, expiry = entitlement
    future_end = expiry + timedelta(days=30)
    with db.transaction() as tx:
        tx.add(
            Segment(
                subscription_id=sid,
                product_id=product_id,
                starts_at=expiry,
                ends_at=future_end,
                devices=6,
                paid_value=Decimal("47.00"),
                price_basis={},
            )
        )
        tx.get(Subscription, sid).expires_at = future_end
    assert events.reconcile_owned(sid)["status"] == "ok"
    with db.transaction() as tx:
        assert tx.get(Setting, "panel_entitlement_override:" + sid) is None
    profile = commerce.dashboard(301, refresh_panel=True)["subscriptions"][0]
    assert profile["expires_at"] == expiry.isoformat()
    assert datetime.fromisoformat(profile["paid_horizon"]) == future_end
    quote = renewal(entitlement)
    with db.transaction() as tx:
        from sunrise_infrastructure.db import Quote

        assert datetime.fromisoformat(tx.get(Quote, quote["id"]).snapshot["starts_at"]) == future_end


def test_manual_shortening_is_effective_without_erasing_future_paid_rights(entitlement):
    db, _, commerce, _, state, sid, product_id, segment_id, expiry = entitlement
    shorter = expiry - timedelta(days=10)
    with db.transaction() as tx:
        future = Segment(
            subscription_id=sid,
            product_id=product_id,
            starts_at=expiry,
            ends_at=expiry + timedelta(days=30),
            devices=6,
            paid_value=Decimal("47.00"),
            price_basis={},
        )
        tx.add(future)
        tx.flush()
        future_id = future.id
    state["user"]["expireAt"] = shorter.isoformat()
    assert (
        commerce.dashboard(301, refresh_panel=True)["subscriptions"][0]["expires_at"] == shorter.isoformat()
    )
    commerce.apply_segment_boundaries()
    with db.transaction() as tx:
        assert tx.get(Segment, segment_id).ends_at == expiry
        assert tx.get(Segment, future_id).starts_at == expiry
        assert not list(tx.scalars(select(Job).where(Job.state == "pending")))
    with pytest.raises(DomainError) as failure:
        commerce.quote(301, product_id, 30, "devices", sid, extra_devices=1)
    assert failure.value.code == "valuation_unknown"


def test_extension_gap_provision_payload_preserves_nonfinancial_access(entitlement):
    db, _, commerce, _, state, sid, _, segment_id, expiry = entitlement
    with db.transaction() as tx:
        tx.get(Segment, segment_id).ends_at = now() - timedelta(days=1)
        job = tx.scalar(select(Job).where(Job.kind == "provision"))
        job.payload = job.payload | {"expires_at": (now() - timedelta(days=1)).isoformat()}
    state["user"]["expireAt"] = expiry.isoformat()
    commerce.dashboard(301, refresh_panel=True)
    with db.transaction() as tx:
        target = commerce._provision_payload(tx, tx.get(Subscription, sid))
        assert target["panel_status"] == "ACTIVE"
        assert datetime.fromisoformat(target["expires_at"]) == expiry
        assert target["active_segment_id"] is None
        assert len(list(tx.scalars(select(Segment)))) == 1


@pytest.mark.parametrize("status", ["DISABLED", "LIMITED"])
def test_manual_panel_status_is_truthful_and_never_reactivated_by_boundary(entitlement, status):
    db, _, commerce, _, state, sid, _, _, _ = entitlement
    state["user"]["status"] = status
    profile = commerce.dashboard(301, refresh_panel=True)["subscriptions"][0]
    assert profile["panel_status"] == status
    assert profile["status"] == ("suspended" if status == "DISABLED" else "limited")
    with pytest.raises(DomainError) as failure:
        renewal(entitlement)
    assert failure.value.code == "panel_access_restricted"
    commerce.apply_segment_boundaries()
    with db.transaction() as tx:
        assert commerce._provision_payload(tx, tx.get(Subscription, sid))["panel_status"] == "DISABLED"


def test_stricter_local_revocation_is_preserved_when_panel_is_active(entitlement):
    db, _, commerce, _, _, sid, _, _, _ = entitlement
    with db.transaction() as tx:
        tx.get(Subscription, sid).status = "revoked"
    assert commerce.dashboard(301, refresh_panel=True)["subscriptions"][0]["status"] == "revoked"


def test_pending_rotation_blocks_snapshot_before_http(entitlement):
    db, _, _, events, state, sid, _, _, _ = entitlement
    with db.transaction() as tx:
        tx.add(
            Job(
                key="synthetic-rotation-pending",
                kind="credential_rotate",
                account_id=301,
                payload={"subscription_id": sid},
            )
        )
    calls = len(state["calls"])
    assert events.reconcile_owned(sid)["status"] == "pending"
    assert len(state["calls"]) == calls


def test_manual_override_retires_at_next_paid_boundary(entitlement, monkeypatch):
    db, _, commerce, _, state, sid, product_id, _, expiry = entitlement
    state["user"].update(hwidDeviceLimit=12, expireAt=(expiry - timedelta(days=10)).isoformat())
    commerce.dashboard(301, refresh_panel=True)
    with db.transaction() as tx:
        future = Segment(
            subscription_id=sid,
            product_id=product_id,
            starts_at=expiry,
            ends_at=expiry + timedelta(days=30),
            devices=6,
            paid_value=Decimal("47.00"),
            price_basis={},
        )
        tx.add(future)
        tx.flush()
        future_id = future.id
    moment = expiry + timedelta(seconds=1)
    monkeypatch.setattr("sunrise_infrastructure.commerce.now", lambda: moment)
    monkeypatch.setattr("sunrise_infrastructure.panel_entitlements.now", lambda: moment)
    commerce.apply_segment_boundaries()
    with db.transaction() as tx:
        assert tx.get(Setting, "panel_entitlement_override:" + sid) is None
        sub = tx.get(Subscription, sid)
        assert sub.devices_limit == 6
        target = tx.scalar(select(Job).where(Job.state == "pending")).payload
        assert target["active_segment_id"] == future_id
        assert datetime.fromisoformat(target["expires_at"]) == expiry + timedelta(days=30)


def test_superseded_or_foreign_done_target_is_not_used_as_expected_fact(entitlement):
    db, _, _, events, _, sid, _, _, expiry = entitlement
    with db.transaction() as tx:
        tx.add(
            Job(
                key="synthetic-superseded",
                kind="provision",
                state="done",
                last_error="superseded",
                account_id=301,
                payload={
                    "subscription_id": sid,
                    "version": 999,
                    "expires_at": (expiry + timedelta(days=100)).isoformat(),
                    "devices_limit": 50,
                },
            )
        )
    assert events.reconcile_owned(sid)["status"] == "ok"
    with db.transaction() as tx:
        assert tx.get(Setting, "panel_entitlement_override:" + sid) is None


def test_manual_panel_tariff_maps_to_current_catalog_without_repricing_history(entitlement):
    db, _, commerce, _, state, sid, old_product, segment_id, expiry = entitlement
    squad = "20000000-0000-4000-8000-000000000009"
    with db.transaction() as tx:
        product = tx.scalar(select(Product).where(Product.code == "alternate"))
        product.internal_squad_uuid, product.price, product.extra_device_price = (
            squad,
            Decimal("111.00"),
            Decimal("5.00"),
        )
        product.included_devices = 4
        new_product = product.id
    state["user"]["activeInternalSquads"] = [{"uuid": squad, "name": "Synthetic squad"}]
    profile = commerce.dashboard(301, refresh_panel=True)["subscriptions"][0]
    assert profile["product_id"] == profile["panel_product_id"] == new_product
    assert profile["panel_product_state"] == "matched"
    with pytest.raises(DomainError) as failure:
        renewal(entitlement)
    assert failure.value.code == "panel_product_changed"
    quote = commerce.quote(301, new_product, 30, "renew", sid)
    assert quote["total"] == "111.00"
    commerce.apply_segment_boundaries()
    with db.transaction() as tx:
        assert tx.get(Subscription, sid).product_id == new_product
        segment = tx.get(Segment, segment_id)
        assert (segment.product_id, segment.ends_at, segment.paid_value) == (
            old_product,
            expiry,
            Decimal("37.00"),
        )
    with pytest.raises(DomainError) as failure:
        commerce.quote(301, new_product, 30, "devices", sid, extra_devices=1)
    assert failure.value.code == "valuation_unknown"


@pytest.mark.parametrize("kind", ["unknown", "ambiguous", "partially_unknown"])
def test_unknown_or_ambiguous_squad_never_guesses_renewal_price(entitlement, kind):
    db, _, commerce, _, state, _, _, _, _ = entitlement
    if kind == "ambiguous":
        with db.transaction() as tx:
            tx.scalar(select(Product).where(Product.code == "alternate")).internal_squad_uuid = SQUAD
    else:
        state["user"]["activeInternalSquads"] = [{"uuid": "20000000-0000-4000-8000-000000000009"}]
        if kind == "partially_unknown":
            state["user"]["activeInternalSquads"].append({"uuid": SQUAD})
    profile = commerce.dashboard(301, refresh_panel=True)["subscriptions"][0]
    assert profile["panel_product_state"] == ("ambiguous" if kind == "ambiguous" else "unknown")
    assert profile["panel_product_id"] is None
    assert profile["renewal_pricing_state"] == "blocked"
    with pytest.raises(DomainError) as failure:
        renewal(entitlement)
    assert failure.value.code == "panel_product_unverified"


@pytest.mark.parametrize("limit", [101, 9007199254740991])
def test_unsupported_positive_capacity_is_visible_without_integer_overflow_or_price_guess(entitlement, limit):
    entitlement[4]["user"]["hwidDeviceLimit"] = limit
    profile = entitlement[2].dashboard(301, refresh_panel=True)["subscriptions"][0]
    assert profile["panel_device_limit"] == limit
    assert profile["devices_limit"] == 4
    with pytest.raises(DomainError) as failure:
        renewal(entitlement)
    assert failure.value.code == "panel_capacity_unpriced"


def test_shared_profile_and_renewal_sync_also_supports_numeric_panel(owned_panel):
    db, panel, worker, state, aid, sid, _ = owned_panel
    assert worker.run_jobs() == 1
    with db.transaction() as tx:
        for product in tx.scalars(select(Product)):
            product.internal_squad_uuid = (
                state["user"]["activeInternalSquads"][0]["uuid"] if product.id == "standard" else None
            )
        tx.get(Subscription, sid).product_id = "standard"
        tx.get(Setting, "discounts").value = {"cap": "0", "bonus_cap": "0", "secondary": []}
    commerce = Commerce(db, local_test=True, panel_provider=panel)
    accept_test_legal(commerce, aid)
    state["user"].update(hwidDeviceLimit=12, expireAt=(now() + timedelta(days=40)).isoformat())
    profile = commerce.dashboard(aid, refresh_panel=True)["subscriptions"][0]
    assert profile["panel_sync_state"] == "fresh"
    assert profile["panel_product_state"] == "matched"
    assert profile["devices_limit"] == 12
    quote = commerce.quote(aid, "standard", 30, "renew", sid)
    assert quote["devices_limit"] == 12


def test_owner_restriction_and_revocation_remain_stricter_than_panel_expiry(entitlement):
    db, _, commerce, _, state, sid, _, _, _ = entitlement
    state["user"]["expireAt"] = (now() - timedelta(days=1)).isoformat()
    with db.transaction() as tx:
        tx.get(Subscription, sid).status = "revoked"
    assert commerce.dashboard(301, refresh_panel=True)["subscriptions"][0]["status"] == "revoked"


@pytest.mark.parametrize("archived_duplicate", [True, False])
def test_catalog_mapping_uses_only_enabled_products(entitlement, archived_duplicate):
    db, _, commerce, _, _, _, product_id, _, _ = entitlement
    with db.transaction() as tx:
        if archived_duplicate:
            product = tx.get(Product, "alternate")
            product.internal_squad_uuid, product.available = SQUAD, False
        else:
            tx.get(Product, product_id).available = False
    profile = commerce.dashboard(301, refresh_panel=True)["subscriptions"][0]
    assert profile["panel_product_state"] == ("matched" if archived_duplicate else "unknown")
    if archived_duplicate:
        assert profile["panel_product_id"] == product_id
        assert renewal(entitlement)["total"] == "37.00"
    else:
        with pytest.raises(DomainError) as failure:
            renewal(entitlement)
        assert failure.value.code == "panel_product_unverified"


def test_authorized_rotation_rebases_existing_manual_access_marker_atomically(entitlement):
    db, _, commerce, events, state, sid, _, _, expiry = entitlement
    state["user"].update(hwidDeviceLimit=12, expireAt=(expiry + timedelta(days=10)).isoformat())
    commerce.dashboard(301, refresh_panel=True)
    operations = CustomerOperations(commerce, events)
    job = operations.rotate_credentials(301, sid, "preserve-manual-marker", confirmed=True)
    assert commerce.provisioning.run_jobs() == 1
    with db.transaction() as tx:
        assert tx.get(Job, job["job_id"]).state == "done"
        assert (
            tx.get(Setting, "panel_entitlement_override:" + sid).value["binding"]
            == tx.get(Setting, "panel_test:" + sid).value["binding"]
        )
    commerce.apply_segment_boundaries()
    with db.transaction() as tx:
        assert tx.get(Subscription, sid).devices_limit == 12
        assert not list(tx.scalars(select(Job).where(Job.state == "pending")))


@pytest.mark.parametrize("bad_field", ["binding", "environment", "version", "subscription_id"])
def test_rotation_never_rebases_unrelated_or_stale_override_provenance(entitlement, bad_field):
    db, _, commerce, events, state, sid, _, _, expiry = entitlement
    state["user"].update(hwidDeviceLimit=12, expireAt=(expiry + timedelta(days=10)).isoformat())
    commerce.dashboard(301, refresh_panel=True)
    with db.transaction() as tx:
        row = tx.get(Setting, "panel_entitlement_override:" + sid)
        value = {
            "binding": row.value["binding"] | {"uuid": "20000000-0000-4000-8000-000000000009"},
            "environment": "live",
            "version": 999,
            "subscription_id": "foreign",
        }[bad_field]
        row.value = row.value | {bad_field: value}
        old_binding = row.value["binding"]
    CustomerOperations(commerce, events).rotate_credentials(301, sid, "negative-marker", confirmed=True)
    assert commerce.provisioning.run_jobs() == 1
    with db.transaction() as tx:
        assert tx.get(Setting, "panel_entitlement_override:" + sid).value["binding"] == old_binding


def test_uncertain_rotation_does_not_rebase_marker_until_verified_completion(entitlement):
    db, _, commerce, events, state, sid, _, _, expiry = entitlement
    state["user"].update(hwidDeviceLimit=12, expireAt=(expiry + timedelta(days=10)).isoformat())
    commerce.dashboard(301, refresh_panel=True)
    with db.transaction() as tx:
        old_binding = tx.get(Setting, "panel_entitlement_override:" + sid).value["binding"]
    CustomerOperations(commerce, events).rotate_credentials(301, sid, "uncertain-marker", confirmed=True)
    state["lose_rotation"] = True
    assert commerce.provisioning.run_jobs() == 0
    with db.transaction() as tx:
        assert tx.get(Setting, "panel_entitlement_override:" + sid).value["binding"] == old_binding


@pytest.mark.parametrize("limit", [0, None, 101])
def test_unsupported_current_limit_never_falls_back_in_mutation_target(entitlement, limit):
    db, _, commerce, events, state, sid, _, _, _ = entitlement
    state["user"]["hwidDeviceLimit"] = limit
    commerce.dashboard(301, refresh_panel=True)
    with db.transaction() as tx:
        assert commerce._provision_payload(tx, tx.get(Subscription, sid))["devices_limit"] == limit
    job = CustomerOperations(commerce, events).rotate_credentials(301, sid, "unpriced-rotate", confirmed=True)
    calls = len(state["calls"])
    assert commerce.provisioning.run_jobs() == 0
    assert len(state["calls"]) == calls
    with db.transaction() as tx:
        assert tx.get(Job, job["job_id"]).state in {"blocked", "review"}
    assert state["user"]["hwidDeviceLimit"] == limit


def test_catalog_mapping_edit_never_overwrites_verified_manual_squad(entitlement):
    db, _, commerce, _, state, sid, product_id, _, expiry = entitlement
    state["user"]["expireAt"] = (expiry + timedelta(days=1)).isoformat()
    commerce.dashboard(301, refresh_panel=True)
    with db.transaction() as tx:
        tx.get(Product, product_id).internal_squad_uuid = "20000000-0000-4000-8000-000000000009"
    with db.transaction() as tx:
        target = commerce._provision_payload(tx, tx.get(Subscription, sid))
        assert target["internal_squad_uuid"] in {None, SQUAD}


@pytest.fixture
def short_renewal(entitlement):
    db, panel, commerce, _, state, sid, product_id, old_segment_id, old_expiry = entitlement
    shortened = old_expiry - timedelta(days=10)
    state["user"]["expireAt"] = shortened.isoformat()
    commerce.sandbox_payments = True
    quote = renewal(entitlement)
    order = commerce.create_order(301, quote["id"], "shortened-real-synthetic-payment")
    billing, provider = Billing(commerce, "synthetic-terminal", "sandbox"), SyntheticProvider()
    billing.prepare_payment(order["id"], provider)
    enqueue(commerce, order)
    billing.reconcile_inbox(provider)
    assert commerce.order(301, order["id"])["status"] == "paid"
    # The mock panel handler now models actual UUID PATCH updates too.
    import json

    import httpx

    transport = panel.client._transport

    def patchable(request):
        if request.method == "PATCH":
            body = json.loads(request.content)
            state["user"].update(
                body | {"activeInternalSquads": [{"uuid": value} for value in body["activeInternalSquads"]]}
            )
        return transport.handle_request(request)

    panel.client._transport = httpx.MockTransport(patchable)
    assert commerce.provisioning.run_jobs() == 1
    with db.transaction() as tx:
        new_segment = tx.scalar(select(Segment).where(Segment.order_id == order["id"]))
        payment = tx.scalar(select(Payment).where(Payment.order_id == order["id"]))
        new_segment_id, payment_id = new_segment.id, payment.id
        assert new_segment.starts_at == shortened
        assert new_segment.ends_at == shortened + timedelta(days=30)
        assert tx.get(Segment, old_segment_id).ends_at == old_expiry
        assert datetime.fromisoformat(tx.get(Quote, quote["id"]).snapshot["starts_at"]) == shortened
    return entitlement, billing, new_segment_id, payment_id, shortened, order


def advance(monkeypatch, moment):
    monkeypatch.setattr("sunrise_infrastructure.commerce.now", lambda: moment)
    monkeypatch.setattr("sunrise_infrastructure.panel_entitlements.now", lambda: moment)
    monkeypatch.setattr("sunrise_infrastructure.billing.now", lambda: moment)
    monkeypatch.setattr("sunrise_infrastructure.refund_reviews.now", lambda: moment)
    monkeypatch.setattr("sunrise_infrastructure.promos.now", lambda: moment)
    monkeypatch.setattr("sunrise_infrastructure.panel_events.now", lambda: moment)


def test_shortened_paid_renewal_activates_once_without_changing_historical_interval(
    short_renewal, monkeypatch
):
    context, _, new_id, _, starts, _ = short_renewal
    db, _, commerce, _, _, sid, _, old_id, old_expiry = context
    advance(monkeypatch, starts + timedelta(seconds=1))
    commerce.apply_segment_boundaries()
    with db.transaction() as tx:
        jobs = list(tx.scalars(select(Job).where(Job.state == "pending", Job.kind == "provision")))
        assert len(jobs) == 1
        assert jobs[0].payload["active_segment_id"] == new_id
        assert datetime.fromisoformat(jobs[0].payload["expires_at"]) == starts + timedelta(days=30)
        assert tx.get(Segment, old_id).ends_at == old_expiry
    assert commerce.provisioning.run_jobs() == 1
    advance(monkeypatch, old_expiry + timedelta(seconds=1))
    commerce.apply_segment_boundaries()
    with db.transaction() as tx:
        assert commerce._provision_payload(tx, tx.get(Subscription, sid))["active_segment_id"] == new_id
        assert not list(tx.scalars(select(Job).where(Job.state == "pending", Job.kind == "provision")))


def test_refund_of_overlap_renewal_never_resurrects_shortened_old_rights_or_replays_value(
    short_renewal, monkeypatch
):
    context, billing, new_id, payment_id, starts, order = short_renewal
    db, _, commerce, _, _, sid, _, old_id, old_expiry = context
    moment = starts + timedelta(seconds=1)
    advance(monkeypatch, moment)
    commerce.apply_segment_boundaries()
    assert commerce.provisioning.run_jobs() == 1
    with db.transaction() as tx:
        new_segment, payment = tx.get(Segment, new_id), tx.get(Payment, payment_id)
        usage = VerifiedEntitlementUsage(
            order_id=order["id"],
            segment_id=new_id,
            started_at=new_segment.starts_at,
            observed_at=moment,
            traffic_bytes=0,
            source="verified_panel_attestation",
        )
        provider_id, amount = payment.provider_id, payment.amount
    first = billing.refund(provider_id, "synthetic-overlap-refund", amount, moment, usage=usage)
    assert first["entitlement_action"] == "automatic_revocation"
    billing.refund(provider_id, "synthetic-overlap-refund", amount, moment, usage=usage)
    commerce.apply_segment_boundaries()
    with db.transaction() as tx:
        assert tx.get(Segment, old_id).ends_at == old_expiry
        assert tx.get(Segment, old_id).paid_value == Decimal("37.00")
        assert tx.get(Segment, new_id).kind == "refunded"
        sub = tx.get(Subscription, sid)
        assert sub.status == "revocation_pending"
        assert commerce._provision_payload(tx, sub)["active_segment_id"] is None
        jobs = list(tx.scalars(select(Job).where(Job.key.like("refund-rights:%"))))
        assert len(jobs) == 1 and jobs[0].payload["panel_status"] == "DISABLED"


def test_owner_refund_resolution_uses_effective_remaining_rights(short_renewal, monkeypatch):
    from sunrise_infrastructure.db import Account, AdminAuthenticator, RefundReview, StaffRole
    from sunrise_infrastructure.refund_reviews import RefundReviews

    context, billing, new_id, payment_id, starts, _ = short_renewal
    db, _, commerce, _, _, sid, _, old_id, old_expiry = context
    moment = starts + timedelta(seconds=1)
    advance(monkeypatch, moment)
    commerce.apply_segment_boundaries()
    assert commerce.provisioning.run_jobs() == 1
    commerce.accounts.settings.admin_email_allowlist = ["synthetic-owner@example.test"]
    commerce.accounts.settings.admin_telegram_allowlist = [TG]
    with db.transaction() as tx:
        account = tx.get(Account, 301)
        account.email, account.email_verified = "synthetic-owner@example.test", True
        tx.add(StaffRole(account_id=301, role="owner"))
        tx.add(AdminAuthenticator(account_id=301, trust_verified_at=moment, trusted_ip_digest="a" * 64))
        payment = tx.get(Payment, payment_id)
        provider_id = payment.provider_id
    result = billing.refund(provider_id, "synthetic-partial-owner-review", Decimal("5.00"), moment)
    assert result["entitlement_action"] == "manual_review"
    with db.transaction() as tx:
        review = tx.scalar(select(RefundReview).where(RefundReview.segment_id == new_id))
        review_id, version = review.id, tx.get(Subscription, sid).version
    RefundReviews(commerce).resolve(
        301, review_id, "revoke", "Явное тестовое решение владельца", expected_subscription_version=version
    )
    commerce.apply_segment_boundaries()
    with db.transaction() as tx:
        assert tx.get(Subscription, sid).status == "revocation_pending"
        assert tx.get(Segment, old_id).ends_at == old_expiry
        assert tx.get(Segment, old_id).paid_value == Decimal("37.00")


def configure_gift(commerce):
    with commerce.db.transaction() as tx:
        policy = tx.get(Setting, "discounts")
        policy.value = policy.value | {
            "promos": [
                {
                    "code": "LOCALGIFT",
                    "reward_type": "free_days",
                    "reward_days": 5,
                    "product_id": "standard",
                    "enabled": True,
                    "audience": "all",
                    "per_account_limit": 1,
                }
            ]
        }
    commerce.activate_promo(301, "LOCALGIFT")


def test_free_days_use_effective_current_segment_after_short_renewal(short_renewal, monkeypatch):
    context, _, new_id, _, starts, _ = short_renewal
    db, _, commerce, _, _, sid, _, _, _ = context
    advance(monkeypatch, starts + timedelta(seconds=1))
    commerce.apply_segment_boundaries()
    assert commerce.provisioning.run_jobs() == 1
    configure_gift(commerce)
    result = commerce.claim_promo(301, "LOCALGIFT", sid)
    with db.transaction() as tx:
        current = tx.get(Segment, new_id)
        gift = tx.scalar(select(Segment).where(Segment.kind == "promo_gift"))
        assert gift.starts_at == current.ends_at
        assert gift.ends_at == current.ends_at + timedelta(days=5)
        assert gift.price_basis["panel_mappings"] == current.price_basis["panel_mappings"]
        assert gift.paid_value == 0
    assert result["reward_days"] == 5


@pytest.mark.parametrize("limit", [0, None, 101])
def test_free_days_never_invent_capacity_for_unsupported_current_panel_limit(entitlement, limit):
    _, _, commerce, _, state, sid, _, _, _ = entitlement
    state["user"]["hwidDeviceLimit"] = limit
    commerce.dashboard(301, refresh_panel=True)
    configure_gift(commerce)
    with pytest.raises(DomainError) as failure:
        commerce.claim_promo(301, "LOCALGIFT", sid)
    assert failure.value.code == "panel_capacity_unpriced"


def test_persisted_shortening_clamps_mutation_target_when_current_override_is_missing(entitlement):
    db, _, commerce, _, state, sid, _, _, expiry = entitlement
    shortened = expiry - timedelta(days=10)
    state["user"]["expireAt"] = shortened.isoformat()
    commerce.dashboard(301, refresh_panel=True)
    with db.transaction() as tx:
        tx.delete(tx.get(Setting, "panel_entitlement_override:" + sid))
    with db.transaction() as tx:
        target = commerce._provision_payload(tx, tx.get(Subscription, sid))
        assert datetime.fromisoformat(target["expires_at"]) == shortened


def test_paid_callback_during_rotation_rebases_only_existing_identity_metadata(entitlement):
    db, _, commerce, events, state, sid, _, segment_id, expiry = entitlement
    with db.transaction() as tx:
        past = now() - timedelta(days=1)
        tx.get(Segment, segment_id).ends_at = past
        job = tx.scalar(select(Job).where(Job.kind == "provision"))
        job.payload = job.payload | {"expires_at": past.isoformat()}
    extended = expiry + timedelta(days=70)
    state["user"].update(hwidDeviceLimit=12, expireAt=extended.isoformat())
    commerce.sandbox_payments = True
    quote = renewal(entitlement)
    order = commerce.create_order(301, quote["id"], "rotation-race-paid")
    billing, provider = Billing(commerce, "synthetic-terminal", "sandbox"), SyntheticProvider()
    billing.prepare_payment(order["id"], provider)
    with db.transaction() as tx:
        captured_version = tx.get(Subscription, sid).version
        marker_before = tx.get(Setting, "panel_entitlement_override:" + sid).value

    def first_read(_):
        def after_rotation_post(_):
            enqueue(commerce, order)
            billing.reconcile_inbox(provider)

        state["next_read_hook"] = after_rotation_post

    state["next_read_hook"] = first_read
    result = CustomerOperations(commerce, events).rotate_credentials(
        301, sid, "callback-during-rotate", confirmed=True
    )
    assert commerce.provisioning.run_jobs(limit=1) == 1
    with db.transaction() as tx:
        assert tx.get(Job, result["job_id"]).state == "done"
        sub = tx.get(Subscription, sid)
        assert sub.version > captured_version
        marker = tx.get(Setting, "panel_entitlement_override:" + sid).value
        assert marker["binding"] == tx.get(Setting, "panel_test:" + sid).value["binding"]
        assert {
            key: marker[key]
            for key in ("expires_at", "devices_limit", "panel_product_id", "version", "adopted_at")
        } == {
            key: marker_before[key]
            for key in ("expires_at", "devices_limit", "panel_product_id", "version", "adopted_at")
        }
    assert commerce.provisioning.run_jobs(limit=1) == 1
    assert events.reconcile_owned(sid)["status"] == "ok"
    with db.transaction() as tx:
        target = commerce._provision_payload(tx, tx.get(Subscription, sid))
        assert target["panel_status"] == "ACTIVE"
        assert datetime.fromisoformat(target["expires_at"]) == extended
        assert target["devices_limit"] == 12


@pytest.mark.parametrize("action", ["shorten", "recalculate"])
def test_explicit_refund_trim_preserves_verified_current_tariff_capacity_and_status(
    short_renewal, monkeypatch, action
):
    from sunrise_infrastructure.db import Account, AdminAuthenticator, RefundReview, StaffRole
    from sunrise_infrastructure.refund_reviews import RefundReviews

    context, billing, segment_id, payment_id, starts, _ = short_renewal
    db, panel, commerce, _, state, sid, _, _, _ = context
    moment = starts + timedelta(seconds=1)
    advance(monkeypatch, moment)
    commerce.apply_segment_boundaries()
    assert commerce.provisioning.run_jobs() == 1
    squad = "20000000-0000-4000-8000-000000000008"
    panel.squads.add(squad)
    with db.transaction() as tx:
        tx.get(Product, "alternate").internal_squad_uuid = squad
        original_end = tx.get(Segment, segment_id).ends_at
        original_value = tx.get(Segment, segment_id).paid_value
    state["user"].update(
        hwidDeviceLimit=5,
        expireAt=(original_end + timedelta(days=5)).isoformat(),
        activeInternalSquads=[{"uuid": squad}],
        status="DISABLED",
    )
    commerce.dashboard(301, refresh_panel=True)
    commerce.accounts.settings.admin_email_allowlist = ["synthetic-owner@example.test"]
    commerce.accounts.settings.admin_telegram_allowlist = [TG]
    with db.transaction() as tx:
        account = tx.get(Account, 301)
        account.email, account.email_verified = "synthetic-owner@example.test", True
        tx.add(StaffRole(account_id=301, role="owner"))
        tx.add(AdminAuthenticator(account_id=301, trust_verified_at=moment, trusted_ip_digest="a" * 64))
        provider_id = tx.get(Payment, payment_id).provider_id
        before = tx.get(Setting, "panel_entitlement_override:" + sid).value
    assert (
        billing.refund(provider_id, "synthetic-manual-trim", Decimal("5.00"), moment)["entitlement_action"]
        == "manual_review"
    )
    with db.transaction() as tx:
        review_id = tx.scalar(select(RefundReview).where(RefundReview.segment_id == segment_id)).id
        version = tx.get(Subscription, sid).version
    target = original_end - timedelta(days=2)
    RefundReviews(commerce).resolve(
        301,
        review_id,
        action,
        "Явное сокращение владельцем",
        ends_at=target.isoformat(),
        expected_subscription_version=version,
    )
    with db.transaction() as tx:
        sub = tx.get(Subscription, sid)
        marker = tx.get(Setting, "panel_entitlement_override:" + sid).value
        assert datetime.fromisoformat(marker["expires_at"]) == target
        for key in ("devices_limit", "status", "panel_product_id", "internal_squad_uuids", "binding"):
            assert marker[key] == before[key]
        assert sub.product_id == "alternate" and sub.devices_limit == 5
        payload = tx.scalar(select(Job).where(Job.key == "refund-review:" + review_id)).payload
        assert datetime.fromisoformat(payload["expires_at"]) == target
        assert payload["devices_limit"] == 5 and payload["internal_squad_uuid"] == squad
        assert payload["panel_status"] == "DISABLED"
        assert tx.get(Segment, segment_id).paid_value == original_value
    assert commerce.provisioning.run_jobs() == 1
    assert datetime.fromisoformat(state["user"]["expireAt"]) == target
    assert state["user"]["hwidDeviceLimit"] == 5
    assert state["user"]["activeInternalSquads"] == [{"uuid": squad}]


@pytest.mark.parametrize("remote_status", ["DISABLED", "LIMITED"])
def test_remote_restriction_across_paid_boundary_does_not_create_permanent_local_suspension(
    short_renewal, monkeypatch, remote_status
):
    context, _, _, _, starts, _ = short_renewal
    db, _, commerce, _, state, sid, _, _, _ = context
    state["user"]["status"] = remote_status
    assert commerce.dashboard(301, refresh_panel=True)["subscriptions"][0]["status"] == (
        "suspended" if remote_status == "DISABLED" else "limited"
    )
    advance(monkeypatch, starts + timedelta(seconds=1))
    commerce.apply_segment_boundaries()
    assert commerce.provisioning.run_jobs() == 1
    assert state["user"]["status"] == "DISABLED"
    with db.transaction() as tx:
        assert tx.get(Subscription, sid).status == "active"
    assert commerce.dashboard(301, refresh_panel=True)["subscriptions"][0]["status"] == (
        "suspended" if remote_status == "DISABLED" else "limited"
    )
    with pytest.raises(DomainError) as restricted:
        renewal(context)
    assert restricted.value.code == "panel_access_restricted"
    state["user"]["status"] = "ACTIVE"
    profile = commerce.dashboard(301, refresh_panel=True)["subscriptions"][0]
    assert profile["status"] == "active"
    assert profile["renewal_pricing_state"] == "ready"


@pytest.mark.parametrize(
    "local_origin,expected_status",
    [
        ("suspended", "suspended"),
        ("suspension_pending", "suspended"),
        ("disputed", "disputed"),
        ("revoked", "revoked"),
        ("provisioning_pending", "suspended"),
        ("owner_flag", "suspended"),
        ("dispute_flag", "disputed"),
    ],
)
def test_paid_boundary_preserves_strict_local_origin_despite_verified_panel_active(
    short_renewal, monkeypatch, local_origin, expected_status
):
    context, _, _, _, starts, _ = short_renewal
    db, _, commerce, _, state, sid, _, _, _ = context
    commerce.dashboard(301, refresh_panel=True)
    with db.transaction() as tx:
        if local_origin == "owner_flag":
            tx.add(Setting(key="restriction:" + sid, value={"suspended": True}))
        elif local_origin == "dispute_flag":
            tx.add(Setting(key="dispute:" + sid, value={"enabled": True}))
        else:
            tx.get(Subscription, sid).status = local_origin
    state["user"]["status"] = "ACTIVE"
    advance(monkeypatch, starts + timedelta(seconds=1))
    commerce.apply_segment_boundaries()
    assert commerce.provisioning.run_jobs() == 1
    assert state["user"]["status"] == "DISABLED"
    with db.transaction() as tx:
        assert tx.get(Subscription, sid).status == expected_status
    state["user"]["status"] = "ACTIVE"
    assert commerce.dashboard(301, refresh_panel=True)["subscriptions"][0]["status"] == expected_status


def test_second_paid_renewal_before_first_provision_preserves_known_pending_origin(entitlement):
    db, _, original, _, _, _, product_id, _, _ = entitlement
    commerce = Commerce(db, local_test=True)
    commerce.public_settings = original.public_settings
    first_quote = commerce.quote(301, product_id, 30, "purchase")
    first = commerce.create_order(301, first_quote["id"], "two-paid-before-provision-first")
    commerce.test_pay(301, first["id"])
    with db.transaction() as tx:
        from sunrise_infrastructure.db import Order

        sid = tx.get(Order, first["id"]).subscription_id
        assert tx.get(Subscription, sid).status == "provisioning_pending"
    second_quote = commerce.quote(301, product_id, 30, "renew", sid)
    second = commerce.create_order(301, second_quote["id"], "two-paid-before-provision-second")
    commerce.test_pay(301, second["id"])
    commerce.run_jobs()
    with db.transaction() as tx:
        assert tx.get(Subscription, sid).status == "active"
        periods = list(
            tx.scalars(select(Segment).where(Segment.subscription_id == sid).order_by(Segment.starts_at))
        )
        assert len(periods) == 2
        assert periods[0].ends_at == periods[1].starts_at
        assert sum(period.paid_value for period in periods) == Decimal("74.00")


@pytest.mark.parametrize(
    "invalid",
    [
        "binding",
        "owner",
        "environment",
        "source",
        "last_error",
        "stale",
        "unknown_squad",
        "zero_limit",
        "null_limit",
        "large_limit",
        "newer_target",
    ],
)
def test_restrictive_fallback_never_applies_unverified_or_unsupported_observation(entitlement, invalid):
    from sunrise_infrastructure.db import PanelObservation

    db, _, commerce, _, state, sid, _, segment_id, _ = entitlement
    commerce.dashboard(301, refresh_panel=True)
    with db.transaction() as tx:
        segment = tx.get(Segment, segment_id)
        segment.kind = "refunded"
        segment.ends_at = now()
        sub = tx.get(Subscription, sid)
        sub.version += 1
        sub.status = "revocation_pending"
        record = tx.scalar(select(PanelObservation).where(PanelObservation.subscription_id == sid))
        data = dict(record.payload)
        if invalid == "binding":
            data["binding"] = data["binding"] | {"short_uuid": "foreign"}
        elif invalid == "owner":
            data["account_id"] = "999"
        elif invalid == "environment":
            data["environment"] = "live"
        elif invalid == "source":
            data["source"] = "webhook"
        elif invalid == "last_error":
            record.last_error = "panel_unavailable"
        elif invalid == "stale":
            record.observed_at = now() - timedelta(minutes=11)
        elif invalid == "unknown_squad":
            data["internal_squad_uuids"] = ["20000000-0000-4000-8000-000000000009"]
        elif invalid.endswith("limit"):
            data["devices_limit"] = {"zero_limit": 0, "null_limit": None, "large_limit": 101}[invalid]
        else:
            latest = tx.scalar(select(Job).where(Job.kind == "provision", Job.state == "done"))
            tx.add(
                Job(
                    key="newer-successful-synthetic-target",
                    kind="provision",
                    state="done",
                    account_id=301,
                    payload=latest.payload | {"version": sub.version},
                )
            )
        record.payload = data
        payload = commerce._provision_payload(tx, sub)
        tx.add(Job(key="restrictive-negative-" + invalid, kind="provision", account_id=301, payload=payload))
    calls = len(state["calls"])
    assert commerce.provisioning.run_jobs() == 0
    assert len(state["calls"]) == calls
    with db.transaction() as tx:
        job = tx.scalar(select(Job).where(Job.key == "restrictive-negative-" + invalid))
        assert job.state in {"blocked", "review"}


@pytest.mark.parametrize("invalid", ["missing", "rejected"])
def test_restrictive_ended_history_requires_verified_current_observation(entitlement, invalid):
    from sunrise_infrastructure.db import PanelObservation

    db, _, commerce, _, state, sid, product_id, segment_id, _ = entitlement
    commerce.dashboard(301, refresh_panel=True)
    with db.transaction() as tx:
        segment = tx.get(Segment, segment_id)
        segment.ends_at = now() - timedelta(seconds=1)
        segment.price_basis = segment.price_basis | {"panel_mappings": {product_id: SQUAD}}
        assert segment.kind == "paid"
        sub = tx.get(Subscription, sid)
        sub.version += 1
        sub.status = "revoked"
        record = tx.scalar(select(PanelObservation).where(PanelObservation.subscription_id == sid))
        if invalid == "missing":
            tx.delete(record)
        else:
            record.payload = record.payload | {"account_id": "999"}
        tx.flush()
        payload = commerce._provision_payload(tx, sub)
        tx.add(Job(key="ended-history-" + invalid, kind="provision", account_id=301, payload=payload))
        history = (segment.starts_at, segment.ends_at, segment.paid_value, dict(segment.price_basis))
    calls = len(state["calls"])
    assert commerce.provisioning.run_jobs() == 0
    assert len(state["calls"]) == calls
    with db.transaction() as tx:
        job = tx.scalar(select(Job).where(Job.key == "ended-history-" + invalid))
        assert job.state in {"blocked", "review"}
        segment = tx.get(Segment, segment_id)
        assert (segment.starts_at, segment.ends_at, segment.paid_value, segment.price_basis) == history


def test_automatic_refund_disables_actual_verified_current_group_without_restoring_old_rights(
    short_renewal, monkeypatch
):
    context, billing, segment_id, payment_id, starts, order = short_renewal
    db, _, commerce, _, state, sid, _, _, _ = context
    moment = starts + timedelta(seconds=1)
    advance(monkeypatch, moment)
    commerce.apply_segment_boundaries()
    assert commerce.provisioning.run_jobs() == 1
    commerce.dashboard(301, refresh_panel=True)
    with db.transaction() as tx:
        segment, payment = tx.get(Segment, segment_id), tx.get(Payment, payment_id)
        usage = VerifiedEntitlementUsage(
            order_id=order["id"],
            segment_id=segment_id,
            started_at=segment.starts_at,
            observed_at=moment,
            traffic_bytes=0,
            source="verified_panel_attestation",
        )
        provider_id, amount = payment.provider_id, payment.amount
    assert (
        billing.refund(provider_id, "synthetic-disable-worker", amount, moment, usage=usage)[
            "entitlement_action"
        ]
        == "automatic_revocation"
    )
    assert commerce.provisioning.run_jobs() == 1
    assert state["user"]["status"] == "DISABLED"
    assert state["user"]["activeInternalSquads"] == [{"uuid": SQUAD}]
    with db.transaction() as tx:
        assert tx.get(Subscription, sid).status == "revoked"


@pytest.mark.parametrize(
    "rotations,prior_status,pre_rotation,future_block",
    [
        (0, "active", False, None),
        (1, "active", False, None),
        (2, "active", False, None),
        (1, "active", True, None),
        (0, "suspended", False, None),
        (0, "disputed", False, None),
        (0, "revoked", False, None),
        (0, "active", False, "foreign_segment"),
        (0, "active", False, "newer_owner"),
    ],
)
def test_observed_own_refund_disable_does_not_block_genuine_future_paid_boundary(
    short_renewal, monkeypatch, rotations, prior_status, pre_rotation, future_block
):
    context, billing, current_id, payment_id, starts, current_order = short_renewal
    db, _, commerce, _, state, sid, product_id, _, _ = context
    moment = starts + timedelta(seconds=1)
    advance(monkeypatch, moment)
    commerce.apply_segment_boundaries()
    assert commerce.provisioning.run_jobs() == 1
    quote = commerce.quote(301, product_id, 30, "renew", sid)
    future_order = commerce.create_order(301, quote["id"], "genuine-future-after-refund")
    provider = SyntheticProvider()
    billing.prepare_payment(future_order["id"], provider)
    enqueue(commerce, future_order)
    billing.reconcile_inbox(provider)
    assert commerce.provisioning.run_jobs() == 1
    commerce.dashboard(301, refresh_panel=True)
    if pre_rotation:
        CustomerOperations(commerce).rotate_credentials(301, sid, "before-own-refund", confirmed=True)
        assert commerce.provisioning.run_jobs() == 1
        commerce.dashboard(301, refresh_panel=True)
    with db.transaction() as tx:
        tx.get(Subscription, sid).status = prior_status
        current, payment = tx.get(Segment, current_id), tx.get(Payment, payment_id)
        future = tx.scalar(select(Segment).where(Segment.order_id == future_order["id"]))
        future_start, future_end = future.starts_at, future.ends_at
        usage = VerifiedEntitlementUsage(
            order_id=current_order["id"],
            segment_id=current_id,
            started_at=current.starts_at,
            observed_at=moment,
            traffic_bytes=0,
            source="verified_panel_attestation",
        )
        provider_id, amount = payment.provider_id, payment.amount
    billing.refund(provider_id, "synthetic-current-with-future", amount, moment, usage=usage)
    assert commerce.provisioning.run_jobs() == 1
    assert state["user"]["status"] == "DISABLED"
    commerce.dashboard(301, refresh_panel=True)
    for index in range(rotations):
        from sunrise_infrastructure.db import PanelObservation

        with db.transaction() as tx:
            observation = tx.scalar(select(PanelObservation).where(PanelObservation.subscription_id == sid))
            previous = (observation.observed_at, observation.status, dict(observation.payload))
        rotation = CustomerOperations(commerce).rotate_credentials(
            301, sid, "own-gap-rotation-" + str(index), confirmed=True
        )
        assert commerce.provisioning.run_jobs() == 1
        with db.transaction() as tx:
            observation = tx.scalar(select(PanelObservation).where(PanelObservation.subscription_id == sid))
            assert tx.get(Job, rotation["job_id"]).state == "done"
            assert observation.payload["binding"] == tx.get(Setting, "panel_test:" + sid).value["binding"]
            assert (observation.observed_at, observation.status) == previous[:2]
            for field in ("expires_at", "devices_limit", "internal_squad_uuids", "subscription_version"):
                assert observation.payload[field] == previous[2][field]
    if future_block:
        with db.transaction() as tx:
            refund_job = tx.scalar(select(Job).where(Job.key.like("refund-rights:%")))
            if future_block == "foreign_segment":
                refund_job.payload = refund_job.payload | {"refunded_segment_id": "foreign"}
            else:
                tx.get(Subscription, sid).version += 1
                tx.add(
                    Job(
                        key="newer-owner-revocation",
                        kind="provision",
                        account_id=301,
                        payload=refund_job.payload
                        | {
                            "version": tx.get(Subscription, sid).version,
                            "target_origin": "owner",
                            "prior_local_status": "revoked",
                        },
                    )
                )
    advance(monkeypatch, future_start + timedelta(seconds=1))
    commerce.apply_segment_boundaries()
    assert commerce.provisioning.run_jobs() >= 1
    expected_status = "revoked" if future_block else prior_status
    assert state["user"]["status"] == ("ACTIVE" if expected_status == "active" else "DISABLED")
    with db.transaction() as tx:
        assert tx.get(Subscription, sid).status == expected_status
    assert datetime.fromisoformat(state["user"]["expireAt"]) == future_end


def test_paid_order_disabled_delivery_stays_review_after_verified_active(short_renewal, monkeypatch):
    from sunrise_infrastructure.db import Order

    context, billing, _, _, starts, _ = short_renewal
    db, _, commerce, _, state, sid, product_id, _, _ = context
    advance(monkeypatch, starts + timedelta(seconds=1))
    commerce.apply_segment_boundaries()
    assert commerce.provisioning.run_jobs() == 1
    quote = commerce.quote(301, product_id, 30, "renew", sid)
    order = commerce.create_order(301, quote["id"], "paid-manual-disable-readiness")
    provider = SyntheticProvider()
    billing.prepare_payment(order["id"], provider)
    state["user"]["status"] = "DISABLED"
    from sunrise_infrastructure import panel_entitlements

    with db.transaction() as tx:
        sub = tx.get(Subscription, sid)
        snapshot = context[1].snapshot(panel_entitlements.registered_binding(tx, sub))
        panel_entitlements.remember_restriction(
            tx, sub, snapshot, panel_entitlements.registered_binding(tx, sub)
        )
    enqueue(commerce, order)
    billing.reconcile_inbox(provider)
    assert commerce.provisioning.run_jobs() == 1
    with db.transaction() as tx:
        dto = commerce._order_dto(tx.get(Order, order["id"]))
        assert dto["status"] == "paid"
        assert dto["provisioning_status"] == "review"
    state["user"]["status"] = "ACTIVE"
    commerce.dashboard(301, refresh_panel=True)
    with db.transaction() as tx:
        assert commerce._order_dto(tx.get(Order, order["id"]))["provisioning_status"] == "review"


def test_gift_preserves_registered_panel_identity_and_paid_history(short_renewal, monkeypatch):
    context, _, segment_id, _, starts, _ = short_renewal
    db, _, commerce, _, _, sid, product_id, _, _ = context
    advance(monkeypatch, starts + timedelta(seconds=1))
    commerce.apply_segment_boundaries()
    assert commerce.provisioning.run_jobs() == 1
    with db.transaction() as tx:
        sub = tx.get(Subscription, sid)
        identity = (sub.panel_uuid, sub.panel_user_id, sub.short_uuid)
        paid = tx.get(Segment, segment_id)
        expiry = paid.ends_at
        history = (paid.starts_at, paid.ends_at, paid.paid_value, dict(paid.price_basis))
        policy = tx.get(Setting, "discounts")
        policy.value = policy.value | {
            "promos": [
                {
                    "code": "OWNEDGIFTDAYS",
                    "enabled": True,
                    "reward_type": "free_days",
                    "reward_days": 5,
                    "product_id": product_id,
                    "per_account_limit": 1,
                }
            ]
        }
    commerce.activate_promo(301, "OWNEDGIFTDAYS")
    benefit = commerce.claim_promo(301, "OWNEDGIFTDAYS", sid)
    assert datetime.fromisoformat(benefit["expires_at"]) == expiry + timedelta(days=5)
    with db.transaction() as tx:
        sub = tx.get(Subscription, sid)
        assert (sub.panel_uuid, sub.panel_user_id, sub.short_uuid) == identity
        paid = tx.get(Segment, segment_id)
        assert (paid.starts_at, paid.ends_at, paid.paid_value, paid.price_basis) == history
