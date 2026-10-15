"""Админский каталог сохраняет наследование и неизменяемые оплаченные факты."""

from copy import deepcopy
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import event, select
from sunrise_core.errors import DomainError
from sunrise_infrastructure.admin import Admin
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import Account, Audit, Product, Quote, Segment, Setting, Subscription, now

from tests.isolated_database import isolated_database


@pytest.fixture
def catalog_context():
    with isolated_database("catalog_patch") as db:
        with db.transaction() as session:
            session.get(Setting, "periods").value = [
                {"days": 31, "months": 1, "discount_percent": "0"},
                {"days": 93, "months": 3, "discount_percent": "7"},
            ]
            product = Product(
                id="inherited-product",
                code="inherited",
                name="Наследуемый тариф",
                tier_level=5,
                price=Decimal("300.00"),
                currency="EUR",
                included_devices=9,
                extra_device_price=Decimal("17.25"),
                presentation={"highlighted": True, "recurring_plan_mapping": {"31": "synthetic-plan"}},
            )
            account = Account(email="catalog-integrity@example.test")
            session.add_all([product, account])
            session.flush()
            end = now() + timedelta(days=31)
            subscription = Subscription(
                id="paid-subscription",
                account_id=account.id,
                product_id=product.id,
                devices_limit=11,
                status="active",
                expires_at=end,
                panel_uuid="synthetic-paid-identity",
                version=7,
            )
            session.add(subscription)
            session.flush()
            snapshot = {
                "product_id": product.id,
                "total": "351.75",
                "currency": "EUR",
                "extra_device_price": "17.25",
                "period_days": 31,
                "legal": {"revision": "synthetic-accepted-revision"},
            }
            session.add_all(
                [
                    Quote(id="saved-quote", account_id=account.id, snapshot=snapshot, expires_at=end),
                    Segment(
                        id="paid-segment",
                        subscription_id=subscription.id,
                        product_id=product.id,
                        starts_at=now(),
                        ends_at=end,
                        devices=11,
                        paid_value=Decimal("351.75"),
                        price_basis=snapshot,
                    ),
                ]
            )
        commerce = Commerce(db, local_test=True)
        yield db, commerce, Admin(commerce, None)


def paid_facts(db):
    with db.transaction() as session:
        subscription = session.get(Subscription, "paid-subscription")
        segment = session.get(Segment, "paid-segment")
        quote = session.get(Quote, "saved-quote")
        return deepcopy(
            {
                "subscription": (
                    subscription.expires_at,
                    subscription.devices_limit,
                    subscription.panel_uuid,
                    subscription.status,
                    subscription.version,
                ),
                "segment": (
                    segment.starts_at,
                    segment.ends_at,
                    segment.devices,
                    segment.paid_value,
                    segment.price_basis,
                ),
                "quote": (quote.snapshot, quote.expires_at),
            }
        )


def test_v7_catalog_patch_list_matches_public_effective_periods_and_exposes_raw_overrides(catalog_context):
    db, commerce, admin = catalog_context
    public = next(item for item in commerce.catalog()["products"] if item["id"] == "inherited-product")
    row = next(item for item in admin.items("catalog") if item["id"] == "inherited-product")
    assert row["base_period_days"] == public["base_period_days"] == 31
    assert (
        row["periods"]
        == public["periods"]
        == [
            {"days": 31, "months": 1, "discount_percent": "0", "price": "300.00"},
            {"days": 93, "months": 3, "discount_percent": "7", "price": "837.00"},
        ]
    )
    assert row["presentation_overrides"] == {
        "highlighted": True,
        "recurring_plan_mapping": {"31": "synthetic-plan"},
    }
    with db.transaction() as session:
        assert "periods" not in session.get(Product, "inherited-product").presentation


def test_v7_catalog_patch_name_only_preserves_inheritance_paid_facts_and_coherent_audit(catalog_context):
    db, _, admin = catalog_context
    original_facts = paid_facts(db)
    with db.transaction() as session:
        original_presentation = deepcopy(session.get(Product, "inherited-product").presentation)
    result = admin.catalog_update(None, "inherited-product", {"name": "Новое название"}, "Изменение названия")
    assert result["base_period_days"] == 31
    assert result["presentation_overrides"] == original_presentation
    with db.transaction() as session:
        product = session.get(Product, "inherited-product")
        assert product.presentation == original_presentation
        assert (product.price, product.extra_device_price) == (Decimal("300.00"), Decimal("17.25"))
        audit = session.scalar(
            select(Audit).where(Audit.action == "catalog_update", Audit.target == product.id)
        )
        assert audit.details["before"]["base_period_days"] == 31
        assert audit.details["after"]["base_period_days"] == 31
        assert audit.details["before"]["periods"] == audit.details["after"]["periods"]
    assert paid_facts(db) == original_facts


def test_v7_catalog_patch_impact_clear_is_explicit_and_readonly(catalog_context):
    db, _, admin = catalog_context
    original_facts = paid_facts(db)
    impact = admin.catalog_impact("inherited-product", {"extra_device_price": None})
    assert impact["after"]["extra_device_price"] is None
    assert impact["before"]["extra_device_price"] == "17.25"
    assert impact["before"]["base_period_days"] == impact["after"]["base_period_days"] == 31
    assert impact["before"]["presentation_overrides"] == impact["after"]["presentation_overrides"]
    assert admin.catalog_impact("inherited-product", {})["after"]["extra_device_price"] == "17.25"
    with db.transaction() as session:
        assert session.get(Product, "inherited-product").extra_device_price == Decimal("17.25")
        assert session.scalar(select(Audit.id)) is None
    assert paid_facts(db) == original_facts


@pytest.mark.parametrize(("values", "expected"), [({}, "17.25"), ({"extra_device_price": None}, None)])
def test_v7_catalog_patch_extra_price_clear_versus_omitted_preserves_paid_snapshots(
    catalog_context, values, expected
):
    db, _, admin = catalog_context
    original_facts = paid_facts(db)
    result = admin.catalog_update(None, "inherited-product", values, "Настройка доплаты")
    assert result["extra_device_price"] == expected
    with db.transaction() as session:
        product = session.get(Product, "inherited-product")
        assert product.extra_device_price == (Decimal(expected) if expected is not None else None)
        assert product.price == Decimal("300.00")
        audit = session.scalar(select(Audit).where(Audit.action == "catalog_update"))
        assert audit.details["before"]["extra_device_price"] == "17.25"
        assert audit.details["after"]["extra_device_price"] == expected
    assert paid_facts(db) == original_facts


@pytest.mark.parametrize("price", ["NaN", "Infinity"])
def test_v7_catalog_patch_impact_rejects_nonfinite_price_before_projection(catalog_context, price):
    db, _, admin = catalog_context
    with pytest.raises(DomainError) as failure:
        admin.catalog_impact("inherited-product", {"price": price})
    assert failure.value.code == "price_invalid"
    with db.transaction() as session:
        assert session.get(Product, "inherited-product").price == Decimal("300.00")


def test_v7_catalog_patch_impact_recomputes_effective_period_price_without_writes(catalog_context):
    db, _, admin = catalog_context
    impact = admin.catalog_impact(
        "inherited-product",
        {"price": "600.00", "periods": [{"days": 47, "months": 1, "discount_percent": "5"}]},
    )
    assert impact["before"]["base_period_days"] == 31
    assert impact["after"]["base_period_days"] == 47
    assert impact["after"]["periods"] == [
        {"days": 47, "months": 1, "discount_percent": "5", "price": "570.00"}
    ]
    with db.transaction() as session:
        product = session.get(Product, "inherited-product")
        assert "periods" not in product.presentation
        assert product.price == Decimal("300.00")


def test_v7_catalog_patch_list_loads_global_periods_once_for_multiple_products(catalog_context):
    db, _, admin = catalog_context
    with db.transaction() as session:
        for index in range(3):
            session.add(
                Product(
                    code=f"batch_{index}",
                    name="Тариф",
                    tier_level=index + 1,
                    price="10.00",
                    included_devices=2,
                    currency="EUR",
                    presentation={},
                )
            )
    period_queries = []

    def observed_query(connection, cursor, statement, parameters, context, executemany):
        bound_values = parameters.values() if isinstance(parameters, dict) else parameters
        if "v5_settings" in statement and "periods" in bound_values:
            period_queries.append(None)

    event.listen(db.engine, "before_cursor_execute", observed_query)
    try:
        items = admin.items("catalog")
    finally:
        event.remove(db.engine, "before_cursor_execute", observed_query)
    assert len(items) >= 4
    assert all(item["base_period_days"] == 31 for item in items)
    assert len(period_queries) == 1
