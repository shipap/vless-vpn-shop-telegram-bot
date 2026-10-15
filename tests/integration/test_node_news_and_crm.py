"""Node news is scoped local chronology; CRM48h is verified owner-only notification."""

import hashlib
import hmac
import json

import httpx
import pytest
from sqlalchemy import func, select
from sunrise_infrastructure.db import (
    Account,
    Alias,
    Notification,
    NotificationDelivery,
    PanelEvent,
    Setting,
    StaffRole,
    now,
)
from sunrise_infrastructure.notifications import NotificationService
from sunrise_infrastructure.panel_events import PanelEvents

from tests.integration.test_subscription_engagement import paid_creation as paid_creation
from tests.integration.test_v11_panel_events import SECRET, TG
from tests.integration.test_v11_panel_events import owned_panel as owned_panel
from tests.unit.test_panel_event_routes import policy


def signed(kind, data, *, stamp=None):
    stamp = stamp or now().isoformat()
    raw = json.dumps(
        {"scope": kind.split(".")[0], "event": kind, "timestamp": stamp, "data": data}, separators=(",", ":")
    ).encode()
    return raw, {
        "x-remnawave-signature": hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest(),
        "x-remnawave-timestamp": stamp,
    }


@pytest.fixture
def routed(paid_creation):
    db, panel, commerce, config, state, _, aid, sid, _ = paid_creation
    commerce.provisioning.run_jobs()
    config.owner_id, config.admin_email_allowlist = TG, ["events-owner@example.test"]
    config.admin_telegram_allowlist = []
    node_id = policy()["nodes"][0]["node_uuid"]
    config.panel_test_node_uuids = [node_id]
    panel.nodes.add(node_id)
    with db.transaction() as session:
        account = session.get(Account, aid)
        account.email, account.email_verified = "events-owner@example.test", True
        session.add(StaffRole(account_id=aid, role="owner"))
        session.add(Setting(key="panel_event_routes", value=policy()))
    transport = panel.client._transport
    node = {
        "uuid": node_id,
        "name": "private-infrastructure-name",
        "isConnected": False,
        "isDisabled": False,
        "countryCode": "ZZ",
        "address": "192.0.2.99",
    }

    def handler(request):
        if request.url.path == "/api/nodes/" + node_id:
            state["calls"].append((request.method, request.url.path))
            return httpx.Response(200, json={"response": node})
        return transport.handle_request(request)

    panel.client._transport = httpx.MockTransport(handler)
    yield paid_creation, node


def test_node_outage_recovery_is_safe_owned_news_without_notification(routed):
    context, node = routed
    db, panel, commerce, config, state, _, aid, *_ = context
    service = PanelEvents(db, panel, config)
    with db.transaction() as session:
        before_deliveries = session.scalar(select(func.count()).select_from(NotificationDelivery))
    assert service.accept(*signed("node.connection_lost", node))["status"] == "accepted"
    assert service.reconcile_pending() == 1
    node["isConnected"] = True
    assert service.accept(*signed("node.connection_restored", node))["status"] == "accepted"
    assert service.reconcile_pending() == 1
    feed = commerce.node_news(aid)
    assert [item["state"] for item in feed["items"]] == ["recovered", "outage"]
    serialized = json.dumps(feed, ensure_ascii=False)
    assert (
        "Тестовый сервер" in serialized
        and "192.0.2.99" not in serialized
        and node["uuid"] not in serialized
        and node["name"] not in serialized
    )
    with db.transaction() as session:
        assert (
            session.scalar(
                select(Notification.id).where(Notification.kind.in_(["node_outage", "node_recovered"]))
            )
            is None
        )
        assert session.scalar(select(func.count()).select_from(NotificationDelivery)) == before_deliveries


def test_crm48h_routes_only_explicit_owner_and_drops_private_url(routed):
    context, _ = routed
    db, panel, _, config, _, _, aid, *_ = context
    service = PanelEvents(db, panel, config)
    data = {
        "providerName": "synthetic-provider",
        "nodeName": "synthetic-infra",
        "nextBillingAt": now().isoformat(),
        "loginUrl": "https://billing.private.example.test/token?secret=do-not-store",
    }
    raw, headers = signed("crm.infra_billing_node_payment_in_48hrs", data)
    assert service.accept(raw, headers)["status"] == "accepted"
    assert service.reconcile_pending() == 1
    assert service.accept(raw, headers)["status"] == "duplicate"
    with db.transaction() as session:
        notice = session.scalar(select(Notification).where(Notification.kind == "infra_billing_due"))
        assert notice.account_id == aid and notice.audience == "owner"
        assert "неизвестна" in notice.body and "https://" not in notice.body
        assert (
            len(
                list(
                    session.scalars(
                        select(NotificationDelivery).where(NotificationDelivery.notification_id == notice.id)
                    )
                )
            )
            == 1
        )
        payloads = [row.payload for row in session.scalars(select(PanelEvent))]
        assert "loginUrl" not in json.dumps(payloads) and "do-not-store" not in json.dumps(payloads)


@pytest.mark.parametrize("wrong", ["missing", "different_case", "other_crm"])
def test_crm_missing_or_other_mapping_is_ignored(routed, wrong):
    context, _ = routed
    db, panel, _, config, *_ = context
    if wrong == "missing":
        with db.transaction() as session:
            session.delete(session.get(Setting, "panel_event_routes"))
    data = {
        "providerName": "Synthetic-Provider" if wrong == "different_case" else "synthetic-provider",
        "nodeName": "synthetic-infra",
        "nextBillingAt": now().isoformat(),
        "loginUrl": "private",
    }
    kind = (
        "crm.infra_billing_node_payment_in_24hrs"
        if wrong == "other_crm"
        else "crm.infra_billing_node_payment_in_48hrs"
    )
    events = PanelEvents(db, panel, config)
    assert events.accept(*signed(kind, data))["status"] == "ignored"
    assert events.reconcile_pending() == 0
    with db.transaction() as session:
        assert session.scalar(select(Notification.id).where(Notification.kind == "infra_billing_due")) is None


def test_mapping_registration_is_owner_only_and_audited(routed):
    from sunrise_infrastructure.admin import Admin
    from sunrise_infrastructure.db import Audit

    context, _ = routed
    db, _, commerce, _, _, _, aid, *_ = context
    admin = Admin(commerce, None, commerce.accounts)
    value = policy()
    value["nodes"][0]["safe_label"] = "Публичный сервер"
    assert admin.save_panel_event_routes(aid, value, "Reviewed synthetic project routes") == value
    with db.transaction() as session:
        assert session.get(Setting, "panel_event_routes").value == value

        assert session.scalar(select(Audit.id).where(Audit.action == "panel_event_routes_update")) is not None
        session.get(Account, aid).telegram_id = None
    from sunrise_core.errors import DomainError

    with pytest.raises(DomainError):
        admin.save_panel_event_routes(aid, policy(), "Denied synthetic registration")
    with db.transaction() as session:
        assert session.get(Setting, "panel_event_routes").value == value


@pytest.mark.parametrize("revoked", ["telegram", "role"])
def test_crm_owner_authority_is_rechecked_after_signed_acceptance(routed, revoked):
    context, _ = routed
    db, panel, _, config, _, _, aid, *_ = context
    service = PanelEvents(db, panel, config)
    data = {
        "providerName": "synthetic-provider",
        "nodeName": "synthetic-infra",
        "nextBillingAt": now().isoformat(),
        "loginUrl": "private-do-not-store",
    }
    assert service.accept(*signed("crm.infra_billing_node_payment_in_48hrs", data))["status"] == "accepted"
    with db.transaction() as session:
        if revoked == "telegram":
            session.get(Account, aid).telegram_id = None
        elif revoked == "role":
            session.delete(session.get(StaffRole, aid))
    service.reconcile_pending()
    with db.transaction() as session:
        assert session.scalar(select(Notification.id).where(Notification.kind == "infra_billing_due")) is None


def canonical_owner(context, *, mode="web", invalid=None):
    """Keep paid panel ownership intact while preparing a separate canonical staff owner."""
    db, _, _, config, _, _, aid, *_ = context
    owner_id = 9007199254740993124
    email = "canonical-owner@example.test"
    linked = int(TG) + 1
    config.owner_id = TG if mode == "configured_other" else None
    config.admin_telegram_allowlist = [str(linked)]
    config.admin_email_allowlist = [email]
    config.email_allowlist = [email]
    config.test_telegram_recipients = TG + "," + str(linked)
    with db.transaction() as session:
        old_grant = session.get(StaffRole, aid)
        if old_grant:
            session.delete(old_grant)
        owner = Account(
            id=owner_id,
            email=email,
            email_verified=True,
            telegram_id=None if invalid == "unbound_telegram" else linked,
        )
        session.add(owner)
        session.flush()
        if invalid != "missing_grant":
            session.add(StaffRole(account_id=owner_id, role="admin" if invalid == "admin" else "owner"))
        if invalid == "unallowlisted":
            config.admin_telegram_allowlist = []
        elif invalid == "alias":
            session.add(Alias(old_id=owner_id, canonical_id=aid))
        elif invalid == "ambiguous":
            second = Account(
                id=owner_id + 1,
                email="other-owner@example.test",
                email_verified=True,
                telegram_id=linked + 1,
            )
            session.add(second)
            session.flush()
            session.add(StaffRole(account_id=second.id, role="owner"))
            config.admin_telegram_allowlist.append(str(linked + 1))
    return owner_id


def accept_crm(context, *, reconciled=1):
    db, panel, _, config, *_ = context
    events = PanelEvents(db, panel, config)
    raw, headers = signed(
        "crm.infra_billing_node_payment_in_48hrs",
        {
            "providerName": "synthetic-provider",
            "nodeName": "synthetic-infra",
            "nextBillingAt": now().isoformat(),
            "loginUrl": "https://billing.example.test/private-do-not-store",
        },
    )
    assert events.accept(raw, headers)["status"] == "accepted"
    assert events.reconcile_pending() == reconciled
    assert events.accept(raw, headers)["status"] == "duplicate"
    return events


@pytest.mark.parametrize("mode", ["web", "linked", "configured_other"])
def test_crm_allowlisted_canonical_owner_does_not_require_configured_owner_id(routed, mode):
    context, _ = routed
    db, _, _, config, _, _, aid, *_ = context
    owner_id = canonical_owner(context, mode=mode)
    accept_crm(context)
    notifier = NotificationService(db, config)
    inbox = notifier.list(owner_id)["items"]
    assert [item["kind"] for item in inbox] == ["infra_billing_due"]
    assert not any(item["kind"] == "infra_billing_due" for item in notifier.list(aid)["items"])
    with db.transaction() as session:
        notices = list(session.scalars(select(Notification).where(Notification.kind == "infra_billing_due")))
        assert len(notices) == 1
        notice = notices[0]
        assert notice.account_id == owner_id and notice.audience == "owner"
        assert "неизвестна" in notice.body and "https://" not in notice.body
        deliveries = list(
            session.scalars(
                select(NotificationDelivery).where(NotificationDelivery.notification_id == notice.id)
            )
        )
        assert [(item.channel, item.recipient) for item in deliveries] == [("telegram", str(int(TG) + 1))]
        intents = list(session.scalars(select(Setting).where(Setting.key.like("infra_notice:%"))))
        assert len(intents) == 1 and intents[0].value["owner_id"] == str(owner_id)


@pytest.mark.parametrize(
    "invalid", ["admin", "missing_grant", "unbound_telegram", "unallowlisted", "alias", "ambiguous"]
)
def test_crm_canonical_owner_selection_fails_closed(routed, invalid):
    context, _ = routed
    db, *_ = context
    canonical_owner(context, invalid=invalid)
    accept_crm(context, reconciled=0)
    with db.transaction() as session:
        assert session.scalar(select(Notification.id).where(Notification.kind == "infra_billing_due")) is None
        assert session.scalar(select(Setting.key).where(Setting.key.like("infra_notice:%"))) is None


@pytest.mark.parametrize("invalid", ["unbound_telegram", "unallowlisted", "alias"])
def test_crm_invalid_additional_grants_do_not_hide_one_canonical_owner(routed, invalid):
    context, _ = routed
    db, _, _, config, _, _, aid, *_ = context
    owner_id = canonical_owner(context)
    with db.transaction() as session:
        for index in range(2):
            linked = int(TG) + 10 + index
            extra = Account(
                id=9007199254740993101 + index,
                email=f"invalid-owner-{index}@example.test",
                email_verified=True,
                telegram_id=None if invalid == "unbound_telegram" else linked,
            )
            session.add(extra)
            session.flush()
            session.add(StaffRole(account_id=extra.id, role="owner"))
            if invalid != "unallowlisted":
                config.admin_telegram_allowlist.append(str(linked))
            if invalid == "alias":
                session.add(Alias(old_id=extra.id, canonical_id=aid))
    accept_crm(context)
    with db.transaction() as session:
        notices = list(session.scalars(select(Notification).where(Notification.kind == "infra_billing_due")))
        assert len(notices) == 1 and notices[0].account_id == owner_id
        recipients = list(
            session.scalars(
                select(NotificationDelivery.recipient).where(
                    NotificationDelivery.notification_id == notices[0].id
                )
            )
        )
        assert recipients == [str(int(TG) + 1)]


def test_crm_owner_id_removal_preserves_allowlisted_canonical_database_owner(routed):
    context, _ = routed
    db, panel, _, config, _, _, aid, *_ = context
    events = PanelEvents(db, panel, config)
    assert (
        events.accept(
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
    config.admin_telegram_allowlist = [TG]
    config.owner_id = None
    assert events.reconcile_pending() == 1
    with db.transaction() as session:
        notice = session.scalar(select(Notification).where(Notification.kind == "infra_billing_due"))
        assert notice is not None and notice.account_id == aid and notice.audience == "owner"


@pytest.mark.parametrize("mode", ["web", "linked", "configured_other"])
@pytest.mark.parametrize("revoked", [False, True])
def test_canonical_crm_owner_authority_is_fresh_after_transport_wait(routed, monkeypatch, mode, revoked):
    from sunrise_infrastructure.mail import SMTPTestMail

    context, _ = routed
    db, _, _, config, *_ = context
    owner_id = canonical_owner(context, mode=mode)
    channel = "email" if mode == "web" else "telegram"
    with db.transaction() as session:
        for item in session.scalars(select(NotificationDelivery)):
            item.state = "cancelled"
    accept_crm(context)
    if channel == "email":
        # Preserve an already queued email channel for a now Telegram-linked canonical owner.
        with db.transaction() as session:
            delivery = session.scalar(
                select(NotificationDelivery)
                .join(Notification)
                .where(Notification.kind == "infra_billing_due", Notification.account_id == owner_id)
            )
            assert delivery is not None
            delivery.channel, delivery.recipient = "email", "canonical-owner@example.test"
    calls = []

    def revoke_owner():
        if revoked:
            with db.transaction() as session:
                session.delete(session.get(StaffRole, owner_id))

    if channel == "telegram":
        config.bot_test_token, config.test_bot_ids = "123456789:synthetic-token", "123456789"

        def handler(request):
            calls.append(request.method)
            if request.method == "GET":
                assert request.url.path.endswith("/getMe")
                revoke_owner()
                return httpx.Response(200, json={"ok": True, "result": {"id": 123456789}})
            assert request.url.path.endswith("/sendMessage")
            assert json.loads(request.content)["chat_id"] == str(int(TG) + 1)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

        original = httpx.Client
        monkeypatch.setattr(
            "sunrise_infrastructure.notifications.httpx.Client",
            lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handler)),
        )
        notifier = NotificationService(db, config)
    else:

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
                revoke_owner()

            def send_message(self, message, **kwargs):
                assert message["To"] == "canonical-owner@example.test"
                calls.append("send")
                return {}

        transport = SMTPTestMail(
            host="smtp.example.test",
            port=587,
            mode="starttls",
            sender="sender@example.test",
            username="synthetic",
            password="synthetic",  # noqa: S106 - synthetic SMTP factory
            allowed_hosts=["smtp.example.test"],
            allowed_recipients=config.email_allowlist,
            authorized=True,
            factory=lambda **kwargs: SMTP(),
        )
        notifier = NotificationService(db, config, mail_transport=transport)
    assert notifier.deliver_pending() == (0 if revoked else 1)
    assert notifier.deliver_pending() == 0
    assert calls == (
        ["GET"] + ([] if revoked else ["POST"])
        if channel == "telegram"
        else ["ehlo", "tls", "ehlo", "login"] + ([] if revoked else ["send"])
    )
    with db.transaction() as session:
        notice = session.scalar(select(Notification).where(Notification.kind == "infra_billing_due"))
        delivery = session.scalar(
            select(NotificationDelivery).where(NotificationDelivery.notification_id == notice.id)
        )
        assert (delivery.delivered_at is not None) == (not revoked)


@pytest.mark.parametrize("locked", ["account", "role"])
def test_crm_busy_owner_retries_without_partial_intent(routed, locked):
    from concurrent.futures import ThreadPoolExecutor
    from threading import current_thread

    from sqlalchemy import event

    context, _ = routed
    db, panel, _, config, _, _, aid, *_ = context
    events = PanelEvents(db, panel, config)
    assert (
        events.accept(
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

    def bound_wait(connection):
        if current_thread().name.startswith("crm-owner-busy"):
            connection.exec_driver_sql("SET LOCAL lock_timeout = '500ms'")

    def reconcile():
        try:
            return events.reconcile_pending()
        except Exception as error:  # noqa: BLE001 - assert real bounded outcome without credential-bearing text
            return error

    event.listen(db.engine, "begin", bound_wait)
    try:
        with db.transaction() as holding:
            query = (
                select(Account).where(Account.id == aid)
                if locked == "account"
                else select(StaffRole).where(StaffRole.account_id == aid)
            )
            holding.scalar(query.with_for_update())
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="crm-owner-busy") as executor:
                result = executor.submit(reconcile).result(timeout=5)
    finally:
        event.remove(db.engine, "begin", bound_wait)
    assert not isinstance(result, Exception), (
        type(result).__name__,
        getattr(getattr(result, "orig", None), "sqlstate", None),
    )
    assert result == 0
    with db.transaction() as session:
        assert session.scalar(select(Notification.id).where(Notification.kind == "infra_billing_due")) is None
        assert session.scalar(select(Setting.key).where(Setting.key.like("infra_notice:%"))) is None
        pending = session.scalar(
            select(PanelEvent).where(PanelEvent.kind == "crm.infra_billing_node_payment_in_48hrs")
        )
        assert pending.state == "pending" and pending.last_error == "panel_unavailable"
        assert pending.available_at > now()
        pending.available_at = now()
    assert events.reconcile_pending() == 1
    assert events.reconcile_pending() == 0
    with db.transaction() as session:
        notices = list(session.scalars(select(Notification).where(Notification.kind == "infra_billing_due")))
        assert len(notices) == 1 and notices[0].account_id == aid
        assert (
            session.scalar(
                select(NotificationDelivery.id).where(NotificationDelivery.notification_id == notices[0].id)
            )
            is not None
        )


@pytest.mark.parametrize("delivery", ["coalesced", "delayed"])
def test_node_signed_chronology_retains_outage_after_confirmed_recovery(routed, delivery):
    from datetime import timedelta

    from sunrise_infrastructure.db import PanelObservation

    context, node = routed
    db, panel, commerce, config, _, _, aid, *_ = context
    service = PanelEvents(db, panel, config)
    outage = (now() - timedelta(seconds=20)).isoformat()
    recovered = (now() - timedelta(seconds=10)).isoformat()
    node["isConnected"] = True
    if delivery == "coalesced":
        assert service.accept(*signed("node.connection_lost", node, stamp=outage))["status"] == "accepted"
    assert service.accept(*signed("node.connection_restored", node, stamp=recovered))["status"] == "accepted"
    assert service.reconcile_pending() == (2 if delivery == "coalesced" else 1)
    if delivery == "delayed":
        assert service.accept(*signed("node.connection_lost", node, stamp=outage))["status"] == "accepted"
        assert service.reconcile_pending() == 1
    feed = commerce.node_news(aid, limit=1)
    assert feed["items"][0]["state"] == "recovered" and feed["next_cursor"]
    older = commerce.node_news(aid, before=feed["next_cursor"], limit=1)
    assert older["items"][0]["state"] == "outage" and older["next_cursor"] is None
    assert older["items"][0]["occurred_at"] == outage
    with db.transaction() as session:
        observation = session.scalar(select(PanelObservation).where(PanelObservation.kind == "node"))
        assert observation.status == "CONNECTED"
        assert (
            session.scalar(
                select(Notification.id).where(Notification.kind.in_(["node_outage", "node_recovered"]))
            )
            is None
        )
        foreign = Account(email="foreign-node-news@example.test")
        session.add(foreign)
        session.flush()
        foreign_id = foreign.id
    assert commerce.node_news(foreign_id)["items"] == []
