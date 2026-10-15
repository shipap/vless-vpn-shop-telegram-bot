import os
from uuid import uuid4

import fakeredis
from sunrise_infrastructure.admin import Admin
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import Database, Product
from sunrise_infrastructure.security import RateGuard


def test_public_catalog_presents_editable_periods_and_metadata():
    db = Database(os.environ["SUNRISE_TEST_DATABASE_URL"])
    assert db.engine.url.database == "sunrise_test_backend"
    c = Commerce(db, local_test=True)
    admin = Admin(c, RateGuard(fakeredis.FakeRedis()))
    pid = "v6-" + str(uuid4())
    with db.transaction() as s:
        s.add(
            Product(
                included_devices=4,
                currency="RUB",
                id=pid,
                code=pid,
                name="Временный",
                tier_level=3,
                price="200.00",
            )
        )
    try:
        admin.catalog_update(
            None,
            pid,
            {
                "name": "Новый тариф",
                "visible_landing": False,
                "highlighted": True,
                "sort_order": -10,
                "semantic_role": "extended",
                "base_period_days": 30,
                "periods": [{"days": 90, "months": 3, "discount_percent": "10"}],
                "icon": {"fallback": "☀️", "custom_emoji_id": "1234567890123456789"},
            },
            "Проверка общего каталога",
        )
        product = next(p for p in c.catalog()["products"] if p["id"] == pid)
        assert product["visible_landing"] is False
        assert product["highlighted"] is True
        assert product["periods"][0]["price"] == "540.00"
        assert product["icon"]["custom_emoji_id"] == "1234567890123456789"
        assert "faq" in c.catalog()["content"]
        assert "legal" in c.catalog()
    finally:
        with db.transaction() as s:
            s.delete(s.get(Product, pid))
