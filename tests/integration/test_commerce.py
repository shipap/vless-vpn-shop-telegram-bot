import os
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal as D
from uuid import uuid4

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema, DropSchema
from sunrise_core.errors import DomainError
from sunrise_infrastructure.commerce import Commerce, bootstrap
from sunrise_infrastructure.db import Account, Agreement, Base, Database, Setting

from tests.legal_fixture import accept_test_legal


@pytest.fixture
def commerce():
    url = os.getenv("SUNRISE_TEST_DATABASE_URL")
    if not url:
        pytest.skip("SUNRISE_TEST_DATABASE_URL required; synthetic database only")
    parsed = make_url(url)
    assert parsed.database == "sunrise_test_backend" and parsed.host in {"127.0.0.1", "localhost"}
    schema = "commerce_test_" + uuid4().hex
    base = Database(url)
    with base.engine.begin() as connection:
        connection.execute(CreateSchema(schema))
    scoped = parsed.set(query={**parsed.query, "options": "-csearch_path=" + schema})
    db = Database(scoped.render_as_string(hide_password=False))
    try:
        # These worker checks must never process an unrelated test's accumulated jobs.
        Base.metadata.create_all(db.engine)
        bootstrap(db)
        with db.transaction() as s:
            a = Account(email=f"{uuid4()}@example.test", email_verified=True, bonus_balance=D("100.00"))
            s.add(a)
            s.flush()
            aid = a.id
        commerce = Commerce(db, local_test=True)
        accept_test_legal(commerce, aid)
        yield commerce, aid
    finally:
        db.engine.dispose()
        assert schema.startswith("commerce_test_") and len(schema) == 46
        with base.engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        base.engine.dispose()


def quote(c, aid, **kw):
    product = c.catalog()["products"][0]
    return c.quote(aid, product_id=product["id"], period_days=30, action="purchase", **kw)


def test_duplicate_payment_provisions_exactly_one_segment(commerce):
    c, aid = commerce
    q = quote(c, aid)
    o = c.create_order(aid, q["id"], "first")
    c.test_pay(aid, o["id"])
    c.test_pay(aid, o["id"])
    d = c.dashboard(aid)
    assert len(d["payments"]) == 1
    assert len(d["subscriptions"]) == 1
    assert len(d["subscriptions"][0]["segments"]) == 1
    assert d["subscriptions"][0]["status"] == "provisioning_pending"
    c.run_jobs(limit=1000)
    c.run_jobs(limit=1000)
    assert c.dashboard(aid)["subscriptions"][0]["status"] == "active"


def test_concurrent_orders_cannot_reserve_same_bonus_twice(commerce):
    c, aid = commerce
    qs = [quote(c, aid, bonus_amount="49.50") for _ in range(3)]

    def create(i):
        try:
            return c.create_order(aid, qs[i]["id"], f"parallel-{i}")["id"]
        except DomainError:
            return None

    with ThreadPoolExecutor(max_workers=3) as pool:
        ids = list(pool.map(create, range(3)))
    assert len([x for x in ids if x]) == 2


def test_off_preserves_agreement_and_cancel_available(commerce):
    c, aid = commerce
    o = c.create_order(aid, quote(c, aid)["id"], "agreement")
    c.test_pay(aid, o["id"])
    sid = c.dashboard(aid)["subscriptions"][0]["id"]
    with c.db.transaction() as s:
        s.get(Setting, "recurring").value = {"enabled": False}
        s.add(
            Agreement(
                account_id=aid,
                subscription_id=sid,
                provider_id="synthetic-" + sid,
                state="enabled",
                consent_amount=D(99),
                currency="RUB",
            )
        )
    with pytest.raises(DomainError) as exc:
        c.enroll(aid, sid)
    assert exc.value.code == "recurring_disabled"
    assert c.cancel_agreement(aid, sid)["state"] == "stop_pending"


def test_stale_paid_quote_keeps_cash_for_reconciliation(commerce):
    c, aid = commerce
    o = c.create_order(aid, quote(c, aid)["id"], "initial")
    c.test_pay(aid, o["id"])
    sid = c.dashboard(aid)["subscriptions"][0]["id"]
    p = c.catalog()["products"][0]["id"]
    q1 = c.quote(aid, p, 30, "renew", sid)
    q2 = c.quote(aid, p, 30, "renew", sid)
    o1 = c.create_order(aid, q1["id"], "r1")
    o2 = c.create_order(aid, q2["id"], "r2")
    c.test_pay(aid, o1["id"])
    c.test_pay(aid, o2["id"])
    assert c.order(aid, o2["id"])["status"] == "reconciliation_required"
    assert len(c.dashboard(aid)["payments"]) == 3


def test_foreign_subscription_cannot_be_quoted(commerce):
    c, aid = commerce
    with pytest.raises(DomainError):
        c.quote(aid, c.catalog()["products"][0]["id"], 30, "renew", "not-owned")


def test_test_cash_excluded_from_actual_analytics(commerce):
    c, aid = commerce
    o = c.create_order(aid, quote(c, aid)["id"], "stats")
    c.test_pay(aid, o["id"])
    assert c.analytics()["currencies"] == {}
