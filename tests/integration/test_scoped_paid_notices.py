"""Approved scoped payments produce only their own paid notices."""

import pytest
from sqlalchemy import select
from sunrise_infrastructure.db import (
    Job,
    Notification,
    NotificationDelivery,
    Order,
    Payment,
    Setting,
    Subscription,
)
from sunrise_infrastructure.notifications import NotificationService
from sunrise_infrastructure.scheduler import Scheduler

from tests.contract.test_scoped_payment_policy import synthetic_scope  # noqa: F401
from tests.integration.test_scoped_real_invoices import prepared_invoice, scoped_database  # noqa: F401,F811


def test_scoped_paid_notices_are_limited_to_paid_order_and_telegram_recipient(scoped_database):  # noqa: F811 -- pytest fixture
    db, settings, _, commerce, account_id = scoped_database
    settings.admin_email_allowlist = ["owner@example.test"]
    settings.admin_telegram_allowlist = [settings.test_telegram_recipients]
    order, billing, provider, _, _ = prepared_invoice(scoped_database)
    try:
        assert billing.poll_known_payments() == 1
        assert billing.reconcile_inbox(provider) == 1
        with db.transaction() as session:
            notices = list(
                session.scalars(
                    select(Notification).where(
                        Notification.event_key.in_(["payment:" + order["id"], "sale:" + order["id"]])
                    )
                )
            )
            assert {notice.kind for notice in notices} == {"payment_received", "sale"}
            assert next(n for n in notices if n.kind == "payment_received").title == "Заказ оплачен"
            sale = next(n for n in notices if n.kind == "sale")
            assert sale.title == "Новая покупка!"
            assert "Метод: СБП" in sale.body
            assert "Сумма: " + str(session.get(Order, order["id"]).amount) + " RUB" in sale.body
            NotificationService(db, settings).emit_in(
                session,
                account_id,
                "unrelated:fixture",
                "generic",
                "Другое событие",
                "Не относится к оплате.",
                channel="telegram",
            )
        Scheduler(commerce, settings).notify_ready(scoped_only=True)
        with db.transaction() as session:
            assert (
                session.scalar(
                    select(Notification.id).where(Notification.event_key == "access-ready:" + order["id"])
                )
                is None
            )
        sent = []
        notifier = NotificationService(
            db,
            settings,
            telegram_transport=lambda recipient, payload, identifier: sent.append((recipient, payload)),
        )
        assert notifier.deliver_pending(scoped_only=True) == 2
        assert [recipient for recipient, _ in sent] == [settings.test_telegram_recipients] * 2
        payment_payload = next(payload for _, payload in sent if payload["kind"] == "payment_received")
        buttons = notifier._paid_payment_buttons(payment_payload)
        assert buttons[0][0]["text"].endswith("Мои подписки")
        assert buttons[0][0]["callback_data"] == "notice:subscriptions"
        assert buttons[1][0]["text"].endswith("Главное меню") and buttons[1][0]["style"] == "danger"
        with db.transaction() as session:
            unrelated = session.scalar(
                select(Notification).where(Notification.event_key == "unrelated:fixture")
            )
            delivery = session.scalar(
                select(NotificationDelivery).where(NotificationDelivery.notification_id == unrelated.id)
            )
            assert delivery.state == "pending"
            provision = session.scalar(select(Job).where(Job.key == "provision:" + order["id"]))
            provision.state, provision.last_error = "done", None
            subscription = session.get(Subscription, session.get(Order, order["id"]).subscription_id)
            subscription.status = "active"
        Scheduler(commerce, settings).notify_ready(scoped_only=True)
        Scheduler(commerce, settings).notify_ready(scoped_only=True)
        with db.transaction() as session:
            ready = list(
                session.scalars(
                    select(Notification).where(Notification.event_key == "access-ready:" + order["id"])
                )
            )
            assert ready == []
        assert notifier.deliver_pending(scoped_only=True) == 0
        assert [payload["kind"] for _, payload in sent].count("access_ready") == 0
        assert "access_ready" not in [item["kind"] for item in notifier.list(account_id)["items"]]
    finally:
        provider.client.close()


@pytest.mark.parametrize("change", ["current", "account_admission", "paid_proof"])
def test_rejected_old_scope_notice_never_mutates_an_unproven_scoped_receipt(scoped_database, change):  # noqa: F811
    db, settings, _, commerce, account_id = scoped_database
    order, billing, provider, _, _ = prepared_invoice(scoped_database)
    try:
        assert billing.poll_known_payments() == 1
        assert billing.reconcile_inbox(provider) == 1
        with db.transaction() as session:
            for row in session.scalars(select(NotificationDelivery)):
                row.state = "delivered"
            item = Notification(
                account_id=account_id,
                event_key="access-ready:" + order["id"],
                kind="access_ready",
                title="Старый маркер",
                body="История",
            )
            session.add(item)
            session.flush()
            delivery = NotificationDelivery(
                notification_id=item.id, channel="telegram", recipient=settings.test_telegram_recipients
            )
            session.add(delivery)
            session.flush()
            identifier = delivery.id
            if change == "account_admission":
                marker = session.get(Setting, "scoped_test_account:" + str(account_id))
                marker.value = marker.value | {"scope_id": "foreign-scope"}
            elif change == "paid_proof":
                session.scalar(
                    select(Payment).where(Payment.order_id == order["id"])
                ).provider_id = "foreign-payment"
        sent = []
        notifier = NotificationService(db, settings, telegram_transport=lambda *args: sent.append(args))
        assert notifier.deliver_pending(scoped_only=True) == 0
        with db.transaction() as session:
            assert session.get(NotificationDelivery, identifier).state == (
                "cancelled" if change == "current" else "pending"
            )
        assert not sent
    finally:
        provider.client.close()


@pytest.mark.parametrize("change", ["order_status", "payment_provider_id"])
def test_scoped_paid_notice_rechecks_order_before_final_transport(scoped_database, change):  # noqa: F811 -- pytest fixture
    db, settings, _, _, _ = scoped_database
    order, billing, provider, _, _ = prepared_invoice(scoped_database)
    try:
        assert billing.poll_known_payments() == 1
        assert billing.reconcile_inbox(provider) == 1
        original = NotificationService._deliver
        sent = []
        notifier = NotificationService(
            db, settings, telegram_transport=lambda recipient, payload, identifier: sent.append(payload)
        )

        def changed_order(channel, recipient, payload, identifier):
            with db.transaction() as session:
                if change == "order_status":
                    session.get(Order, order["id"]).status = "awaiting_payment"
                else:
                    session.scalar(
                        select(Payment).where(Payment.order_id == order["id"])
                    ).provider_id = "different-payment"
            return original(notifier, channel, recipient, payload, identifier)

        notifier._deliver = changed_order
        assert notifier.deliver_pending(scoped_only=True) == 0
        assert not sent
        with db.transaction() as session:
            payment = session.scalar(
                select(Notification).where(Notification.event_key == "payment:" + order["id"])
            )
            delivery = session.scalar(
                select(NotificationDelivery).where(NotificationDelivery.notification_id == payment.id)
            )
            assert delivery.state == "review"
    finally:
        provider.client.close()
