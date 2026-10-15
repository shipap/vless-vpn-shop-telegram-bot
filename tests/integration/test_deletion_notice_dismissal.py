"""Owned archive notice dismissal survives reloads and canonical account merge."""

from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sunrise_api.main import create_app
from sunrise_core.errors import DomainError
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import (
    Account,
    Notification,
    PanelEvent,
    Payment,
    Segment,
    Session,
    Setting,
    Subscription,
    now,
)
from sunrise_infrastructure.panel_events import PanelEvents
from sunrise_infrastructure.security import digest

from tests.integration.test_subscription_engagement import paid_creation  # noqa: F401
from tests.integration.test_v11_panel_events import TG, owned_panel, signed_event  # noqa: F401


@pytest.fixture
def archived_card(paid_creation):  # noqa: F811
    db, panel, commerce, config, state, _, aid, sid, _ = paid_creation
    assert commerce.provisioning.run_jobs() == 1
    events = PanelEvents(db, panel, config)
    assert events.accept(*signed_event("user.deleted"))["status"] == "accepted"
    state["user"] = None
    assert events.reconcile_pending() == 1
    assert [n["subscription_id"] for n in commerce.dashboard(aid)["subscription_deletion_notices"]] == [sid]
    return SimpleNamespace(db=db, panel=panel, commerce=commerce, config=config, aid=aid, sid=sid)


def test_owned_dismissal_is_idempotent_durable_and_preserves_archive_money_and_notification_history(
    archived_card,
):
    item = archived_card
    with item.db.transaction() as session:
        archived = deepcopy(session.get(Setting, "archive:" + item.sid).value)
        payments = list(session.scalars(select(Payment.id)))
        segments = list(session.scalars(select(Segment.id)))
        notices = list(session.scalars(select(Notification.id)))
    assert item.commerce.dismiss_deletion_notice(item.aid, item.sid) == {"dismissed": True}
    assert item.commerce.dismiss_deletion_notice(item.aid, item.sid) == {"dismissed": True}
    restarted = Commerce(item.db, local_test=True, panel_provider=item.panel, public_settings=item.config)
    assert restarted.dashboard(item.aid)["subscription_deletion_notices"] == []
    with item.db.transaction() as session:
        assert session.get(Setting, "archive:" + item.sid).value == archived
        assert list(session.scalars(select(Payment.id))) == payments
        assert list(session.scalars(select(Segment.id))) == segments
        assert list(session.scalars(select(Notification.id))) == notices


def test_foreign_unknown_and_unarchived_dismissals_are_404(archived_card):
    item = archived_card
    other = item.commerce.resolve_telegram(70000000000000002)
    with item.db.transaction() as session:
        active = session.scalar(select(Subscription).where(Subscription.id != item.sid))
        active_id = active.id
    for account, sid in ((other["id"], item.sid), (item.aid, "unknown"), (item.aid, active_id)):
        with pytest.raises(DomainError) as error:
            item.commerce.dismiss_deletion_notice(account, sid)
        assert error.value.status == 404


def test_acknowledgement_follows_immutable_subscription_after_verified_merge(archived_card):
    item = archived_card
    assert item.commerce.dismiss_deletion_notice(item.aid, item.sid) == {"dismissed": True}
    with item.db.transaction() as session:
        email_owner = Account(email="merge.synthetic@example.test", email_verified=True)
        session.add(email_owner)
        session.flush()
        canonical = email_owner.id
    proof = item.commerce.accounts.request_link(canonical, TG, now())["proof"]
    item.commerce.accounts.confirm_telegram_link(item.aid, proof)
    assert item.commerce.dashboard(canonical)["subscription_deletion_notices"] == []
    assert item.commerce.dismiss_deletion_notice(canonical, item.sid) == {"dismissed": True}


def test_http_dismissal_requires_session_csrf_empty_body_and_actual_archive_owner(archived_card):
    item = archived_card
    item.config.allowed_hosts = ["testserver"]
    item.config.allowed_origins = ["http://testserver"]
    app = create_app(item.config, redis_client=fakeredis.FakeRedis(), panel_provider=item.panel)
    token, csrf = "synthetic-dismiss-session", "synthetic-dismiss-csrf"
    other = item.commerce.resolve_telegram(70000000000000002)
    with item.db.transaction() as session:
        session.add(
            Session(id=digest(token), account_id=item.aid, csrf=csrf, expires_at=now() + timedelta(hours=1))
        )
        session.add(
            Session(
                id=digest(token + "-foreign"),
                account_id=int(other["id"]),
                csrf=csrf,
                expires_at=now() + timedelta(hours=1),
            )
        )
    endpoint = "/api/v1/subscriptions/" + item.sid + "/deletion-notice/dismiss"
    with TestClient(app) as client:
        assert client.post(endpoint, json={}).status_code == 401
        client.cookies.set("sunrise_session", token)
        assert client.post(endpoint, json={}).status_code == 403
        headers = {"X-CSRF-Token": csrf}
        assert client.post(endpoint, headers=headers, json={"unexpected": True}).status_code == 422
        client.cookies.set("sunrise_session", token + "-foreign")
        assert client.post(endpoint, headers=headers, json={}).status_code == 404
        client.cookies.set("sunrise_session", token)
        response = client.post(endpoint, headers=headers, json={})
        assert response.status_code == 200 and response.json() == {"dismissed": True}
        assert item.commerce.dashboard(item.aid)["subscription_deletion_notices"] == []
    app.state.commerce.db.engine.dispose()


def test_acknowledgement_does_not_hide_a_different_verified_archive_proof(archived_card):
    item = archived_card
    item.commerce.dismiss_deletion_notice(item.aid, item.sid)
    events = PanelEvents(item.db, item.panel, item.config)
    raw, headers = signed_event("user.deleted", timestamp=(now() + timedelta(seconds=1)).isoformat())
    assert events.accept(raw, headers)["status"] == "accepted"
    with item.db.transaction() as session:
        archive = session.get(Setting, "archive:" + item.sid)
        proof = session.scalar(
            select(PanelEvent).where(
                PanelEvent.kind == "user.deleted", PanelEvent.id != archive.value["proof_id"]
            )
        )
        assert proof.subscription_id == item.sid
        archive.value = archive.value | {"proof_id": proof.id}
    assert [
        notice["subscription_id"]
        for notice in item.commerce.dashboard(item.aid)["subscription_deletion_notices"]
    ] == [item.sid]
    assert item.commerce.dismiss_deletion_notice(item.aid, item.sid) == {"dismissed": True}
    assert item.commerce.dashboard(item.aid)["subscription_deletion_notices"] == []
