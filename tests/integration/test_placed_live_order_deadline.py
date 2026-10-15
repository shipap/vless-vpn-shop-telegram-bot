"""A placed immutable live order uses authenticated invoice time, not unused quote TTL."""

import json
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select
from sunrise_core.errors import DomainError
from sunrise_infrastructure.billing import Billing
from sunrise_infrastructure.db import (
    Inbox,
    Order,
    Payment,
    ProviderOperation,
    Quote,
    Segment,
    Setting,
    Subscription,
    now,
)
from sunrise_infrastructure.providers import RollyPay

from tests.integration.test_legacy_promo_entitlements import legacy_business as legacy_business
from tests.integration.test_production_business_races import live_business as live_business


@pytest.mark.parametrize(
    "paid_minutes,received_minutes,fulfilled,recover,expiry_minutes",
    [
        (15, 15, True, False, 30),
        (20, 20, True, False, 30),
        (30, 30, True, False, 30),
        (15, 45, True, False, 30),
        (31, 31, False, False, 30),
        (15, 45, True, True, 30),
        (2, 2, False, False, 1),
    ],
)
def test_authenticated_live_invoice_window_survives_unused_quote_ttl(
    legacy_business, monkeypatch, paid_minutes, received_minutes, fulfilled, recover, expiry_minutes
):
    import sunrise_infrastructure.billing as billing_module
    import sunrise_infrastructure.commerce as commerce_module

    db, commerce, aid, sid = legacy_business
    quote = commerce.quote(aid, "standard", 30, "renew", sid, payment_method="sbp")
    order = commerce.create_order(aid, quote["id"], "placed-window")
    commerce.runtime_policy.scope.rollypay.origin = "https://bank.synthetic.test"
    commerce.runtime_policy.scope.rollypay.terminal = "synthetic-terminal"
    created = now()
    expiry = created + timedelta(minutes=expiry_minutes)
    requests = []

    def transport(request):
        requests.append(request.method)
        body = json.loads(request.content) if request.method == "POST" else {}
        return httpx.Response(
            200,
            json={
                "environment": "prod",
                "payment_id": "deadline-payment",
                "order_id": body.get("order_id", order["id"]),
                "amount": body.get("amount", order["total"]),
                "payment_currency": "RUB",
                "payment_method": "sbp",
                "pay_url": "https://checkout.synthetic.test/own",
                "created_at": created.isoformat(),
                "expires_at": expiry.isoformat(),
                "paid_at": (created + timedelta(minutes=paid_minutes)).isoformat(),
                "status": "paid",
            },
        )

    provider = RollyPay(
        "https://bank.synthetic.test",
        "synthetic-key",
        "synthetic-terminal",
        {"bank.synthetic.test"},
        transport=httpx.MockTransport(transport),
        environment="live",
        runtime_policy=commerce.runtime_policy,
    )
    billing = Billing(commerce, "synthetic-terminal", "live")
    assert billing.prepare_payment(order["id"], provider)["state"] == "created"
    with db.transaction() as session:
        operation = session.scalar(select(ProviderOperation).where(ProviderOperation.order_id == order["id"]))
        # The merchant-created window is durable, not supplied by a callback/client.
        assert operation.request["live_creation"].get("expires_at") == expiry.isoformat()
        receipt = Inbox(
            event_key="placed-window",
            state="pending",
            payload={"order_id": order["id"], "payment_id": operation.provider_id},
        )
        session.add(receipt)
        session.flush()
        iid = receipt.id
    clock = created + timedelta(minutes=received_minutes)
    monkeypatch.setattr(commerce_module, "now", lambda: clock)
    monkeypatch.setattr(billing_module, "now", lambda: clock)
    # Existing immutable checkout remains reachable during its own merchant window.
    before = commerce.order(aid, order["id"])
    assert (before["checkout_url"] is not None) is (received_minutes <= expiry_minutes)
    assert before["status"] == "awaiting_payment"
    if recover:
        with db.transaction() as session:
            marker = session.get(Setting, "legacy_promo_usage")
            saved_marker = marker.value
            marker.value = saved_marker | {"pending_grants": {}}
    assert billing.reconcile_inbox(provider) == 1
    if recover:
        with db.transaction() as session:
            assert session.get(Order, order["id"]).fulfilled_at is None
            assert session.get(Inbox, iid).state == "pending"
            prior_id = session.scalar(select(Payment.id).where(Payment.order_id == order["id"]))
            assert prior_id is not None
            clock = session.get(Inbox, iid).available_at
            session.get(Setting, "legacy_promo_usage").value = saved_marker
        assert billing.reconcile_inbox(provider) == 1
    with db.transaction() as session:
        saved = session.get(Order, order["id"])
        assert (saved.fulfilled_at is not None) is fulfilled
        assert (
            session.scalar(select(Segment).where(Segment.order_id == order["id"])) is not None
        ) is fulfilled
        assert session.scalar(select(Payment).where(Payment.order_id == order["id"])) is not None
        assert (session.get(Quote, quote["id"]).expires_at < clock) is (received_minutes > 10)
        assert session.get(Inbox, iid).state == "processed"
        if recover:
            assert session.scalar(select(Payment.id).where(Payment.order_id == order["id"])) == prior_id
    assert billing.reconcile_inbox(provider) == 0
    assert requests == (["POST", "GET", "GET"] if recover else ["POST", "GET"])


def test_expired_unused_quote_still_cannot_create_order(legacy_business, monkeypatch):
    import sunrise_infrastructure.commerce as commerce_module

    db, commerce, aid, sid = legacy_business
    quote = commerce.quote(aid, "standard", 30, "renew", sid, payment_method="sbp")
    with db.transaction() as session:
        clock = session.get(Quote, quote["id"]).expires_at + timedelta(seconds=1)
    monkeypatch.setattr(commerce_module, "now", lambda: clock)
    with pytest.raises(DomainError) as denied:
        commerce.create_order(aid, quote["id"], "expired-unused")
    assert denied.value.code == "quote_expired"


@pytest.mark.parametrize("change", ["canceled", "stale_version"])
def test_placed_invoice_deadline_does_not_override_order_or_subscription_fences(
    legacy_business, monkeypatch, change
):
    db, commerce, aid, sid = legacy_business
    quote = commerce.quote(aid, "standard", 30, "renew", sid, payment_method="sbp")
    order = commerce.create_order(aid, quote["id"], "placed-fence")
    with db.transaction() as session:
        if change == "canceled":
            session.get(Order, order["id"]).status = "canceled"
        else:
            session.get(Subscription, sid).version += 1
    from tests.integration.test_legacy_promo_entitlements import own_fact

    Billing(commerce, "synthetic-terminal", "live").accept(own_fact(order))
    with db.transaction() as session:
        assert session.get(Order, order["id"]).fulfilled_at is None
        assert session.scalar(select(Segment).where(Segment.order_id == order["id"])) is None


@pytest.mark.parametrize(
    "change",
    [
        "foreign_scope",
        "owner",
        "method",
        "deadline_conflict",
        "created_bool",
        "expiry_missing",
        "paid_unknown",
        "paid_bool",
        "paid_null",
        "paid_number",
        "missing_window",
    ],
)
def test_placed_window_requires_current_own_operation_and_authoritative_dates(
    legacy_business, monkeypatch, change
):
    import sunrise_infrastructure.billing as billing_module
    import sunrise_infrastructure.commerce as commerce_module

    db, commerce, aid, sid = legacy_business
    quote = commerce.quote(aid, "standard", 30, "renew", sid, payment_method="sbp")
    order = commerce.create_order(aid, quote["id"], "own-window-negative")
    commerce.runtime_policy.scope.rollypay.origin = "https://bank.synthetic.test"
    commerce.runtime_policy.scope.rollypay.terminal = "synthetic-terminal"
    created = now()
    response = {
        "environment": "prod",
        "payment_id": "negative-deadline-payment",
        "order_id": order["id"],
        "amount": order["total"],
        "payment_currency": "RUB",
        "payment_method": "sbp",
        "pay_url": "https://checkout.synthetic.test/own",
        "created_at": created.isoformat(),
        "expires_at": (created + timedelta(minutes=30)).isoformat(),
        "paid_at": (created + timedelta(minutes=15)).isoformat(),
        "status": "paid",
    }
    if change == "missing_window":
        response.pop("created_at")
        response.pop("expires_at")
    provider = RollyPay(
        "https://bank.synthetic.test",
        "synthetic-key",
        "synthetic-terminal",
        {"bank.synthetic.test"},
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=dict(response))),
        environment="live",
        runtime_policy=commerce.runtime_policy,
    )
    billing = Billing(commerce, "synthetic-terminal", "live")
    assert billing.prepare_payment(order["id"], provider)["state"] == "created"
    with db.transaction() as session:
        operation = session.scalar(select(ProviderOperation).where(ProviderOperation.order_id == order["id"]))
        if change in {"foreign_scope", "owner", "method"}:
            key, value = {
                "foreign_scope": ("scope_id", "foreign"),
                "owner": ("account_id", "9007199254740997"),
                "method": ("payment_method", "crypto"),
            }[change]
            operation.request = operation.request | {key: value}
        receipt = Inbox(
            event_key="negative-placed-window",
            state="pending",
            payload={"order_id": order["id"], "payment_id": operation.provider_id},
        )
        session.add(receipt)
        session.flush()
        iid = receipt.id
    if change == "deadline_conflict":
        response["expires_at"] = (created + timedelta(minutes=31)).isoformat()
    elif change == "created_bool":
        response["created_at"] = True
    elif change == "expiry_missing":
        response.pop("expires_at")
    elif change == "paid_unknown":
        response.pop("paid_at")
    elif change in {"paid_bool", "paid_null", "paid_number"}:
        response["paid_at"] = {"paid_bool": True, "paid_null": None, "paid_number": 1}[change]
    clock = created + timedelta(minutes=15)
    monkeypatch.setattr(commerce_module, "now", lambda: clock)
    monkeypatch.setattr(billing_module, "now", lambda: clock)
    billing.reconcile_inbox(provider)
    with db.transaction() as session:
        assert session.get(Order, order["id"]).fulfilled_at is None
        assert session.scalar(select(Segment).where(Segment.order_id == order["id"])) is None
        assert (session.scalar(select(Payment).where(Payment.order_id == order["id"])) is not None) is (
            change == "missing_window"
        )
        assert session.get(Inbox, iid).state == ("processed" if change == "missing_window" else "review")
