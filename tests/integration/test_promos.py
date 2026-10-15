import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sunrise_core.errors import DomainError
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import Account, Database, Setting

from tests.legal_fixture import accept_test_legal


def test_promo_usage_is_reserved_once_and_audience_enforced():
    url = os.getenv("SUNRISE_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Synthetic PostgreSQL required")
    c = Commerce(Database(url), True)
    code = "TEST" + uuid4().hex[:10].upper()
    with c.db.transaction() as s:
        a = Account(email=f"{uuid4()}@example.test")
        s.add(a)
        s.flush()
        aid = a.id
        row = s.get(Setting, "discounts")
        old = row.value
        row.value = old | {
            "promos": old.get("promos", [])
            + [
                {
                    "code": code,
                    "percent": "20",
                    "enabled": True,
                    "audience": "first_purchase",
                    "per_account_limit": 1,
                }
            ]
        }
    accept_test_legal(Commerce(c.db), a.id)
    quotes = [c.quote(aid, "standard", 30, "purchase", promo_code=code) for _ in range(2)]
    assert quotes[0]["total"] == "79.20"

    def order(i):
        try:
            return c.create_order(aid, quotes[i]["id"], str(uuid4()))
        except DomainError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        orders = list(pool.map(order, range(2)))
    assert sum(x is not None for x in orders) == 1
    paid = next(x for x in orders if x)
    c.test_pay(aid, paid["id"])
    with pytest.raises(DomainError):
        c.quote(aid, "standard", 30, "purchase", promo_code=code)
