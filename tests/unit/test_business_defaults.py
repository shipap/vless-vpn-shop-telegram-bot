from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sunrise_api.schemas import ProductCreate, ProductUpdate
from sunrise_infrastructure import legacy
from sunrise_infrastructure.db import Account, Database, Product, Segment, Subscription


@pytest.fixture
def db():
    database = Database("sqlite+pysqlite:///:memory:")
    for model in (Account, Product, Subscription, Segment):
        model.__table__.create(database.engine)
    with database.transaction() as session:
        session.add(Account(id=1))
    yield database
    database.engine.dispose()


PRODUCT = {
    "code": "explicit_catalog",
    "name": "Настроенный тариф",
    "tier_level": 7,
    "price": Decimal("19.00"),
    "currency": "USD",
    "included_devices": 9,
}


@pytest.mark.parametrize("field", ["included_devices", "currency"])
def test_new_product_cannot_silently_invent_business_values(db, field):
    with pytest.raises(IntegrityError):
        with db.transaction() as session:
            session.add(Product(**{key: value for key, value in PRODUCT.items() if key != field}))


def test_unknown_current_capacity_is_preserved_without_inventing_a_limit(db):
    with db.transaction() as session:
        sub = Subscription(account_id=1, status="active", devices_limit=None)
        session.add(sub)
        session.flush()
        sid = sub.id
    with db.transaction() as session:
        assert session.get(Subscription, sid).devices_limit is None


def test_new_paid_segment_still_requires_finite_capacity(db):
    with db.transaction() as session:
        product = Product(**PRODUCT)
        sub = Subscription(account_id=1, status="active", devices_limit=None)
        session.add_all([product, sub])
        session.flush()
        pid, sid = product.id, sub.id
    start = datetime.now(UTC)
    with pytest.raises(IntegrityError):
        with db.transaction() as session:
            session.add(
                Segment(
                    subscription_id=sid,
                    product_id=pid,
                    starts_at=start,
                    ends_at=start + timedelta(days=30),
                    devices=None,
                    paid_value=Decimal("19.00"),
                    price_basis={},
                )
            )


def test_explicit_catalog_and_paid_subscription_values_survive_unrelated_updates(db):
    with db.transaction() as session:
        product = Product(**PRODUCT)
        session.add(product)
        session.flush()
        subscription = Subscription(account_id=1, product_id=product.id, devices_limit=13, status="active")
        session.add(subscription)
        session.flush()
        product_id, subscription_id = product.id, subscription.id
    with db.transaction() as session:
        session.get(Product, product_id).description = "Изменено описание"
        session.get(Subscription, subscription_id).name = "Сохранённые платные права"
    with db.transaction() as session:
        assert session.get(Product, product_id).currency == "USD"
        assert session.get(Product, product_id).included_devices == 9
        assert session.get(Subscription, subscription_id).devices_limit == 13


@pytest.mark.parametrize("field", ["included_devices", "currency"])
def test_api_product_creation_requires_catalog_policy(field):
    payload = PRODUCT | {"price": "19.00", "reason": "Настройка каталога владельцем", "confirmed": True}
    with pytest.raises(ValidationError) as rejected:
        ProductCreate.model_validate({key: value for key, value in payload.items() if key != field})
    assert rejected.value.errors()[0]["loc"] == (field,)


def test_api_product_patch_keeps_omitted_policy_unchanged():
    patch = ProductUpdate(name="Новое название", reason="Редактирование владельцем", confirmed=True)
    assert "included_devices" not in patch.model_dump(exclude_unset=True)
    assert "currency" not in patch.model_dump(exclude_unset=True)


def test_legacy_only_interprets_absent_columns_using_historical_contract():
    assert legacy.legacy_device_limit({}, "included_devices") == 4
    assert legacy.legacy_device_limit({}, "devices_limit") == 4
    assert legacy.legacy_currency({}) == "RUB"
    assert legacy.legacy_device_limit({"included_devices": 11}, "included_devices") == 11
    assert legacy.legacy_device_limit({"devices_limit": 17}, "devices_limit") == 17
    assert legacy.legacy_currency({"currency": "EUR"}) == "EUR"


@pytest.mark.parametrize("value", [None, 0, -1, "4", True])
def test_legacy_present_invalid_device_column_is_not_replaced(value):
    with pytest.raises(ValueError):
        legacy.legacy_device_limit({"devices_limit": value}, "devices_limit")


@pytest.mark.parametrize("value", [None, "", "rub", "RUB\n", 1])
def test_legacy_present_invalid_currency_column_is_not_replaced(value):
    with pytest.raises(ValueError):
        legacy.legacy_currency({"currency": value})


@pytest.mark.parametrize(
    "presentation,periods,expected",
    [
        ({"base_period_days": 42}, [], 42),
        ({}, [{"days": 31, "months": "1.0", "discount_percent": "0"}], 31),
        (
            {"periods": [{"days": 77, "months": 1, "discount_percent": "0"}]},
            [{"days": 31, "months": "1.0", "discount_percent": "0"}],
            77,
        ),
        ({}, [], None),
        (
            {},
            [
                {"days": 31, "months": "1.0", "discount_percent": "0"},
                {"days": 42, "months": 1, "discount_percent": "0"},
            ],
            None,
        ),
    ],
)
def test_catalog_base_period_is_explicit_or_derived_only_from_persisted_monthly_terms(
    presentation, periods, expected
):
    from sunrise_infrastructure.catalog import presentation_dto

    product = Product(**PRODUCT, presentation=presentation)
    result = presentation_dto(product, periods)
    assert result["base_period_days"] == expected
    assert product.presentation == presentation


@pytest.mark.parametrize(
    "price,months,discount,expected",
    [
        ("5.00", 1, 0.1, "5.00"),
        ("9999999999999999.00", 1.1, 27.1, "8018999999999999.20"),
    ],
)
def test_catalog_display_price_preserves_legacy_json_decimal_values(price, months, discount, expected):
    from sunrise_infrastructure.catalog import presentation_dto

    product = Product(**(PRODUCT | {"price": Decimal(price)}), presentation={})
    periods = [{"days": 42, "months": months, "discount_percent": discount}]
    result = presentation_dto(product, periods)
    assert result["periods"][0]["price"] == expected
    assert periods[0]["months"] == months
    assert periods[0]["discount_percent"] == discount
