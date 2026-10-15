"""Rejected technical notices remain auditable but disappear from every customer channel."""

import pytest
from sqlalchemy import select
from sunrise_core.errors import DomainError
from sunrise_infrastructure.db import Notification, NotificationDelivery
from sunrise_infrastructure.notifications import NotificationService

from tests.integration.test_v11_notifications import notification_context as notification_context


@pytest.mark.parametrize("kind", ["checkout_ready", "access_ready"])
def test_old_notice_is_hidden_from_feed_counts_read_and_delivery_without_deleting_rows(
    notification_context, kind
):
    db, settings, (owner, _, _) = notification_context
    with db.transaction() as session:
        item = Notification(
            account_id=owner,
            event_key="rejected:" + kind,
            kind=kind,
            title="Старое уведомление",
            body="Сохранённая история",
        )
        session.add(item)
        session.flush()
        identifier = item.id
        for channel, recipient in [
            ("telegram", "202"),
            ("email", "owner@example.test"),
            ("push", "synthetic-push"),
        ]:
            session.add(NotificationDelivery(notification_id=item.id, channel=channel, recipient=recipient))
    sent = []
    notifier = NotificationService(db, settings, telegram_transport=lambda *args: sent.append(args))
    feed = notifier.list(owner, limit=1)
    assert feed["items"] == [] and feed["unread_count"] == feed["total_count"] == 0
    assert feed["next_offset"] is None
    with pytest.raises(DomainError) as error:
        notifier.mark_read(owner, identifier)
    assert error.value.status == 404
    notifier.mark_all_read(owner)
    assert notifier.deliver_pending() == 0 and not sent
    with db.transaction() as session:
        historical = session.get(Notification, identifier)
        assert historical.body == "Сохранённая история" and historical.read_at is None
        deliveries = list(
            session.scalars(
                select(NotificationDelivery).where(NotificationDelivery.notification_id == identifier)
            )
        )
        assert len(deliveries) == 3
        assert all(
            row.state == "cancelled" and row.last_error == "notification_suppressed" for row in deliveries
        )


@pytest.mark.parametrize("kind", ["checkout_ready", "access_ready"])
def test_already_claimed_rejected_notice_cannot_reach_even_injected_transport(notification_context, kind):
    db, settings, _ = notification_context
    sent = []
    notifier = NotificationService(db, settings, telegram_transport=lambda *args: sent.append(args))
    with pytest.raises(DomainError) as error:
        notifier._deliver("telegram", "202", {"kind": kind}, "synthetic-claimed-delivery")
    assert error.value.code == "notification_suppressed"
    assert not sent
