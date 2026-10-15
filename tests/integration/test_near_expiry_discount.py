"""Renewal eligibility uses effective rights, never nominal history or client time."""

from datetime import timedelta
from decimal import Decimal

import pytest
from sunrise_infrastructure.db import Segment, Setting, Subscription, now

from tests.integration.test_panel_28_workflows import panel28_workflow as panel28_workflow
from tests.integration.test_panel_entitlement_freshness import entitlement as entitlement


@pytest.mark.parametrize(
    "remaining,eligible",
    [
        (timedelta(hours=24, microseconds=1), False),
        (timedelta(hours=24), False),
        (timedelta(hours=24, microseconds=-1), True),
        (timedelta(0), True),
        (timedelta(days=-1), True),
    ],
)
def test_near_expiry_renewal_strict_boundary(entitlement, monkeypatch, remaining, eligible):
    db, _, commerce, _, state, sid, pid, segment_id, original_end = entitlement
    moment = now()
    monkeypatch.setattr("sunrise_infrastructure.commerce.now", lambda: moment)
    state["user"]["expireAt"] = (moment + remaining).isoformat()
    with db.transaction() as session:
        session.get(Setting, "discounts").value = {
            "cap": "100",
            "bonus_cap": "0",
            "secondary": [],
            "near_expiry_renewal": {"enabled": True, "percent": "10"},
        }
    quote = commerce.quote(301, pid, 30, "renew", sid)
    assert Decimal(quote["total"]) == Decimal("33.30" if eligible else "37.00")
    assert (quote["selected_secondary"]["source"] == "near_expiry_renewal") is eligible
    with db.transaction() as session:
        segment = session.get(Segment, segment_id)
        assert segment.ends_at == original_end
        assert segment.paid_value == Decimal("37.00")


def test_untouched_future_paid_horizon_excludes_near_expiry_discount(entitlement):
    db, _, commerce, _, state, sid, pid, _, _ = entitlement
    moment = now()
    state["user"]["expireAt"] = moment.isoformat()
    future_start, future_end = moment + timedelta(days=2), moment + timedelta(days=32)
    with db.transaction() as session:
        session.get(Setting, "discounts").value = {
            "cap": "100",
            "bonus_cap": "0",
            "secondary": [],
            "near_expiry_renewal": {"enabled": True, "percent": "10"},
        }
        future = Segment(
            subscription_id=sid,
            product_id=pid,
            starts_at=future_start,
            ends_at=future_end,
            devices=4,
            paid_value=Decimal("37.00"),
            price_basis={"currency": "RUB"},
        )
        session.add(future)
        session.flush()
        future_id = future.id
    quote = commerce.quote(301, pid, 30, "renew", sid)
    assert Decimal(quote["total"]) == Decimal("37.00")
    with db.transaction() as session:
        future = session.get(Segment, future_id)
        assert (future.starts_at, future.ends_at, future.paid_value) == (
            future_start,
            future_end,
            Decimal("37.00"),
        )
        assert session.get(Subscription, sid).account_id == 301


@pytest.mark.parametrize("status", ["suspended", "revoked", "unknown"])
def test_strict_local_status_cannot_receive_near_expiry_discount(entitlement, status):
    db, _, commerce, _, state, sid, pid, *_ = entitlement
    state["user"]["expireAt"] = (now() + timedelta(hours=1)).isoformat()
    with db.transaction() as session:
        session.get(Setting, "discounts").value = {
            "cap": "100",
            "bonus_cap": "0",
            "secondary": [],
            "near_expiry_renewal": {"enabled": True, "percent": "10"},
        }
        session.get(Subscription, sid).status = status
    quote = commerce.quote(301, pid, 30, "renew", sid)
    assert Decimal(quote["total"]) == Decimal("37.00")
    assert quote["selected_secondary"]["source"] != "near_expiry_renewal"


def test_unbound_expiry_without_paid_proof_cannot_create_discount(entitlement):
    from sunrise_infrastructure import panel_entitlements

    db = entitlement[0]
    with db.transaction() as session:
        unproved = Subscription(account_id=301, devices_limit=4, expires_at=now() + timedelta(hours=1))
        session.add(unproved)
        session.flush()
        assert panel_entitlements.verified_service_horizon(session, unproved, now()) is None


def test_remote_expired_status_receives_manual_renewal_discount(entitlement):
    db, _, commerce, _, state, sid, pid, *_ = entitlement
    state["user"]["status"] = "EXPIRED"
    state["user"]["expireAt"] = (now() - timedelta(days=1)).isoformat()
    with db.transaction() as session:
        session.get(Setting, "discounts").value = {
            "cap": "100",
            "bonus_cap": "0",
            "near_expiry_renewal": {"enabled": True, "percent": "10"},
            "secondary": [],
        }
    quote = commerce.quote(301, pid, 30, "renew", sid)
    assert quote["total"] == "33.30"
    assert quote["selected_secondary"]["source"] == "near_expiry_renewal"


@pytest.mark.parametrize("restricted", [False, True])
def test_customer_discount_projection_obeys_real_renewal_authority(entitlement, restricted):
    from sunrise_infrastructure.db import Agreement

    db, _, commerce, _, state, sid, *_ = entitlement
    state["user"]["expireAt"] = (now() + timedelta(hours=1)).isoformat()
    with db.transaction() as session:
        session.get(Setting, "discounts").value = {
            "cap": "100",
            "bonus_cap": "0",
            "near_expiry_renewal": {"enabled": True, "percent": "10"},
        }
        if restricted:
            session.add(
                Agreement(
                    account_id=301,
                    subscription_id=sid,
                    state="stop_pending",
                    consent_amount="37.00",
                    currency="RUB",
                )
            )
    projection = commerce.dashboard(301, refresh_panel=True)["subscriptions"][0]
    assert projection["manual_renewal_discount_eligible"] is (not restricted)
    assert projection["manual_renewal_discount_percent"] == (None if restricted else "10")


@pytest.mark.parametrize(
    "campaign,expected,source", [("0", "81.00", "near_expiry_renewal"), ("20", "72.00", "renewal_campaign")]
)
def test_near_discount_uses_common_best_candidate_and_immutable_quote(
    entitlement, campaign, expected, source
):
    from sunrise_infrastructure.db import Product, Quote

    db, _, commerce, _, state, sid, pid, *_ = entitlement
    state["user"]["expireAt"] = (now() + timedelta(hours=1)).isoformat()
    with db.transaction() as session:
        session.get(Product, pid).price = Decimal("100.00")
        session.get(Setting, "periods").value = [{"days": 30, "months": 1, "discount_percent": "10"}]
        session.get(Setting, "discounts").value = {
            "cap": "100",
            "bonus_cap": "0",
            "secondary": [],
            "renewal_campaign": {"enabled": True, "percent": campaign},
            "near_expiry_renewal": {"enabled": True, "percent": "10"},
        }
    quote = commerce.quote(301, pid, 30, "renew", sid)
    assert quote["total"] == expected and quote["selected_secondary"]["source"] == source
    with db.transaction() as session:
        snapshot = dict(session.get(Quote, quote["id"]).snapshot)
        assert snapshot["near_expiry_renewal_evidence"]["percent"] == "10"
        session.get(Setting, "discounts").value = {"cap": "0", "bonus_cap": "0", "secondary": []}
    order = commerce.create_order(301, quote["id"], "immutable-near-discount")
    assert order["total"] == expected
    assert commerce.create_order(301, quote["id"], "immutable-near-discount")["id"] == order["id"]
    with db.transaction() as session:
        assert session.get(Quote, quote["id"]).snapshot == snapshot
