"""Owner-only text delivery must retain current authority after network waits."""

import httpx
import pytest
from sqlalchemy import select
from sunrise_infrastructure.db import Account, Notification, NotificationDelivery, StaffRole, now
from sunrise_infrastructure.mail import SMTPTestMail
from sunrise_infrastructure.notifications import NotificationService
from sunrise_infrastructure.panel_events import PanelEvents

from tests.integration.test_node_news_and_crm import routed as routed
from tests.integration.test_node_news_and_crm import signed
from tests.integration.test_subscription_engagement import paid_creation as paid_creation
from tests.integration.test_v11_panel_events import owned_panel as owned_panel


def revoke(db, config, aid, did, change, *, channel):
    if change == "unchanged":
        return
    with db.transaction() as session:
        if change == "owner_role":
            session.get(StaffRole, aid).role = "admin"
            config.owner_id = None
        elif change == "verified_email":
            session.get(Account, aid).email_verified = False
        elif change == "telegram_identity":
            session.get(Account, aid).telegram_id = None
        elif change == "allowlist":
            config.owner_id = None
            config.admin_telegram_allowlist = []
        elif change == "recipient":
            if channel == "telegram":
                session.get(Account, aid).telegram_notifications_blocked = True
            else:
                session.get(Account, aid).email = "new-owner@example.test"
        else:
            session.get(NotificationDelivery, did).state = "cancelled"


@pytest.mark.parametrize(
    "change", ["owner_role", "telegram_identity", "allowlist", "recipient", "claim", "unchanged"]
)
def test_crm_owner_text_revalidates_after_getme(routed, monkeypatch, change):
    context, _ = routed
    db, panel, _, config, _, _, aid, *_ = context
    with db.transaction() as session:
        for delivery in session.scalars(select(NotificationDelivery)):
            delivery.state = "cancelled"
    service = PanelEvents(db, panel, config)
    assert (
        service.accept(
            *signed(
                "crm.infra_billing_node_payment_in_48hrs",
                {
                    "providerName": "synthetic-provider",
                    "nodeName": "synthetic-infra",
                    "nextBillingAt": now().isoformat(),
                    "loginUrl": "https://billing.example.test/synthetic",
                },
            )
        )["status"]
        == "accepted"
    )
    assert service.reconcile_pending() == 1
    with db.transaction() as session:
        notice = session.scalar(select(Notification).where(Notification.kind == "infra_billing_due"))
        delivery = session.scalar(
            select(NotificationDelivery).where(NotificationDelivery.notification_id == notice.id)
        )
        did = delivery.id
        assert notice.audience == "owner" and delivery.channel == "telegram"
    config.bot_test_token, config.test_bot_ids = "123456789:synthetic-token", "123456789"
    calls = []

    def handler(request):
        calls.append(request.method)
        if request.method == "GET":
            revoke(db, config, aid, did, change, channel="telegram")
            return httpx.Response(200, json={"ok": True, "result": {"id": 123456789}})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    original = httpx.Client
    monkeypatch.setattr(
        "sunrise_infrastructure.notifications.httpx.Client",
        lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handler)),
    )
    notifier = NotificationService(db, config)
    assert notifier.deliver_pending() == (1 if change == "unchanged" else 0)
    assert notifier.deliver_pending() == 0
    assert calls == (["GET", "POST"] if change == "unchanged" else ["GET"])
    with db.transaction() as session:
        assert (session.get(NotificationDelivery, did).delivered_at is not None) == (change == "unchanged")


@pytest.mark.parametrize(
    "change", ["owner_role", "verified_email", "allowlist", "recipient", "claim", "unchanged"]
)
def test_owner_text_email_revalidates_after_smtp_login(routed, change):
    context, _ = routed
    db, _, _, config, _, _, aid, *_ = context
    config.email_allowlist = ["events-owner@example.test"]
    with db.transaction() as session:
        for delivery in session.scalars(select(NotificationDelivery)):
            delivery.state = "cancelled"
        notice = NotificationService(db, config).emit_in(
            session,
            aid,
            "synthetic-owner-email-review",
            "identity_review",
            "Проверка владельца",
            "Синтетическое закрытое уведомление.",
            channel="email",
            owner_only=True,
            allow_push=False,
        )
        session.flush()
        did = session.scalar(
            select(NotificationDelivery.id).where(NotificationDelivery.notification_id == notice.id)
        )
    calls = []

    class SMTP:
        esmtp_features = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def ehlo(self):
            calls.append("ehlo")

        def starttls(self, **kwargs):
            calls.append("tls")

        def login(self, *args):
            calls.append("login")
            revoke(db, config, aid, did, change, channel="email")

        def send_message(self, *args, **kwargs):
            calls.append("send")
            return {}

    transport = SMTPTestMail(
        host="smtp.example.test",
        port=587,
        mode="starttls",
        sender="sender@example.test",
        username="synthetic",
        password="synthetic",  # noqa: S106 - synthetic SMTP factory, no external request
        allowed_hosts=["smtp.example.test"],
        allowed_recipients=config.email_allowlist,
        authorized=True,
        factory=lambda **kwargs: SMTP(),
    )
    notifier = NotificationService(db, config, mail_transport=transport)
    assert notifier.deliver_pending() == (1 if change == "unchanged" else 0)
    assert notifier.deliver_pending() == 0
    assert calls == ["ehlo", "tls", "ehlo", "login"] + (["send"] if change == "unchanged" else [])
    with db.transaction() as session:
        assert (session.get(NotificationDelivery, did).delivered_at is not None) == (change == "unchanged")
