"""Original paid numeric creation and fresh connection proof drive one durable help intent."""

import importlib
from datetime import timedelta
from types import SimpleNamespace

import fakeredis
import httpx
import pytest
from sqlalchemy import select
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import Job, Notification, Product, Setting, now
from sunrise_infrastructure.settings import Settings

from tests.integration.test_v11_panel_events import (
    SECRET,
    SQUAD,
    TG,
)
from tests.integration.test_v11_panel_events import (
    owned_panel as owned_panel,
)
from tests.legal_fixture import accept_test_legal


@pytest.fixture
def paid_creation(owned_panel):
    db, panel, _, state, aid, _, old_job = owned_panel
    clock = {"now": now()}
    original = panel.client._transport

    def handler(request):
        try:
            response = original.handle_request(request)
        finally:
            if request.method == "POST" and state["user"] is not None:
                state["user"].update(
                    createdAt=clock["now"].isoformat(),
                    userTraffic={
                        "usedTrafficBytes": 0,
                        "lifetimeUsedTrafficBytes": 0,
                        "firstConnectedAt": None,
                        "onlineAt": None,
                    },
                )
        if response.status_code == 200 and request.url.path.startswith("/api/users"):
            return httpx.Response(200, json={"response": state["user"]})
        return response

    panel.client._transport = httpx.MockTransport(handler)
    config = Settings(
        _env_file=None,
        secret_key="s" * 48,
        database_url=db.engine.url.render_as_string(hide_password=False),
        local_test=True,
        panel_contract="3.4_id",
        panel_webhook_test_authorized=True,
        panel_webhook_secret=SECRET,
        panel_subscription_allowed_hosts=["subscription.example.test"],
        panel_test_user_ids=[],
        panel_test_node_uuids=[],
        admin_email_allowlist=[],
        email_allowlist=[],
        owner_id=None,
        test_telegram_recipients=TG,
    )
    commerce = Commerce(db, local_test=True, panel_provider=panel, public_settings=config)
    from sunrise_infrastructure.accounts import Accounts

    commerce.accounts = Accounts(db, SimpleNamespace(redis=fakeredis.FakeRedis()), config)
    with db.transaction() as session:
        session.get(Job, old_job).state = "done"
        for product in session.scalars(select(Product)):
            product.internal_squad_uuid = SQUAD if product.id == "standard" else None
    accept_test_legal(commerce, aid)
    quote = commerce.quote(aid, "standard", 30, "purchase")
    order = commerce.create_order(aid, quote["id"], "paid-onboarding-new-user")
    assert commerce.test_pay(aid, order["id"])["status"] == "paid"
    sid = commerce.order(aid, order["id"])["subscription_id"]
    clock["now"] = now()
    yield db, panel, commerce, config, state, clock, aid, sid, order["id"]


def engagement(context, monkeypatch, *, advance=timedelta(0)):
    module = importlib.import_module("sunrise_infrastructure.subscription_engagement")
    clock = context[5]
    clock["now"] += advance
    for name in (
        "subscription_engagement",
        "panel_events",
        "commerce",
        "retention",
        "notifications",
        "provisioning",
    ):
        monkeypatch.setattr("sunrise_infrastructure." + name + ".now", lambda: clock["now"])
    return module.SubscriptionEngagement(context[2])


def projection(context):
    return next(
        item for item in context[2].dashboard(context[6])["subscriptions"] if item["id"] == context[7]
    )


def test_help_exact_24h_from_original_paid_creation_once(paid_creation, monkeypatch):
    context = paid_creation
    service = engagement(context, monkeypatch)
    assert context[2].provisioning.run_jobs() == 1
    created = context[5]["now"]
    context[5]["now"] = created + timedelta(hours=24, microseconds=-1)
    assert service.run_due() == 0
    assert projection(context)["connection_help_due"] is False
    context[5]["now"] = created + timedelta(hours=24)
    assert service.run_due() == 1
    assert projection(context)["connection_help_due"] is True
    assert service.run_due() == 0
    with context[0].transaction() as session:
        notices = list(session.scalars(select(Notification).where(Notification.kind == "connection_help")))
        assert len(notices) == 1
        marker = session.get(Setting, "engagement:" + context[7]).value
        assert marker["help_intent_id"] == notices[0].id


@pytest.mark.parametrize("missing", ["firstConnectedAt", "onlineAt", "lifetimeUsedTrafficBytes"])
def test_unknown_connection_field_never_emits_help(paid_creation, monkeypatch, missing):
    context = paid_creation
    service = engagement(context, monkeypatch)
    assert context[2].provisioning.run_jobs() == 1
    context[5]["now"] += timedelta(hours=25)
    del context[4]["user"]["userTraffic"][missing]
    service.run_due()
    assert projection(context)["connection_help_due"] is False
    with context[0].transaction() as session:
        assert session.scalar(select(Notification).where(Notification.kind == "connection_help")) is None


def test_first_connection_is_sticky_after_late_null_reset_snapshot(paid_creation, monkeypatch):
    context = paid_creation
    service = engagement(context, monkeypatch)
    context[2].provisioning.run_jobs()
    context[5]["now"] += timedelta(hours=25)
    assert service.run_due() == 1
    context[4]["user"]["userTraffic"].update(
        firstConnectedAt=context[5]["now"].isoformat(), lifetimeUsedTrafficBytes=4096
    )
    context[2].dashboard(context[6], refresh_panel=True)
    assert projection(context)["ever_connected"] is True
    assert projection(context)["connection_help_due"] is False
    context[4]["user"]["userTraffic"].update(firstConnectedAt=None, lifetimeUsedTrafficBytes=0)
    context[2].dashboard(context[6], refresh_panel=True)
    assert projection(context)["ever_connected"] is True
    assert projection(context)["connection_help_due"] is False
    with context[0].transaction() as session:
        assert (
            len(list(session.scalars(select(Notification).where(Notification.kind == "connection_success"))))
            == 1
        )
        assert (
            len(list(session.scalars(select(Notification).where(Notification.kind == "connection_help"))))
            == 1
        )


def test_help_intent_survives_verified_canonical_merge(paid_creation, monkeypatch):
    from sunrise_infrastructure.db import Account

    context = paid_creation
    service = engagement(context, monkeypatch)
    assert context[2].provisioning.run_jobs() == 1
    context[5]["now"] += timedelta(hours=25)
    assert service.run_due() == 1
    with context[0].transaction() as session:
        source = Account(email="engagement-merge@example.test", email_verified=True)
        session.add(source)
        session.flush()
        canonical = source.id
    accept_test_legal(context[2], canonical)
    proof = context[2].accounts.request_link(canonical, TG, context[5]["now"])["proof"]
    context[2].accounts.confirm_telegram_link(context[6], proof)
    assert (
        next(
            item
            for item in context[2].dashboard(canonical, refresh_panel=True)["subscriptions"]
            if item["id"] == context[7]
        )["connection_help_due"]
        is True
    )
    with context[0].transaction() as session:
        assert session.get(Setting, "engagement:" + context[7]).value["owner_id"] == str(canonical)
        assert (
            len(list(session.scalars(select(Notification).where(Notification.kind == "connection_help"))))
            == 1
        )


def test_exact_first_connected_event_emits_success_for_unknown_previous_fact(paid_creation, monkeypatch):
    from sunrise_infrastructure.panel_events import PanelEvents

    from tests.integration.test_v11_panel_events import signed_event

    context = paid_creation
    engagement(context, monkeypatch)
    assert context[2].provisioning.run_jobs() == 1
    context[4]["user"]["userTraffic"].update(
        firstConnectedAt=context[5]["now"].isoformat(), lifetimeUsedTrafficBytes=100
    )
    with context[0].transaction() as session:
        row = session.get(Setting, "engagement:" + context[7])
        row.value = row.value | {"known_never_connected": False}
    events = PanelEvents(context[0], context[1], context[3])
    raw, headers = signed_event("user.first_connected", timestamp=context[5]["now"].isoformat())
    assert events.accept(raw, headers)["status"] == "accepted"
    assert events.reconcile_pending() == 1
    with context[0].transaction() as session:
        assert (
            len(list(session.scalars(select(Notification).where(Notification.kind == "connection_success"))))
            == 1
        )


def test_unresolved_checkout_holds_connection_help(paid_creation, monkeypatch):
    context = paid_creation
    service = engagement(context, monkeypatch)
    assert context[2].provisioning.run_jobs() == 1
    quote = context[2].quote(context[6], "standard", 30, "purchase")
    context[2].create_order(context[6], quote["id"], "unfinished-onboarding-purchase")
    context[5]["now"] += timedelta(hours=25)
    service.run_due()
    assert projection(context)["connection_help_due"] is False
    with context[0].transaction() as session:
        assert session.scalar(select(Notification.id).where(Notification.kind == "connection_help")) is None


def test_corrupt_engagement_marker_fails_closed_without_breaking_profile(paid_creation, monkeypatch):
    context = paid_creation
    engagement(context, monkeypatch)
    assert context[2].provisioning.run_jobs() == 1
    with context[0].transaction() as session:
        marker = session.get(Setting, "engagement:" + context[7])
        marker.value = marker.value | {"owner_id": "999999999"}
    assert (
        next(
            item
            for item in context[2].dashboard(context[6], refresh_panel=True)["subscriptions"]
            if item["id"] == context[7]
        )["connection_help_due"]
        is False
    )


def test_initial_unknown_connected_snapshot_is_not_retroactive_success(paid_creation, monkeypatch):
    context = paid_creation
    engagement(context, monkeypatch)
    context[2].provisioning.run_jobs()
    with context[0].transaction() as session:
        marker = session.get(Setting, "engagement:" + context[7])
        marker.value = marker.value | {"known_never_connected": False}
    context[4]["user"]["userTraffic"].update(lifetimeUsedTrafficBytes=256)
    context[2].dashboard(context[6], refresh_panel=True)
    assert projection(context)["ever_connected"] is True
    with context[0].transaction() as session:
        assert (
            session.scalar(select(Notification.id).where(Notification.kind == "connection_success")) is None
        )


def test_old_failed_attempt_does_not_overwrite_reclaimed_engagement_lease(paid_creation, monkeypatch):
    context = paid_creation
    service = engagement(context, monkeypatch)
    context[2].provisioning.run_jobs()
    context[5]["now"] += timedelta(hours=25)
    original = context[1].client._transport
    reclaimed_at = context[5]["now"] + timedelta(minutes=5)

    def handler(request):
        if request.method == "GET" and request.url.path == "/api/users/123":
            with context[0].transaction() as session:
                job = session.scalar(select(Job).where(Job.kind == "subscription_engagement"))
                assert job.state == "in_flight" and job.last_error != "new-worker-lease"
                job.last_error, job.available_at, job.attempts = "new-worker-lease", reclaimed_at, 3
            return httpx.Response(503)
        return original.handle_request(request)

    context[1].client._transport = httpx.MockTransport(handler)
    assert service.run_due() == 0
    with context[0].transaction() as session:
        job = session.scalar(select(Job).where(Job.kind == "subscription_engagement"))
        assert (job.state, job.last_error, job.available_at, job.attempts) == (
            "in_flight",
            "new-worker-lease",
            reclaimed_at,
            3,
        )
