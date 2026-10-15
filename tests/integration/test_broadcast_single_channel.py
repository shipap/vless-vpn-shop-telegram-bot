from sqlalchemy import select
from sunrise_infrastructure.db import NotificationDelivery, NotificationPreference, PushSubscription
from sunrise_infrastructure.notifications import NotificationService

from tests.integration.test_v11_notifications import notification_context as notification_context


def test_broadcast_uses_only_preferred_bound_channel_even_with_browser_push_enabled(notification_context):
    db, settings, (owner, _, _) = notification_context
    service = NotificationService(db, settings)
    with db.transaction() as session:
        session.add(NotificationPreference(account_id=owner, browser_enabled=True))
        session.add(
            PushSubscription(
                account_id=owner, endpoint_digest="synthetic-endpoint", subscription_ciphertext="synthetic"
            )
        )
    with db.transaction() as session:
        service.emit_in(session, owner, "broadcast:single", "broadcast", "Рассылка", "Новость")
    with db.transaction() as session:
        assert list(session.scalars(select(NotificationDelivery.channel))) == ["telegram"]


def test_legacy_broadcast_push_is_blocked_before_transport(notification_context):
    db, settings, (owner, _, _) = notification_context
    settings.push_test_authorized = True
    service = NotificationService(db, settings)
    with db.transaction() as session:
        session.add(NotificationPreference(account_id=owner, browser_enabled=True))
        subscription = PushSubscription(
            account_id=owner, endpoint_digest="old-broadcast", subscription_ciphertext="synthetic"
        )
        session.add(subscription)
        session.flush()
        item = service.emit_in(
            session, owner, "broadcast:legacy", "broadcast", "Рассылка", "Новость", channel="inbox"
        )
        delivery = NotificationDelivery(notification_id=item.id, channel="push", recipient=subscription.id)
        session.add(delivery)
        session.flush()
        identifier = delivery.id
    service.deliver_pending()
    with db.transaction() as session:
        delivery = session.get(NotificationDelivery, identifier)
        assert (delivery.state, delivery.last_error) == ("blocked", "recipient_not_authorized")


def test_legacy_broadcast_email_is_blocked_when_telegram_is_bound(notification_context):
    from types import SimpleNamespace

    db, settings, (owner, _, _) = notification_context
    sent = []
    service = NotificationService(
        db, settings, mail_transport=SimpleNamespace(deliver=lambda *args: sent.append(args))
    )
    with db.transaction() as session:
        item = service.emit_in(
            session, owner, "broadcast:legacy-email", "broadcast", "Рассылка", "Новость", channel="inbox"
        )
        delivery = NotificationDelivery(
            notification_id=item.id, channel="email", recipient="owner@example.test"
        )
        session.add(delivery)
        session.flush()
        identifier = delivery.id
    service.deliver_pending()
    assert sent == []
    with db.transaction() as session:
        delivery = session.get(NotificationDelivery, identifier)
        assert (delivery.state, delivery.last_error) == ("blocked", "recipient_not_authorized")
