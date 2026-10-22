"""Confirmed Telegram failures cannot replay payments or ambiguous sends."""

from datetime import timedelta

import pytest
from sqlalchemy import select
from sunrise_core.errors import DomainError
from sunrise_infrastructure.db import Account, Notification, NotificationDelivery, now
from sunrise_infrastructure.notifications import NotificationService, _TelegramRetryAfter

from tests.integration.test_v11_notifications import notification_context  # noqa: F401


def enqueue(db, config, account, key):
    service = NotificationService(db, config)
    with db.transaction() as session:
        service.emit_in(session, account, key, "security", "Событие", "Текст")
    return service


@pytest.mark.parametrize(
    "code,state,blocked",
    [
        ("telegram_recipient_blocked", "cancelled", True),
        ("telegram_recipient_deactivated", "cancelled", True),
        ("telegram_chat_not_found", "cancelled", False),
        ("telegram_forbidden", "review", False),
        ("telegram_auth_failed", "review", False),
        ("telegram_endpoint_invalid", "review", False),
        ("telegram_rejected", "review", False),
        ("telegram_delivery_unknown", "review", False),
    ],
)
def test_definite_recipient_refusal_and_unknown_are_separate(notification_context, code, state, blocked):  # noqa: F811
    db, config, (owner, _, _) = notification_context
    service = enqueue(db, config, owner, "recovery-first")
    calls = []

    def transport(*args):
        calls.append(args)
        raise DomainError(code, "Отказ", 503)

    service.telegram_transport = transport
    assert service.deliver_pending() == 0
    with db.transaction() as session:
        delivery = session.scalar(select(NotificationDelivery))
        assert (delivery.state, delivery.last_error, delivery.attempts) == (state, code, 1)
        assert session.get(Account, owner).telegram_notifications_blocked is blocked
    assert service.deliver_pending() == 0
    assert len(calls) == 1
    if blocked:
        enqueue(db, config, owner, "recovery-second")
        service.deliver_pending()
        assert len(calls) == 1
        with db.transaction() as session:
            assert {row.state for row in session.scalars(select(NotificationDelivery))} <= {
                "cancelled",
                "blocked",
            }


@pytest.mark.parametrize("code", ["telegram_rate_limited", "telegram_temporary_failure"])
def test_definite_temporary_failures_exhaust_bounded_retries(notification_context, code):  # noqa: F811
    db, config, (owner, _, _) = notification_context
    service = enqueue(db, config, owner, "retry")
    calls = []

    def transport(*args):
        calls.append(args)
        raise _TelegramRetryAfter(17, code=code)

    service.telegram_transport = transport
    for attempt in range(1, 5):
        assert service.deliver_pending() == 0
        with db.transaction() as session:
            row = session.scalar(select(NotificationDelivery))
            assert row.attempts == attempt
            assert row.state == ("pending" if attempt < 4 else "review")
            assert row.last_error == code
            assert row.delivered_at is None
            assert row.available_at > now()
        if attempt < 4:
            with db.transaction() as session:
                session.scalar(select(NotificationDelivery)).available_at = now() - timedelta(seconds=1)
    service.deliver_pending()
    assert len(calls) == 4


def test_resume_keeps_historical_suppression_and_unknown_reviews(notification_context):  # noqa: F811
    db, config, (owner, _, _) = notification_context
    service = enqueue(db, config, owner, "historical")
    with db.transaction() as session:
        account = session.get(Account, owner)
        account.telegram_notifications_blocked = True
        telegram_id = str(account.telegram_id)
        row = session.scalar(select(NotificationDelivery))
        row.state, row.last_error = "review", "telegram_rejected"
    assert service.resume_telegram(owner, telegram_id) == {"status": "resumed"}
    calls = []
    service.telegram_transport = lambda *args: calls.append(args)
    assert service.deliver_pending() == 0
    with db.transaction() as session:
        assert session.get(Account, owner).telegram_notifications_blocked is False
        row = session.scalar(select(NotificationDelivery))
        assert (row.state, row.last_error, row.attempts) == ("review", "telegram_rejected", 0)
    assert calls == []


def test_late_block_response_does_not_block_rebound_identity(notification_context):  # noqa: F811
    db, config, (owner, _, target) = notification_context
    service = enqueue(db, config, owner, "race")
    with db.transaction() as session:
        telegram_id = session.get(Account, owner).telegram_id

    def transport(*args):
        with db.transaction() as session:
            session.get(Account, owner).telegram_id = None
            session.get(Account, target).telegram_id = telegram_id
        raise DomainError("telegram_recipient_blocked", "Отказ", 503)

    service.telegram_transport = transport
    service.deliver_pending()
    with db.transaction() as session:
        assert not session.get(Account, owner).telegram_notifications_blocked
        assert not session.get(Account, target).telegram_notifications_blocked


def test_fresh_private_activity_wins_over_an_older_block_response(notification_context):  # noqa: F811
    db, config, (owner, _, _) = notification_context
    service = enqueue(db, config, owner, "activity-race")
    with db.transaction() as session:
        telegram_id = str(session.get(Account, owner).telegram_id)

    def transport(*args):
        service.resume_telegram(owner, telegram_id)
        raise DomainError("telegram_recipient_blocked", "Отказ", 503)

    service.telegram_transport = transport
    service.deliver_pending()
    with db.transaction() as session:
        assert not session.get(Account, owner).telegram_notifications_blocked
        assert session.scalar(select(NotificationDelivery)).state == "cancelled"


def test_late_block_response_cannot_block_merged_canonical_account(notification_context):  # noqa: F811
    db, config, (owner, _, target) = notification_context
    service = enqueue(db, config, owner, "merge-activity-race")
    with db.transaction() as session:
        telegram_id = session.get(Account, owner).telegram_id

    def transport(*args):
        service.resume_telegram(owner, str(telegram_id))
        with db.transaction() as session:
            # A merge preserves in-flight deliveries and moves their notification.
            assert session.scalar(select(NotificationDelivery)).state == "in_flight"
            session.get(Account, owner).telegram_id = None
            session.get(Account, target).telegram_id = telegram_id
            session.scalar(select(Notification)).account_id = target
        raise DomainError("telegram_recipient_blocked", "Отказ", 503)

    service.telegram_transport = transport
    assert service.deliver_pending() == 0
    with db.transaction() as session:
        assert not session.get(Account, target).telegram_notifications_blocked
        assert session.scalar(select(NotificationDelivery)).state == "cancelled"
