from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sunrise_infrastructure.db import Job, Notification, Order, Quote, Subscription, now
from sunrise_infrastructure.scheduler import Scheduler

from tests.integration.test_v11_notifications import notification_context as notification_context


@pytest.mark.parametrize("purchase_batch", [False, True])
def test_completed_notifications_do_not_starve_renewals_after_batch_limit(
    notification_context, purchase_batch
):
    db, settings, (owner, _, _) = notification_context
    with db.transaction() as session:
        subscription = Subscription(account_id=owner, devices_limit=1, status="active")
        session.add(subscription)
        session.flush()
        for i in range(1001):
            session.add(
                Job(
                    key=f"provision:order-{i:04d}",
                    kind="provision",
                    state="done",
                    account_id=owner,
                    payload={"subscription_id": subscription.id},
                )
            )
            quote = Quote(
                id=f"quote-{i}",
                account_id=owner,
                snapshot={"action": "purchase" if purchase_batch and i < 1000 else "renew"},
                expires_at=now() + timedelta(hours=1),
            )
            session.add(quote)
        session.flush()
        for i in range(1001):
            session.add(
                Order(
                    id=f"order-{i:04d}",
                    account_id=owner,
                    quote_id=f"quote-{i}",
                    idempotency_key=f"key-{i}",
                    status="paid",
                    amount=Decimal("1"),
                    currency="RUB",
                    subscription_id=subscription.id,
                )
            )
            session.add(
                Notification(
                    account_id=owner,
                    event_key=f"payment:order-{i:04d}",
                    kind="payment_received",
                    title="Оплата",
                    body="Получена",
                )
            )
    scheduler = Scheduler(SimpleNamespace(db=db), settings)
    scheduler.notify_ready()
    with db.transaction() as session:
        assert session.scalar(
            select(func.count()).select_from(Notification).where(Notification.kind == "subscription_renewed")
        ) == (1 if purchase_batch else 1000)
    scheduler.notify_ready()
    scheduler.notify_ready()
    with db.transaction() as session:
        assert session.scalar(
            select(func.count()).select_from(Notification).where(Notification.kind == "subscription_renewed")
        ) == (1 if purchase_batch else 1001)
        assert session.scalar(select(Notification.id).where(Notification.kind == "access_ready")) is None


@pytest.mark.parametrize(
    "state,error", [("pending", None), ("review", None), ("blocked", None), ("done", "superseded")]
)
def test_active_subscription_is_not_proof_this_order_was_issued(notification_context, state, error):
    db, settings, (owner, _, _) = notification_context
    with db.transaction() as session:
        subscription = Subscription(account_id=owner, devices_limit=1, status="active")
        quote = Quote(account_id=owner, snapshot={"action": "renew"}, expires_at=now())
        session.add_all([subscription, quote])
        session.flush()
        session.add(
            Order(
                id="unissued",
                account_id=owner,
                quote_id=quote.id,
                idempotency_key="unissued",
                status="paid",
                amount=Decimal("1"),
                currency="RUB",
                subscription_id=subscription.id,
            )
        )
        session.add(
            Job(
                key="provision:unissued",
                kind="provision",
                account_id=owner,
                payload={"subscription_id": subscription.id},
                state=state,
                last_error=error,
            )
        )
        session.add(
            Notification(
                account_id=owner,
                event_key="payment:unissued",
                kind="payment_received",
                title="Оплата",
                body="Получена",
            )
        )
    Scheduler(SimpleNamespace(db=db), settings).notify_ready()
    with db.transaction() as session:
        assert session.scalar(select(Notification.id).where(Notification.kind == "access_ready")) is None
        assert (
            session.scalar(select(Notification.id).where(Notification.kind == "subscription_renewed")) is None
        )
