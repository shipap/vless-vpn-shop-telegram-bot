import os
from decimal import Decimal as D
from uuid import uuid4

import pytest
from sqlalchemy import select
from sunrise_core.errors import DomainError
from sunrise_infrastructure.billing import Billing, VerifiedPayment
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import Account, Database, Payment, Refund, now

from tests.legal_fixture import accept_test_legal


@pytest.fixture
def billing():
    url = os.getenv("SUNRISE_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Synthetic PostgreSQL required")
    c = Commerce(Database(url), True)
    with c.db.transaction() as s:
        ref = Account(email=f"{uuid4()}@example.test")
        s.add(ref)
        s.flush()
        a = Account(email=f"{uuid4()}@example.test", referrer_id=ref.id)
        s.add(a)
        s.flush()
        aid = a.id
        rid = ref.id
    accept_test_legal(Commerce(c.db), a.id)
    accept_test_legal(Commerce(c.db), ref.id)
    q = c.quote(aid, "standard", 30, "purchase")
    o = c.create_order(aid, q["id"], str(uuid4()))
    return c, Billing(c, "test-terminal", "local_test"), aid, rid, o


def fact(o, amount="99.00"):
    return VerifiedPayment(
        provider="fixture",
        provider_id=str(uuid4()),
        terminal="test-terminal",
        environment="local_test",
        order_id=o["id"],
        amount=D(amount),
        currency="RUB",
        paid_at=now(),
        fee=None,
        net_usdt=D("1.10"),
    )


def test_verified_payment_duplicate_and_refund_reversal_once(billing):
    c, b, aid, rid, o = billing
    p = fact(o)
    b.accept(p)
    b.accept(p)
    assert c.dashboard(rid)["bonus_balance"] == "14.850000"
    b.refund(p.provider_id, "full-refund", D("99.00"), now())
    b.refund(p.provider_id, "full-refund", D("99.00"), now())
    assert c.dashboard(rid)["bonus_balance"] == "0.000000"
    with c.db.transaction() as s:
        payment = s.scalar(select(Payment).where(Payment.provider_id == p.provider_id))
        assert payment.fee is None
        assert payment.net_usdt == D("1.10")
        assert len(list(s.scalars(select(Refund).where(Refund.payment_id == payment.id)))) == 1


def test_amount_or_merchant_mismatch_never_grants_access(billing):
    c, b, aid, rid, o = billing
    with pytest.raises(DomainError):
        b.accept(fact(o, "98.00"))
    assert c.dashboard(aid)["subscriptions"] == []
