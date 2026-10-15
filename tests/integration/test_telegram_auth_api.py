# ruff: noqa: F811 -- pytest fixture imports share names with fixture parameters
import json
from uuid import uuid4

import pytest
from sqlalchemy import select
from sunrise_infrastructure.db import Account, Session

from tests.integration.test_v10_admin_mfa import mfa_context  # noqa: F401
from tests.unit.test_telegram_miniapp_auth import TOKEN, signed_init


@pytest.fixture
def telegram_context(mfa_context):
    _, app, _, _, _, _, _ = mfa_context
    config = app.state.settings
    config.bot_test_token = TOKEN
    config.test_bot_ids = "123456789"
    config.test_telegram_recipients = "70000000000000001"
    config.telegram_miniapp_enabled = True
    yield mfa_context


def init_data():
    from sunrise_infrastructure.db import now

    return signed_init(
        {
            "auth_date": str(int(now().timestamp())),
            "query_id": uuid4().hex,
            "user": json.dumps({"id": 70000000000000001, "username": "test_miniapp"}),
        }
    )


def test_miniapp_login_legal_gate_commits_one_canonical_session_and_replay_denies_without_side_effects(
    telegram_context,
):
    db, _, client, _, _, _, documents = telegram_context
    data = init_data()
    denied = client.post("/api/v1/auth/telegram/miniapp", json={"init_data": data})
    assert denied.status_code == 409
    with db.transaction() as s:
        assert s.scalar(select(Account).where(Account.telegram_id == 70000000000000001)) is None
        assert s.scalar(select(Session)) is None
    allowed = client.post(
        "/api/v1/auth/telegram/miniapp",
        json={
            "init_data": data,
            "legal_accepted": True,
            "documents": documents,
        },
    )
    assert allowed.status_code == 200, allowed.text
    aid = int(allowed.json()["account"]["id"])
    assert allowed.json()["account"]["email_verified"] is False
    with db.transaction() as s:
        sessions = list(s.scalars(select(Session).where(Session.account_id == aid)))
        assert len(sessions) == 1 and sessions[0].email_confirmed_at is None
    replay = client.post(
        "/api/v1/auth/telegram/miniapp",
        json={
            "init_data": data,
            "legal_accepted": True,
            "documents": documents,
        },
    )
    assert replay.status_code == 409
    assert replay.json()["error"]["code"] == "telegram_auth_replay"
    with db.transaction() as s:
        assert len(list(s.scalars(select(Session).where(Session.account_id == aid)))) == 1


def test_current_accepted_canonical_telegram_account_resumes_without_new_checkbox(telegram_context):
    db, app, client, _, _, _, documents = telegram_context
    commerce = app.state.commerce
    target = commerce.resolve_telegram(70000000000000001, username="test_miniapp")
    commerce.accept_legal(target["id"], documents, "telegram", "first_use")
    response = client.post("/api/v1/auth/telegram/miniapp", json={"init_data": init_data()})
    assert response.status_code == 200
    assert response.json()["account"]["id"] == target["id"]
    assert response.json()["account"]["telegram_id"] == "70000000000000001"


def test_miniapp_rejects_unapproved_test_identity_and_unconfigured_bot(telegram_context):
    db, app, client, _, _, _, documents = telegram_context
    app.state.settings.test_telegram_recipients = "70000000000000002"
    response = client.post(
        "/api/v1/auth/telegram/miniapp",
        json={
            "init_data": init_data(),
            "legal_accepted": True,
            "documents": documents,
        },
    )
    assert response.status_code == 403
    with db.transaction() as s:
        assert s.scalar(select(Account)) is None
    app.state.settings.test_bot_ids = "987654321"
    assert client.get("/api/v1/public-config").json()["telegram_miniapp_enabled"] is False
    assert client.post("/api/v1/auth/telegram/miniapp", json={"init_data": init_data()}).status_code == 503


def test_local_app_cannot_skip_turnstile_without_explicit_test_bypass(mfa_context, monkeypatch):
    _, app, client, email, _, _, _ = mfa_context
    app.state.settings.turnstile_test_bypass = False
    app.state.settings.turnstile_secret = "synthetic-cloudflare-test-secret"  # noqa: S105 -- synthetic fixture
    app.state.settings.turnstile_hostname = "127.0.0.1"

    class FailedCheck:
        def json(self):
            return {"success": False}

    monkeypatch.setattr("sunrise_api.main.httpx.post", lambda *args, **kwargs: FailedCheck())
    response = client.post(
        "/api/v1/auth/email/request", json={"email": email, "turnstile_token": "local-test"}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "captcha_invalid"


def test_oidc_start_is_unavailable_with_blank_credentials_and_never_returns_provider_secret(mfa_context):
    _, _, client, _, _, _, documents = mfa_context
    assert client.get("/api/v1/public-config").json()["telegram_login_enabled"] is False
    response = client.post(
        "/api/v1/auth/telegram/start", json={"legal_accepted": True, "documents": documents}
    )
    assert response.status_code == 503
    assert "secret" not in response.text


def test_malformed_test_bot_token_never_enables_miniapp_auth(telegram_context):
    _, app, client, _, _, _, _ = telegram_context
    app.state.settings.bot_test_token = "123456789"  # noqa: S105 -- deliberately malformed synthetic fixture
    assert client.get("/api/v1/public-config").json()["telegram_miniapp_enabled"] is False


def test_miniapp_redis_replay_storage_failure_rolls_back_account_consent_and_session(
    telegram_context, monkeypatch
):
    from redis.exceptions import RedisError
    from sunrise_infrastructure.db import LegalConsent

    db, app, client, _, _, _, documents = telegram_context
    redis = app.state.telegram_auth.redis
    original = redis.set

    def broken_claim(key, *args, **kwargs):
        if key.startswith("sunrise:miniapp-used:"):
            raise RedisError("synthetic-replay-storage-error")
        return original(key, *args, **kwargs)

    monkeypatch.setattr(redis, "set", broken_claim)
    response = client.post(
        "/api/v1/auth/telegram/miniapp",
        json={"init_data": init_data(), "legal_accepted": True, "documents": documents},
    )
    assert response.status_code == 503
    with db.transaction() as s:
        assert s.scalar(select(Account)) is None and s.scalar(select(Session)) is None
        assert s.scalar(select(LegalConsent)) is None


@pytest.mark.parametrize(
    "result",
    [
        {"success": "false", "hostname": "127.0.0.1", "action": "login"},
        {"success": 1, "hostname": "127.0.0.1", "action": "login"},
        [True],
    ],
)
def test_turnstile_requires_boolean_success_and_response_object_before_issuing_email_challenge(
    mfa_context, monkeypatch, result
):
    from sunrise_infrastructure.db import Challenge

    db, app, client, email, _, _, _ = mfa_context
    app.state.settings.turnstile_test_bypass = False
    app.state.settings.turnstile_secret = "synthetic-secret"  # noqa: S105 -- synthetic fixture
    app.state.settings.turnstile_hostname = "127.0.0.1"

    class Check:
        def json(self):
            return result

    monkeypatch.setattr("sunrise_api.main.httpx.post", lambda *args, **kwargs: Check())
    response = client.post(
        "/api/v1/auth/email/request", json={"email": email, "turnstile_token": "synthetic-response"}
    )
    assert response.status_code == 400
    with db.transaction() as s:
        assert s.scalar(select(Challenge)) is None


@pytest.mark.parametrize("hostname,expected_status", [
    ("login.example.test", 200),
    ("foreign.example.test", 400),
    (None, 400),
])
def test_turnstile_without_hostname_override_requires_configured_dashboard_host(
    mfa_context, monkeypatch, hostname, expected_status
):
    from sunrise_infrastructure.db import Challenge

    db, app, client, email, _, _, _ = mfa_context
    app.state.settings.turnstile_test_bypass = False
    app.state.settings.turnstile_secret = "synthetic-secret"  # noqa: S105 -- synthetic fixture
    app.state.settings.turnstile_hostname = None
    app.state.settings.dashboard_url = "https://login.example.test/"

    class Check:
        def json(self):
            return {"success": True, "hostname": hostname, "action": "login"}

    monkeypatch.setattr("sunrise_api.main.httpx.post", lambda *args, **kwargs: Check())
    response = client.post(
        "/api/v1/auth/email/request", json={"email": email, "turnstile_token": "synthetic-response"}
    )
    assert response.status_code == expected_status
    with db.transaction() as session:
        assert (session.scalar(select(Challenge)) is not None) == (expected_status == 200)
