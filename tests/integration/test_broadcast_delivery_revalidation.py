"""Durable media cannot degrade to text or outlive current delivery authority."""

import httpx
import pytest
from sqlalchemy import select
from sunrise_infrastructure.broadcasts import BroadcastService
from sunrise_infrastructure.db import Account, Notification, NotificationDelivery, Setting
from sunrise_infrastructure.notifications import NotificationService

from tests.integration.test_broadcast_media_registry import png, registry
from tests.integration.test_node_news_and_crm import routed as routed
from tests.integration.test_subscription_engagement import paid_creation as paid_creation
from tests.integration.test_v11_panel_events import owned_panel as owned_panel


@pytest.fixture
def expanded_media(routed, tmp_path, monkeypatch):
    context, _ = routed
    db, _, commerce, config, _, _, aid, *_ = context
    media_registry = registry(context, tmp_path)
    monkeypatch.setattr(
        "sunrise_infrastructure.broadcast_media_registry.configured_media_store",
        lambda config: media_registry.store,
    )
    reference = media_registry.register(aid, [png()], "image/png")
    config.bot_test_token, config.test_bot_ids = "123456789:synthetic-token", "123456789"
    with db.transaction() as session:
        for delivery in session.scalars(select(NotificationDelivery)):
            delivery.state = "cancelled"
    broadcasts = BroadcastService(commerce, config)
    job = broadcasts.create(
        aid,
        [{"type": "text", "text": "Synthetic scoped media"}],
        "Synthetic authority test",
        draft=False,
        media=reference,
    )
    assert broadcasts.expand_pending() == 1
    with db.transaction() as session:
        item = session.scalar(select(Notification).where(Notification.event_key == "broadcast:" + job["id"]))
        delivery = session.scalar(
            select(NotificationDelivery).where(NotificationDelivery.notification_id == item.id)
        )
        nid, did = item.id, delivery.id
    return context, broadcasts, job["id"], nid, did


def test_missing_durable_media_marker_never_sends_text_fallback(expanded_media):
    context, _, _, nid, did = expanded_media
    db, _, _, config, *_ = context
    with db.transaction() as session:
        session.delete(session.get(Setting, "notification_media:" + nid))
    calls = []
    notifier = NotificationService(db, config, telegram_transport=lambda *args: calls.append(args))
    assert notifier.deliver_pending() == 0
    assert calls == []
    with db.transaction() as session:
        delivery = session.get(NotificationDelivery, did)
        assert delivery.state == "review" and delivery.last_error


@pytest.mark.parametrize(
    "change", ["staff_telegram", "staff_allowlist", "job_stop", "recipient", "claim", "marker", "asset"]
)
def test_telegram_media_rechecks_after_identity_network_wait(expanded_media, monkeypatch, change):
    context, broadcasts, jid, nid, did = expanded_media
    db, _, _, config, _, _, aid, *_ = context
    calls = []

    def handler(request):
        calls.append(request.method)
        if request.method == "GET":
            if change == "job_stop":
                broadcasts.stop(aid, jid, "Synthetic stop during identity wait")
            else:
                with db.transaction() as session:
                    if change == "staff_telegram":
                        session.get(Account, aid).telegram_id = None
                    elif change == "staff_allowlist":
                        config.owner_id = None
                        config.admin_telegram_allowlist = []
                    elif change == "recipient":
                        session.get(Account, aid).telegram_notifications_blocked = True
                    elif change == "claim":
                        session.get(NotificationDelivery, did).state = "cancelled"
                    elif change == "marker":
                        session.delete(session.get(Setting, "notification_media:" + nid))
                    else:
                        marker = session.get(Setting, "notification_media:" + nid)
                        session.delete(
                            session.get(Setting, "media_asset:" + marker.value["media"]["asset_id"])
                        )
            return httpx.Response(200, json={"ok": True, "result": {"id": 123456789}})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    original = httpx.Client
    monkeypatch.setattr(
        "sunrise_infrastructure.notifications.httpx.Client",
        lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handler)),
    )
    assert NotificationService(db, config).deliver_pending() == 0
    assert calls == ["GET"]
    with db.transaction() as session:
        delivery = session.get(NotificationDelivery, did)
        assert delivery.state in {"review", "cancelled"} and delivery.delivered_at is None


@pytest.mark.parametrize("change", ["staff_email", "job_stop", "recipient", "claim", "marker"])
def test_smtp_media_rechecks_after_authenticated_network_wait(expanded_media, change):
    from sunrise_infrastructure.broadcast_media_registry import configured_media_store
    from sunrise_infrastructure.mail import SMTPTestMail

    context, broadcasts, _, _, old_did = expanded_media
    db, _, _, config, _, _, aid, *_ = context
    config.email_allowlist = ["events-owner@example.test"]
    with db.transaction() as session:
        session.get(NotificationDelivery, old_did).state = "cancelled"
        reference = session.get(
            Setting, "notification_media:" + session.get(NotificationDelivery, old_did).notification_id
        ).value["media"]
    job = broadcasts.test_self(
        aid,
        [{"type": "text", "text": "Synthetic email authority"}],
        "Synthetic email wait test",
        media=reference,
    )
    with db.transaction() as session:
        notice = session.scalar(
            select(Notification).where(Notification.event_key == "broadcast:" + job["id"])
        )
        delivery = session.scalar(
            select(NotificationDelivery).where(NotificationDelivery.notification_id == notice.id)
        )
        nid, did = notice.id, delivery.id
        assert delivery.channel == "email"
    events = []

    class SMTP:
        esmtp_features = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def ehlo(self):
            events.append("ehlo")

        def starttls(self, **kwargs):
            events.append("tls")

        def login(self, *args):
            events.append("login")
            with db.transaction() as session:
                if change == "staff_email":
                    session.get(Account, aid).email_verified = False
                elif change == "job_stop":
                    from sunrise_infrastructure.db import Job

                    session.get(Job, job["id"]).state = "stopped"
                elif change == "recipient":
                    config.email_allowlist = []
                elif change == "claim":
                    session.get(NotificationDelivery, did).state = "cancelled"
                else:
                    session.delete(session.get(Setting, "notification_media:" + nid))

        def send_message(self, *args, **kwargs):
            events.append("send")
            return {}

    transport = SMTPTestMail(
        host="smtp.example.test",
        port=587,
        mode="starttls",
        sender="sender@example.test",
        username="synthetic",
        password="synthetic",  # noqa: S106 - synthetic SMTP double, no external connection
        allowed_hosts=["smtp.example.test"],
        allowed_recipients=config.email_allowlist,
        authorized=True,
        factory=lambda **kwargs: SMTP(),
        media_store=configured_media_store(config),
    )
    assert NotificationService(db, config, mail_transport=transport).deliver_pending() == 0
    assert events == ["ehlo", "tls", "ehlo", "login"]
    with db.transaction() as session:
        delivery = session.get(NotificationDelivery, did)
        assert delivery.state in {"review", "cancelled"} and delivery.delivered_at is None
