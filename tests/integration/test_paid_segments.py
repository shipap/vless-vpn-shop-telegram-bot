import os
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import Account, Database, Segment, now

from tests.legal_fixture import accept_test_legal


def test_tier_change_never_converts_paid_rubles_to_euros():
    from decimal import Decimal

    from sunrise_core.errors import DomainError
    from sunrise_infrastructure.db import Product

    c = Commerce(Database(os.environ["SUNRISE_TEST_DATABASE_URL"]), True)
    with c.db.transaction() as s:
        account = Account(email=str(uuid4()) + "@example.test")
        product = Product(
            included_devices=4,
            code=str(uuid4()),
            name="Проверка другой валюты",
            currency="EUR",
            price=Decimal(1),
            tier_level=99,
        )
        s.add_all([account, product])
        s.flush()
        aid, pid = account.id, product.id
    accept_test_legal(Commerce(c.db), account.id)
    quote = c.quote(aid, "standard", 30, "purchase")
    order = c.create_order(aid, quote["id"], str(uuid4()))
    c.test_pay(aid, order["id"])
    sid = c.dashboard(aid)["subscriptions"][0]["id"]
    with pytest.raises(DomainError) as error:
        c.quote(aid, pid, 30, "upgrade", sid)
    assert error.value.code == "valuation_currency_mismatch"


def test_downgrade_with_paid_device_prices_includes_device_value(monkeypatch):
    url = os.getenv("SUNRISE_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Synthetic PostgreSQL required")
    c = Commerce(Database(url), True)
    moment = now()
    monkeypatch.setattr("sunrise_infrastructure.commerce.now", lambda: moment)
    with c.db.transaction() as s:
        a = Account(email=f"{uuid4()}@example.test")
        s.add(a)
        s.flush()
        aid = a.id
    accept_test_legal(Commerce(c.db), a.id)
    q = c.quote(aid, "universal", 30, "purchase")
    o = c.create_order(aid, q["id"], str(uuid4()))
    c.test_pay(aid, o["id"])
    sid = c.dashboard(aid)["subscriptions"][0]["id"]
    q = c.quote(aid, "universal", 30, "devices", sid, extra_devices=1)
    o = c.create_order(aid, q["id"], str(uuid4()))
    c.test_pay(aid, o["id"])
    moment += timedelta(days=15)
    q = c.quote(aid, "standard", 30, "downgrade", sid)
    from datetime import datetime

    seconds = int((datetime.fromisoformat(q["resulting_expires_at"]) - moment).total_seconds())
    assert seconds == 2600697  # floor(15 * 86400 * 299 / 149)


def test_downgrade_shifts_future_paid_segment_without_erasing_it(monkeypatch):
    url = os.getenv("SUNRISE_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Synthetic PostgreSQL required")
    c = Commerce(Database(url), True)
    moment = now()
    monkeypatch.setattr("sunrise_infrastructure.commerce.now", lambda: moment)
    with c.db.transaction() as s:
        a = Account(email=f"{uuid4()}@example.test")
        s.add(a)
        s.flush()
        aid = a.id
    accept_test_legal(Commerce(c.db), a.id)
    q = c.quote(aid, "universal", 30, "purchase")
    o = c.create_order(aid, q["id"], str(uuid4()))
    c.test_pay(aid, o["id"])
    sid = c.dashboard(aid)["subscriptions"][0]["id"]
    q = c.quote(aid, "standard", 90, "renew", sid)
    o = c.create_order(aid, q["id"], str(uuid4()))
    c.test_pay(aid, o["id"])
    moment += timedelta(days=15)
    q = c.quote(aid, "standard", 30, "downgrade", sid)
    c.create_order(aid, q["id"], str(uuid4()))
    with c.db.transaction() as s:
        segs = list(
            s.scalars(select(Segment).where(Segment.subscription_id == sid).order_by(Segment.starts_at))
        )
        assert len(segs) == 2
        assert segs[1].starts_at == segs[0].ends_at
        assert segs[1].ends_at - segs[1].starts_at == timedelta(days=90)


@pytest.mark.parametrize("days", [90, 180, 365])
def test_long_paid_basis_downgrade_ignores_later_catalog_prices(days, monkeypatch):
    from datetime import datetime
    from decimal import ROUND_FLOOR, Decimal

    from sunrise_infrastructure.db import Product

    c = Commerce(Database(os.environ["SUNRISE_TEST_DATABASE_URL"]), True)
    moment = now()
    monkeypatch.setattr("sunrise_infrastructure.commerce.now", lambda: moment)
    with c.db.transaction() as s:
        account = Account(email=f"{uuid4()}@example.test")
        s.add(account)
        s.flush()
        aid = account.id
    accept_test_legal(Commerce(c.db), account.id)
    quote = c.quote(aid, "universal", days, "purchase")
    order = c.create_order(aid, quote["id"], str(uuid4()))
    c.test_pay(aid, order["id"])
    sid = c.dashboard(aid)["subscriptions"][0]["id"]
    with c.db.transaction() as s:
        product = s.get(Product, "standard")
        original_price, version = product.price, product.version
        product.price, product.version = Decimal(9999), product.version + 1
    try:
        moment += timedelta(days=days / 2)
        downgrade = c.quote(aid, "standard", 30, "downgrade", sid)
        remaining = Decimal(days * 86400 // 2)
        expected = int((remaining * Decimal(249) / Decimal(99)).to_integral_value(rounding=ROUND_FLOOR))
        assert (
            int((datetime.fromisoformat(downgrade["resulting_expires_at"]) - moment).total_seconds())
            == expected
        )
        assert downgrade["total"] == "0.00"
    finally:
        with c.db.transaction() as s:
            product = s.get(Product, "standard")
            product.price, product.version = original_price, version
        c.db.engine.dispose()
