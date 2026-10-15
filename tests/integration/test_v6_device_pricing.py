from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select
from sunrise_core.errors import DomainError
from sunrise_infrastructure.db import Product, Quote, Segment
from test_v6_legal import legal_context as legal_context

RULES = [
    {"through_extra": 5, "price": "50.00"},
    {"through_extra": 9, "price": "100.00"},
    {"through_extra": None, "price": "150.00"},
]


def purchased(c, aid, documents):
    c.accept_legal(aid, documents, "website", "checkout")
    pid = str(uuid4())
    with c.db.transaction() as s:
        s.add(
            Product(
                currency="RUB",
                id=pid,
                code=pid,
                name="Ступени устройств",
                tier_level=1,
                price=99,
                included_devices=4,
                extra_device_price=50,
                presentation={"extra_device_rules": RULES},
            )
        )
    quote = c.quote(aid, pid, 30, "purchase")
    order = c.create_order(aid, quote["id"], str(uuid4()))
    c.test_pay(aid, order["id"])
    return pid, c.order(aid, order["id"])["subscription_id"]


@pytest.mark.parametrize("count,expected", [(5, "250.00"), (6, "300.00"), (9, "450.00"), (10, "500.00")])
def test_device_purchase_and_renewal_use_fixed_configured_unit_price(legal_context, count, expected):
    c, aid, docs = legal_context
    pid, sid = purchased(c, aid, docs)
    quote = c.quote(aid, pid, 30, "devices", sid, extra_devices=count)
    assert Decimal(quote["total"]) == Decimal(expected)
    order = c.create_order(aid, quote["id"], str(uuid4()))
    c.test_pay(aid, order["id"])
    renewal = c.quote(aid, pid, 30, "renew", sid)
    assert Decimal(renewal["total"]) == Decimal(99) + Decimal(expected)
    with c.db.transaction() as s:
        basis = s.scalar(select(Segment).where(Segment.subscription_id == sid)).price_basis
        assert basis["device_terms"][pid]["rules"] == []


def test_new_device_quote_requires_known_paid_basis_and_keeps_previous_snapshot(legal_context):
    c, aid, docs = legal_context
    pid, sid = purchased(c, aid, docs)
    with c.db.transaction() as s:
        segment = s.scalar(select(Segment).where(Segment.subscription_id == sid))
        prior_basis = segment.price_basis
        product = s.get(Product, pid)
        product.presentation = {"extra_device_rules": [{"through_extra": None, "price": "999.00"}]}
    quote = c.quote(aid, pid, 30, "devices", sid, extra_devices=1)
    assert Decimal(quote["total"]) == Decimal("50.00")
    with c.db.transaction() as s:
        assert s.scalar(select(Segment).where(Segment.subscription_id == sid)).price_basis == prior_basis
        frozen = s.get(Quote, quote["id"]).snapshot
        # Original value plus a fixed contribution, normalized to actual remaining time.
        assert Decimal(frozen["basis"]["products"][pid]) >= Decimal("149.00")
        segment = s.scalar(select(Segment).where(Segment.subscription_id == sid))
        segment.price_basis = {k: v for k, v in segment.price_basis.items() if k != "device_terms"}
    with pytest.raises(DomainError) as unknown:
        c.quote(aid, pid, 30, "devices", sid, extra_devices=1)
    assert unknown.value.code == "valuation_unknown"


def test_device_marginal_price_stays_fixed_with_half_the_term_remaining(legal_context):
    from datetime import timedelta

    from sunrise_infrastructure.db import Subscription, now

    c, aid, docs = legal_context
    pid, sid = purchased(c, aid, docs)
    initial = c.quote(aid, pid, 30, "devices", sid, extra_devices=5)
    order = c.create_order(aid, initial["id"], str(uuid4()))
    c.test_pay(aid, order["id"])
    with c.db.transaction() as s:
        segment = s.scalar(select(Segment).where(Segment.subscription_id == sid))
        segment.starts_at = now() - timedelta(days=15)
        segment.ends_at = now() + timedelta(days=15)
        s.get(Subscription, sid).expires_at = segment.ends_at
    marginal = c.quote(aid, pid, 30, "devices", sid, extra_devices=5)
    assert Decimal(marginal["total"]) == Decimal("250.00")


def test_recurring_extra_device_amount_is_fixed_by_separate_bank_consent(legal_context):
    from sunrise_infrastructure.db import Agreement, Setting, now
    from sunrise_infrastructure.recurring import Recurring
    from test_recurring import RecurringProvider

    class Provider(RecurringProvider):
        def plans(self):
            return {
                "items": [
                    {
                        "id": "plan-devices",
                        "payer_amount_rub": "399.00",
                        "merchant_amount_rub": "399.00",
                        "interval": "month",
                    }
                ]
            }

    c, aid, docs = legal_context
    pid, sid = purchased(c, aid, docs)
    quote = c.quote(aid, pid, 30, "devices", sid, extra_devices=6)
    order = c.create_order(aid, quote["id"], str(uuid4()))
    c.test_pay(aid, order["id"])
    with c.db.transaction() as s:
        prior = s.get(Setting, "recurring").value
        s.get(Setting, "recurring").value = {"enabled": True}
    try:
        recurring = Recurring(c, Provider())
        agreement = recurring.enroll(aid, sid, "plan-devices", True, str(uuid4()))
        with c.db.transaction() as s:
            product = s.get(Product, pid)
            product.price = 1000
            product.presentation = {"extra_device_rules": [{"through_extra": None, "price": "999.00"}]}
            stored = s.get(Agreement, agreement["id"])
            assert stored.consent_amount == Decimal("399.00")
            assert stored.consent["basis"]["device_terms"][pid]["rules"] == []
        paid = {
            "payment_id": "cycle-" + str(uuid4()),
            "amount": "399.00",
            "payment_currency": "RUB",
            "status": "paid",
            "subscription_id": agreement["provider_id"],
            "paid_at": now().isoformat(),
            "test": True,
            "terminal_id": "test-recurring",
        }
        charge = {
            "cycle_number": 1,
            "payment_id": paid["payment_id"],
            "status": "payed",
            "validation_status": "accepted",
        }
        recurring.service_cycle(paid, charge)
        with c.db.transaction() as s:
            segments = list(
                s.scalars(select(Segment).where(Segment.subscription_id == sid).order_by(Segment.starts_at))
            )
            assert segments[-1].paid_value == Decimal("399.00")
            assert segments[-1].devices == 10
            assert segments[-1].price_basis["products"][pid] == "399.00"
    finally:
        with c.db.transaction() as s:
            s.get(Setting, "recurring").value = prior
