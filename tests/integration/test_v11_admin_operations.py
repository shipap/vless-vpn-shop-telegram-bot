import importlib.util
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sunrise_core.errors import DomainError
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import AdminAuthenticator, Payment, Product, Segment, Subscription, now
from test_v11_notifications import notification_context as notification_context


def test_owner_gift_durable_idempotent_without_fake_payment(notification_context):
    db, settings, (owner, target, _) = notification_context
    with db.transaction() as session:
        product = Product(
            included_devices=4,
            currency="RUB",
            code="synthetic-v11",
            name="Тестовый тариф",
            tier_level=1,
            price=Decimal("100"),
        )
        session.add(product)
        session.add(
            AdminAuthenticator(account_id=owner, trusted_ip_digest="synthetic", trust_verified_at=now())
        )
        session.flush()
        product_id = product.id
    assert importlib.util.find_spec("sunrise_infrastructure.admin_operations") is not None, (
        "Owner grant missing"
    )
    from sunrise_infrastructure.admin_operations import AdminOperations

    service = AdminOperations(Commerce(db, local_test=True, public_settings=settings), settings)
    result = service.grant(
        owner, target, product_id, 7, "synthetic-grant", "Синтетический подарок", confirmed=True
    )
    assert (
        service.grant(
            owner, target, product_id, 7, "synthetic-grant", "Синтетический подарок", confirmed=True
        )
        == result
    )
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Subscription)) == 1
        assert session.scalar(select(func.count()).select_from(Segment)) == 1
        assert session.scalar(select(func.count()).select_from(Payment)) == 0
        assert session.scalar(select(Segment.paid_value)) == 0
    with pytest.raises(DomainError) as failure:
        service.grant(target, target, product_id, 7, "foreign-grant", "Запрещённый подарок", confirmed=True)
    assert failure.value.status == 404
