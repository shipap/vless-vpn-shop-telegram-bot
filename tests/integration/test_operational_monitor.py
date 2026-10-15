from datetime import timedelta

import pytest
from sqlalchemy import select
from sunrise_infrastructure.db import (
    Job,
    Notification,
    NotificationDelivery,
    Order,
    Quote,
    Setting,
    Subscription,
    now,
)

from tests.integration.test_v11_notifications import notification_context as notification_context


def test_stalled_jobs_are_actionable_without_exposing_provider_secrets(notification_context):
    from sunrise_infrastructure.operational_monitor import OperationalMonitor

    db, config, (owner, _, _) = notification_context
    with db.transaction() as session:
        session.add_all(
            [
                Job(
                    id="stalled",
                    key="stalled",
                    kind="provision",
                    account_id=owner,
                    payload={"order_id": "order-safe", "connection_url": "https://private.invalid/secret"},
                    state="pending",
                    available_at=now() - timedelta(minutes=20),
                    last_error="provider-private-secret",
                ),
                Job(
                    id="future",
                    key="future",
                    kind="provision",
                    account_id=owner,
                    payload={},
                    state="pending",
                    available_at=now() + timedelta(minutes=20),
                ),
                Job(
                    id="unknown",
                    key="unknown",
                    kind="cancel_agreement",
                    account_id=owner,
                    payload={},
                    state="review",
                    available_at=now(),
                ),
            ]
        )
    monitor = OperationalMonitor(db, config)
    result = monitor.snapshot()
    assert {item["reference"] for item in result["items"]} == {"stalled", "unknown"}
    assert "private-secret" not in str(result) and "connection_url" not in str(result)
    assert next(x for x in result["items"] if x["reference"] == "stalled")["retry_kind"] == "provisioning"
    assert next(x for x in result["items"] if x["reference"] == "unknown")["retry_kind"] is None


@pytest.mark.parametrize("local_test", [True, False])
def test_owner_alert_is_durable_deduplicated_and_excludes_revoked_owner(notification_context, local_test):
    from sunrise_infrastructure.operational_monitor import OperationalMonitor

    db, config, (owner, _, _) = notification_context
    config.local_test = local_test
    with db.transaction() as session:
        session.add(
            Job(
                id="review",
                key="review",
                kind="provision",
                account_id=owner,
                payload={"order_id": "order-safe"},
                state="review",
                available_at=now(),
            )
        )
    monitor = OperationalMonitor(db, config)
    assert monitor.notify_owner() == 1
    assert monitor.notify_owner() == 0
    with db.transaction() as session:
        notice = session.scalar(select(Notification).where(Notification.kind == "operational_alert"))
        assert notice.account_id == owner and notice.audience == "owner"
        assert notice.target == "/admin#operations"
        assert "Записей, требующих внимания: 1." in notice.body
        assert "Фоновые задачи: 1" in notice.body
        assert "Заказы: 0" in notice.body
        assert "Платёжные события: 0" in notice.body
        assert "Операции провайдера: 0" in notice.body
        assert "Доставка уведомлений: 0" in notice.body
        assert "Один заказ может учитываться в нескольких категориях" in notice.body
        assert ("Тестовый стенд" in notice.body) is local_test
        assert "Повтор выдачи доступа не выполняет повторное списание денег." in notice.body
        marker = session.scalar(select(Setting).where(Setting.key == "operational-alert:" + str(owner)))
        assert marker is not None
    config.owner_id = None
    config.admin_telegram_allowlist = []
    with db.transaction() as session:
        session.add(Job(id="another", key="another", kind="provision", payload={}, state="review"))
    assert monitor.notify_owner() == 0


def test_old_fulfilled_payment_is_not_reported_when_later_renewal_is_stalled(notification_context):
    from sunrise_infrastructure.operational_monitor import OperationalMonitor

    db, config, (owner, _, _) = notification_context
    with db.transaction() as session:
        subscription = Subscription(account_id=owner, devices_limit=1, status="provisioning_pending")
        session.add(subscription)
        session.flush()
        for identifier, days, state in [("old", 30, "done"), ("new", 1, "pending")]:
            quote = Quote(account_id=owner, snapshot={}, expires_at=now())
            session.add(quote)
            session.flush()
            session.add(
                Order(
                    id=identifier,
                    account_id=owner,
                    quote_id=quote.id,
                    idempotency_key=identifier,
                    status="paid",
                    amount="10",
                    currency="RUB",
                    subscription_id=subscription.id,
                    created_at=now() - timedelta(days=days),
                    fulfilled_at=now() - timedelta(days=days),
                )
            )
            session.add(
                Job(
                    id="job-" + identifier,
                    key="provision:" + identifier,
                    kind="provision",
                    account_id=owner,
                    payload={"order_id": identifier},
                    state=state,
                    available_at=now() - timedelta(days=days),
                )
            )
    items = OperationalMonitor(db, config).snapshot()["items"]
    assert all(item["order_id"] != "old" for item in items)
    assert any(item["order_id"] == "new" for item in items)


def test_modern_delivery_unknown_is_visible_but_normal_recipient_block_is_not(notification_context):
    from sunrise_infrastructure.notifications import NotificationService
    from sunrise_infrastructure.operational_monitor import OperationalMonitor

    db, config, (owner, _, _) = notification_context
    with db.transaction() as session:
        for key, state, error in [
            ("unknown-mail", "review", "mail_delivery_unknown"),
            ("not-allowed", "blocked", "recipient_not_authorized"),
        ]:
            notice = NotificationService(db, config).emit_in(
                session, owner, key, "security", "Событие", "Текст", channel="inbox"
            )
            session.add(
                NotificationDelivery(
                    notification_id=notice.id,
                    channel="email",
                    recipient="owner@example.test",
                    state=state,
                    last_error=error,
                )
            )
    result = OperationalMonitor(db, config).snapshot()
    assert result["counts"]["delivery"] == 1
    assert len(result["items"]) == 1 and result["items"][0]["retry_kind"] is None
