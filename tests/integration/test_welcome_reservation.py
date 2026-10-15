import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from sunrise_core.errors import DomainError
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import Account, Database, Order, Setting

from tests.legal_fixture import accept_test_legal


def test_first_welcome_is_reserved_once_and_canceled_reservation_releases():
    db = Database(os.environ["SUNRISE_TEST_DATABASE_URL"])
    assert db.engine.url.database == "sunrise_test_backend"
    with db.transaction() as s:
        policy = s.get(Setting, "discounts")
        old = policy.value
        policy.value = old | {"secondary": [], "referral_welcome": {"enabled": True, "percent": "20"}}
        ref = Account(email=f"{uuid4()}@example.test")
        s.add(ref)
        s.flush()
        a = Account(email=f"{uuid4()}@example.test", referrer_id=ref.id)
        s.add(a)
        s.flush()
        aid = a.id
    accept_test_legal(Commerce(db), a.id)
    accept_test_legal(Commerce(db), ref.id)
    try:
        c = Commerce(db, local_test=True)
        pid = c.catalog()["products"][0]["id"]
        qs = [c.quote(aid, pid, 30, "purchase") for _ in range(2)]
        assert all(q["selected_secondary"]["source"] == "referral_welcome" for q in qs)

        def create(q):
            try:
                return c.create_order(aid, q["id"], str(uuid4()))
            except DomainError as exc:
                assert exc.code == "quote_stale"
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            orders = list(pool.map(create, qs))
        accepted = [o for o in orders if o]
        assert len(accepted) == 1, "First-welcome reservation must serialize per canonical payer"
        with db.transaction() as s:
            s.get(Order, accepted[0]["id"]).status = "canceled"
        unused = next(q for q in qs if q["id"] != accepted[0]["quote_id"])
        second = c.create_order(aid, unused["id"], str(uuid4()))
        c.test_pay(aid, second["id"])
        fresh = c.quote(aid, pid, 30, "purchase")
        assert (
            fresh["selected_secondary"] is None or fresh["selected_secondary"]["source"] != "referral_welcome"
        )
    finally:
        with db.transaction() as s:
            s.get(Setting, "discounts").value = old
        db.engine.dispose()


def test_cash_only_first_purchase_cannot_reuse_welcome_discount():
    from decimal import Decimal

    db = Database(os.environ["SUNRISE_TEST_DATABASE_URL"])
    with db.transaction() as s:
        setting = s.get(Setting, "discounts")
        previous = setting.value
        setting.value = previous | {"secondary": [], "referral_welcome": {"enabled": True, "percent": "20"}}
        ref = Account(email=f"{uuid4()}@example.test")
        s.add(ref)
        s.flush()
        payer = Account(
            email=f"{uuid4()}@example.test",
            referrer_id=ref.id,
            credit_balance=Decimal(500),
            credit_currency="RUB",
            credit_provenance="owner_adjustment",
        )
        s.add(payer)
        s.flush()
        aid = payer.id
    accept_test_legal(Commerce(db), payer.id)
    accept_test_legal(Commerce(db), ref.id)
    try:
        commerce = Commerce(db, local_test=True)
        quote = commerce.quote(aid, "standard", 30, "purchase", credit_amount="500")
        assert quote["selected_secondary"]["source"] == "referral_welcome"
        commerce.create_order(aid, quote["id"], str(uuid4()))
        next_quote = commerce.quote(aid, "standard", 30, "purchase")
        assert not any(x["source"] == "referral_welcome" for x in next_quote["discount_candidates"])
    finally:
        with db.transaction() as s:
            s.get(Setting, "discounts").value = previous
        db.engine.dispose()
