from datetime import timedelta

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sunrise_api.main import create_app
from sunrise_infrastructure.db import Session, now
from sunrise_infrastructure.notifications import NotificationService
from sunrise_infrastructure.security import digest
from sunrise_infrastructure.settings import Settings

from tests.integration.test_v11_notifications import notification_context as notification_context


@pytest.fixture
def api_context(notification_context):
    db, _, (owner, other, _) = notification_context
    config = Settings(
        _env_file=None,
        database_url=db.engine.url.render_as_string(hide_password=False),
        secret_key="a" * 48,
        local_test=True,
        allowed_hosts=["testserver"],
        email_allowlist=["owner@example.test", "email@example.test"],
        admin_email_allowlist=["owner@example.test"],
        owner_id="202",
        panel_webhook_max_bytes=1024,
    )
    with db.transaction() as session:
        session.add(
            Session(
                id=digest("synthetic-session"),
                account_id=owner,
                csrf="synthetic-csrf",
                expires_at=now() + timedelta(days=1),
            )
        )
        own = NotificationService(db, config).emit_in(
            session, owner, "api-own", "test", "Ваше уведомление", "Только в вашем аккаунте"
        )
        foreign = NotificationService(db, config).emit_in(
            session, other, "api-other", "test", "Чужое уведомление", "Другой аккаунт"
        )
    app = create_app(config, redis_client=fakeredis.FakeRedis())
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        yield client, own.id, foreign.id
    app.state.commerce.db.engine.dispose()


def test_inbox_requires_session_and_scopes_read(api_context):
    client, own, foreign = api_context
    assert client.get("/api/v1/notifications").status_code == 401
    client.cookies.set("sunrise_session", "synthetic-session")
    response = client.get("/api/v1/notifications")
    assert response.status_code == 200 and [r["id"] for r in response.json()["items"]] == [own]
    assert client.post(f"/api/v1/notifications/{own}/read").status_code == 403
    assert (
        client.post(
            f"/api/v1/notifications/{foreign}/read", headers={"X-CSRF-Token": "synthetic-csrf"}
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/v1/notifications/{own}/read", headers={"X-CSRF-Token": "synthetic-csrf"}
        ).status_code
        == 200
    )


def test_auth_exposes_server_roles_and_own_sessions(api_context):
    client, _, _ = api_context
    client.cookies.set("sunrise_session", "synthetic-session")
    auth = client.get("/api/v1/auth/me").json()
    assert auth["staff_role"] == "owner" and "wallet_write" in auth["capabilities"]
    rows = client.get("/api/v1/account/sessions").json()["items"]
    assert len(rows) == 1 and rows[0]["current"] is True


def test_panel_webhook_rejects_oversized_stream_before_authentication(api_context):
    client, _, _ = api_context
    response = client.post("/api/v1/webhooks/remnawave", content=b"x" * 1025)
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "webhook_too_large"


def test_browser_preference_change_requires_current_session_and_csrf(api_context):
    client, _, _ = api_context
    url = "/api/v1/notifications/preferences"
    assert client.patch(url, json={"browser_enabled": True}).status_code == 401
    client.cookies.set("sunrise_session", "synthetic-session")
    assert client.patch(url, json={"browser_enabled": True}).status_code == 403
    headers = {"X-CSRF-Token": "synthetic-csrf"}
    assert client.patch(url, headers=headers, json={"browser_enabled": True}).json() == {
        "browser_enabled": True
    }
    assert client.get(url).json()["browser_enabled"] is True
    assert client.patch(url, headers=headers, json={"browser_enabled": False}).json() == {
        "browser_enabled": False
    }
    assert client.get(url).json()["browser_enabled"] is False
