from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import (
    Account,
    Agreement,
    Notification,
    NotificationDelivery,
    Product,
    RecurringAttempt,
    RecurringCycle,
    Setting,
    Subscription,
    now,
)
from sunrise_infrastructure.notifications import NotificationService, validate_telegram_content
from sunrise_infrastructure.providers import RollyPay
from sunrise_infrastructure.recurring import Recurring

from tests.integration.test_recurring import RecurringProvider
from tests.isolated_database import isolated_database
from tests.legal_fixture import accept_test_legal


@pytest.fixture
def failure_context():
    with isolated_database("recurring_fail") as db:
        commerce = Commerce(db, True)
        with db.transaction() as session:
            account = Account(
                id=9007199254740993,
                email="recurring@example.test",
                email_verified=True,
                telegram_id=701234567890123457,
            )
            session.add(account)
            session.get(Setting, "recurring").value = {"enabled": True}
            session.get(Product, "standard").name = "<b>Согласованный & тариф</b>"
        accept_test_legal(commerce, str(account.id))
        quote = commerce.quote(str(account.id), "standard", 30, "purchase")
        order = commerce.create_order(str(account.id), quote["id"], uuid4().hex)
        commerce.test_pay(str(account.id), order["id"])
        subscription_id = commerce.dashboard(str(account.id))["subscriptions"][0]["id"]
        recurring = Recurring(commerce, RecurringProvider())
        agreement = recurring.enroll(str(account.id), subscription_id, "plan-standard", True, uuid4().hex)
        config = SimpleNamespace(
            local_test=True,
            email_allowlist=["recurring@example.test"],
            admin_email_allowlist=[],
            test_telegram_recipients="701234567890123457",
            bot_test_token=None,
            push_private_key=None,
        )
        commerce.public_settings = config
        with db.transaction() as session:
            session.get(Setting, "recurring").value = {"enabled": False}
            session.get(Product, "standard").name = "Переименованный тариф"
            before = session.get(Subscription, subscription_id).expires_at
        moment = now() - timedelta(seconds=10)
        charge = {
            "cycle_number": 1,
            "status": "fail",
            "validation_status": "accepted",
            "payment_id": None,
            "amount_rub": "99.00",
            "expected_amount_rub": "99.00",
            "scheduled_at": moment.isoformat(),
            "actual_at": moment.isoformat(),
            "message": "secret-link https://example.test/credential <script>hostile</script>",
        }
        context = SimpleNamespace(
            commerce=commerce,
            recurring=recurring,
            config=config,
            aid=account.id,
            sid=subscription_id,
            agreement=agreement,
            charges=[charge],
            requests=[],
            payments={},
            before=before,
            remote_state="enabled",
        )

        def handler(request):
            context.requests.append((request.method, request.url.path))
            assert request.method == "GET", "Notifications must never create, stop or charge agreements"
            if request.url.path.endswith("/charges"):
                return httpx.Response(200, json=context.charges)
            if "/payments/" in request.url.path:
                return httpx.Response(200, json=context.payments[request.url.path.rsplit("/", 1)[1]])
            return httpx.Response(
                200,
                json={
                    "id": agreement["provider_id"],
                    "state": "active",
                    "billing_status": context.remote_state,
                },
            )

        provider = RollyPay(
            "https://payments.example.test",
            "synthetic",
            "test-recurring",
            {"payments.example.test"},
            recurring_authorized=True,
            transport=httpx.MockTransport(handler),
        )
        recurring.provider = provider
        try:
            yield context
        finally:
            provider.client.close()


@pytest.mark.parametrize("telegram", [True, False])
def test_confirmed_failure_queues_one_customer_event_and_preserves_rights_and_consent(
    failure_context, telegram
):
    context = failure_context
    db = context.commerce.db
    with db.transaction() as session:
        if not telegram:
            session.get(Account, context.aid).telegram_id = None
        consent = session.get(Agreement, context.agreement["id"]).consent
    context.recurring.reconcile()
    context.recurring.reconcile()
    with db.transaction() as session:
        notifications = list(session.scalars(select(Notification)))
        assert len(notifications) == 1
        item = notifications[0]
        assert item.account_id == 9007199254740993 and item.audience == "account"
        assert item.kind == "recurring_failed" and item.target == "/#subscriptions"
        assert "<b>Согласованный & тариф</b>" in item.body and "Переименованный тариф" not in item.body
        assert "99.00 RUB" in item.body and "30" in item.body
        assert "secret-link" not in item.body and "credential" not in item.body
        assert item.actions == [{"label": "Проверить статус", "target": "/#subscriptions"}]
        assert "&lt;b&gt;" in validate_telegram_content(item.title, item.body)
        delivery = session.scalar(select(NotificationDelivery))
        assert delivery.channel == ("telegram" if telegram else "email")
        assert delivery.recipient == ("701234567890123457" if telegram else "recurring@example.test")
        assert session.get(Subscription, context.sid).expires_at == context.before
        assert list(session.scalars(select(RecurringCycle))) == []
        assert session.get(Agreement, context.agreement["id"]).consent == consent | {
            "last_checked_at": session.get(Agreement, context.agreement["id"]).consent["last_checked_at"]
        }
        attempt = session.scalar(select(RecurringAttempt))
        assert attempt.state == "fail" and attempt.occurred_at == datetime.fromisoformat(
            context.charges[0]["actual_at"]
        )
        evidence = session.get(Setting, "rc_attempt:" + attempt.id).value
        assert evidence["account_id"] == "9007199254740993"
        assert evidence["agreement_id"] == context.agreement["id"]
        assert evidence["cycle_number"] == 1 and evidence["status"] == "fail"
        assert "message" not in evidence and "credential" not in str(evidence)
        saved_evidence = dict(evidence)
    context.charges.append(context.charges[0] | {"actual_at": now().isoformat()})
    context.recurring.reconcile()
    with db.transaction() as session:
        assert len(list(session.scalars(select(Notification)))) == 1
        assert len(list(session.scalars(select(RecurringAttempt)))) == 2
        assert session.get(Setting, "rc_attempt:" + attempt.id).value == saved_evidence
    sent = []

    class Mail:
        def deliver(self, recipient, subject, body, identifier):
            sent.append((recipient, body))

    service = NotificationService(
        db,
        context.config,
        mail_transport=Mail(),
        telegram_transport=lambda recipient, payload, identifier: sent.append((recipient, payload["body"])),
    )
    assert service.deliver_pending() == 1
    assert service.deliver_pending() == 0 and len(sent) == 1
    assert all(method == "GET" for method, path in context.requests)


@pytest.mark.parametrize(
    "status,validation",
    [("fail", "review"), ("fail", None), ("pending", "accepted"), ("unknown", "accepted")],
)
def test_unconfirmed_charge_never_sends_decline_or_changes_paid_time(failure_context, status, validation):
    context = failure_context
    context.charges[0] = context.charges[0] | {"status": status, "validation_status": validation}
    context.recurring.reconcile()
    with context.commerce.db.transaction() as session:
        assert list(session.scalars(select(Notification))) == []
        assert session.get(Subscription, context.sid).expires_at == context.before
        attempt = session.scalar(select(RecurringAttempt))
        assert session.get(Setting, "rc_attempt:" + attempt.id) is not None


def test_later_paid_same_cycle_suppresses_historical_failure_and_extends_once(failure_context):
    context = failure_context
    paid_id = "synthetic-paid-cycle"
    context.charges.append(context.charges[0] | {"status": "payed", "payment_id": paid_id})
    context.payments[paid_id] = {
        "payment_id": paid_id,
        "amount": "99.00",
        "payment_currency": "RUB",
        "status": "paid",
        "subscription_id": context.agreement["provider_id"],
        "paid_at": now().isoformat(),
        "terminal_id": "test-recurring",
        "test": True,
    }
    context.recurring.reconcile()
    context.recurring.reconcile()
    with context.commerce.db.transaction() as session:
        assert (
            list(session.scalars(select(Notification).where(Notification.kind == "recurring_failed"))) == []
        )
        assert session.get(Subscription, context.sid).expires_at == context.before + timedelta(days=30)
        assert len(list(session.scalars(select(RecurringCycle)))) == 1
    context.charges = context.charges[:1]
    context.recurring.reconcile()
    with context.commerce.db.transaction() as session:
        assert (
            list(session.scalars(select(Notification).where(Notification.kind == "recurring_failed"))) == []
        )


def test_review_or_pending_same_cycle_blocks_stale_failure_notification(failure_context):
    context = failure_context
    context.charges.append(context.charges[0] | {"status": "payed", "validation_status": "review"})
    context.recurring.reconcile()
    with context.commerce.db.transaction() as session:
        assert list(session.scalars(select(Notification))) == []
        assert session.get(Subscription, context.sid).expires_at == context.before


def test_parallel_reconciliation_records_attempt_and_customer_event_once(failure_context):
    context = failure_context
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: context.recurring.reconcile(), range(2)))
    with context.commerce.db.transaction() as session:
        assert len(list(session.scalars(select(Notification)))) == 1
        assert len(list(session.scalars(select(NotificationDelivery)))) == 1
        assert len(list(session.scalars(select(RecurringAttempt)))) == 1


def test_array_contract_timeout_never_creates_failed_cycle_event(failure_context):
    context = failure_context
    context.charges = None
    context.recurring.reconcile()
    with context.commerce.db.transaction() as session:
        assert list(session.scalars(select(Notification))) == []
        assert session.get(Agreement, context.agreement["id"]).state == "review"


@pytest.mark.parametrize("cycle", [True, "1", None, {"unexpected": "cycle"}])
def test_invalid_cycle_reference_never_creates_customer_event(failure_context, cycle):
    context = failure_context
    context.charges[0] = context.charges[0] | {"cycle_number": cycle}
    context.recurring.reconcile()
    with context.commerce.db.transaction() as session:
        assert list(session.scalars(select(Notification))) == []
        assert list(session.scalars(select(RecurringAttempt))) == []


def test_paid_cycle_cancels_pending_failure_delivery_but_preserves_attempt_history(failure_context):
    context = failure_context
    context.recurring.reconcile()
    with context.commerce.db.transaction() as session:
        assert session.scalar(select(NotificationDelivery)).state == "pending"
    paid_id = "synthetic-later-payment"
    context.charges.append(context.charges[0] | {"status": "payed", "payment_id": paid_id})
    context.payments[paid_id] = {
        "payment_id": paid_id,
        "amount": "99.00",
        "payment_currency": "RUB",
        "status": "paid",
        "subscription_id": context.agreement["provider_id"],
        "paid_at": now().isoformat(),
        "terminal_id": "test-recurring",
        "test": True,
    }
    context.recurring.reconcile()
    with context.commerce.db.transaction() as session:
        decline = session.scalar(select(Notification).where(Notification.kind == "recurring_failed"))
        delivery = session.scalar(
            select(NotificationDelivery).where(NotificationDelivery.notification_id == decline.id)
        )
        assert delivery.state == "cancelled" and delivery.last_error == "recurring_cycle_paid"
        assert len(list(session.scalars(select(RecurringAttempt)))) == 2
    sent = []
    service = NotificationService(
        context.commerce.db,
        context.config,
        telegram_transport=lambda recipient, payload, identifier: sent.append(payload["title"]),
    )
    service.deliver_pending()
    assert "Автопродление не прошло" not in sent


def test_malformed_pending_cycle_is_ignored_without_aborting_valid_history(failure_context):
    context = failure_context
    context.charges.append(context.charges[0] | {"cycle_number": {"unexpected": 1}, "status": "pending"})
    context.recurring.reconcile()
    with context.commerce.db.transaction() as session:
        assert len(list(session.scalars(select(Notification)))) == 1


@pytest.mark.parametrize("status", [{"unexpected": "status"}, "unexpected-" * 20])
def test_malformed_attempt_status_is_recorded_as_unknown_without_customer_decline(failure_context, status):
    context = failure_context
    context.charges[0] = context.charges[0] | {"status": status}
    context.recurring.reconcile()
    with context.commerce.db.transaction() as session:
        assert list(session.scalars(select(Notification))) == []
        assert session.scalar(select(RecurringAttempt)).state == "unknown"
