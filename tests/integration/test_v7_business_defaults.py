import pytest
from sqlalchemy.exc import IntegrityError
from sunrise_infrastructure.db import Account, Product, Subscription

from tests.isolated_database import isolated_database


@pytest.fixture
def db():
    with isolated_database("business_defaults") as database:
        yield database


@pytest.mark.parametrize("field", ["included_devices", "currency"])
def test_postgres_product_requires_explicit_business_values(db, field):
    values = {
        "code": "explicit",
        "name": "Явный тариф",
        "tier_level": 5,
        "price": "12.50",
        "currency": "EUR",
        "included_devices": 6,
    }
    with pytest.raises(IntegrityError):
        with db.transaction() as session:
            session.add(Product(**{key: value for key, value in values.items() if key != field}))


def test_postgres_subscription_preserves_unknown_capacity_and_explicit_paid_values(db):
    with db.transaction() as session:
        account = Account(email="devices-policy@example.test")
        product = Product(
            code="arbitrary",
            name="Настроенный тариф",
            tier_level=8,
            price="29.00",
            currency="USD",
            included_devices=7,
        )
        session.add_all([account, product])
        session.flush()
        account_id, product_id = account.id, product.id
    with db.transaction() as session:
        unknown = Subscription(account_id=account_id, product_id=product_id, devices_limit=None)
        session.add(unknown)
        session.flush()
        unknown_id = unknown.id
    with db.transaction() as session:
        subscription = Subscription(
            account_id=account_id,
            product_id=product_id,
            devices_limit=11,
            status="active",
            panel_uuid="synthetic-paid-uuid",
        )
        session.add(subscription)
        session.flush()
        subscription_id = subscription.id
    with db.transaction() as session:
        session.get(Product, product_id).name = "Изменено владельцем"
        session.get(Subscription, subscription_id).name = "Платная подписка"
    with db.transaction() as session:
        assert session.get(Subscription, unknown_id).devices_limit is None
        product = session.get(Product, product_id)
        subscription = session.get(Subscription, subscription_id)
        assert (product.currency, product.included_devices) == ("USD", 7)
        assert (subscription.devices_limit, subscription.panel_uuid, subscription.status) == (
            11,
            "synthetic-paid-uuid",
            "active",
        )
