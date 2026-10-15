from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from sunrise_infrastructure.billing import Billing
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import (
    Account,
    Job,
    Notification,
    NotificationDelivery,
    Order,
    ProviderOperation,
    Quote,
)
from sunrise_infrastructure.notifications import NotificationService

from tests.integration.test_payment_pipeline import SyntheticProvider
from tests.isolated_database import isolated_database
from tests.legal_fixture import accept_test_legal


@pytest.fixture
def pipeline():
    with isolated_database("checkout_notice") as db:
        connection = db.engine.connect()
        transaction = connection.begin()
        db.sessions = sessionmaker(
            bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        commerce = Commerce(db, False)
        with db.transaction() as session:
            account = Account(email=f"{uuid4()}@example.test", bonus_balance=Decimal("100"))
            session.add(account)
            session.flush()
            aid = account.id
        accept_test_legal(commerce, aid)
        quote = commerce.quote(aid, "standard", 30, "purchase", bonus_amount="49.50")
        order = commerce.create_order(aid, quote["id"], str(uuid4()))
        try:
            yield (
                commerce,
                Billing(commerce, "synthetic-terminal", "sandbox"),
                aid,
                order,
                SyntheticProvider(),
            )
        finally:
            transaction.rollback()
            connection.close()


def configure(context):
    commerce, billing, aid, order, provider = context
    with commerce.db.transaction() as session:
        account = session.get(Account, aid)
        account.telegram_id = 701234567890123456
    config = SimpleNamespace(
        test_telegram_recipients="701234567890123456",
        email_allowlist=[],
        admin_email_allowlist=[],
        dashboard_url="http://127.0.0.1:3200",
    )
    commerce.public_settings = config
    return config


def test_verified_checkout_preparation_returns_payment_link_without_notification(pipeline):
    config = configure(pipeline)
    commerce, billing, aid, order, provider = pipeline
    billing.prepare_payment(order["id"], provider)
    billing.prepare_payment(order["id"], provider)
    with commerce.db.transaction() as session:
        rows = list(
            session.scalars(
                select(Notification).where(
                    Notification.kind == "checkout_ready", Notification.account_id == aid
                )
            )
        )
        assert rows == []
        operation = session.scalar(select(ProviderOperation).where(ProviderOperation.order_id == order["id"]))
        assert operation.state == "created"
    assert commerce.order(aid, order["id"])["checkout_url"] == "https://pay.rollypay.io/invoice/synthetic"
    sent = []
    notifier = NotificationService(commerce.db, config, telegram_transport=lambda *args: sent.append(args))
    assert notifier.deliver_pending() == 0
    assert notifier.deliver_pending() == 0
    assert not sent


@pytest.mark.parametrize("change", ["paid", "wrong_owner", "unsafe_url", "unknown", "expired"])
def test_checkout_delivery_rechecks_current_order_and_trusted_operation(pipeline, change):
    config = configure(pipeline)
    commerce, billing, aid, order, provider = pipeline
    billing.prepare_payment(order["id"], provider)
    with commerce.db.transaction() as session:
        item = session.scalar(
            select(Notification).where(Notification.kind == "checkout_ready", Notification.account_id == aid)
        )
        if item is None:
            item = Notification(
                account_id=aid,
                event_key="checkout-ready:" + order["id"],
                kind="checkout_ready",
                title="Заказ готов к оплате",
                body="Старая очередь",
            )
            session.add(item)
            session.flush()
        notification_id = item.id
        if (
            session.scalar(
                select(NotificationDelivery.id).where(NotificationDelivery.notification_id == item.id)
            )
            is None
        ):
            session.add(
                NotificationDelivery(
                    notification_id=item.id, channel="telegram", recipient="701234567890123456"
                )
            )
        if change == "paid":
            session.get(Order, order["id"]).status = "paid"
        elif change == "expired":
            from datetime import timedelta

            from sunrise_infrastructure.db import now

            session.get(Quote, order["quote_id"]).expires_at = now() - timedelta(seconds=1)
        elif change == "wrong_owner":
            item.event_key = "checkout-ready:nonexistent-order"
        else:
            op = session.scalar(select(ProviderOperation).where(ProviderOperation.order_id == order["id"]))
            if change == "unsafe_url":
                op.pay_url = "https://evil.example.test/invoice"
            else:
                op.state = "outcome_unknown"
    sent = []
    notifier = NotificationService(commerce.db, config, telegram_transport=lambda *args: sent.append(args))
    assert notifier.deliver_pending() == 0
    assert not sent
    with commerce.db.transaction() as session:
        delivery = session.scalar(
            select(NotificationDelivery).where(NotificationDelivery.notification_id == notification_id)
        )
        assert delivery.state == "cancelled"


def test_real_paid_order_provisioning_is_linked_in_recovery_queue(pipeline):
    from datetime import timedelta

    from sunrise_infrastructure.billing import VerifiedPayment
    from sunrise_infrastructure.db import now
    from sunrise_infrastructure.operational_monitor import OperationalMonitor

    config = configure(pipeline)
    commerce, billing, aid, order, provider = pipeline
    billing.accept(
        VerifiedPayment(
            provider="fixture",
            provider_id="paid-recovery",
            terminal=provider.terminal,
            environment="sandbox",
            order_id=order["id"],
            amount=Decimal(order["total"]),
            currency="RUB",
            paid_at=now(),
        )
    )
    with commerce.db.transaction() as session:
        job = session.scalar(select(Job).where(Job.key == "provision:" + order["id"]))
        assert job.payload["order_id"] == order["id"]
        job.available_at = now() - timedelta(minutes=20)
        job.payload = {k: v for k, v in job.payload.items() if k != "order_id"}
        job_id = job.id
    row = next(
        row
        for row in OperationalMonitor(commerce.db, config).snapshot()["items"]
        if row["reference"] == job_id
    )
    assert row["order_id"] == order["id"]
    assert row["retry_kind"] == "provisioning"
