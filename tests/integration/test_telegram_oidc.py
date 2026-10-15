from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import select
from sunrise_infrastructure.db import Account, Session, now

pytest_plugins = ["tests.integration.test_v10_admin_mfa"]


class SyntheticTelegram:
    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.nonce = None
        self.overrides = {}
        self.requests = []

    def token(self, *, code, verifier, client_id, client_secret, redirect_uri):
        assert code == "synthetic-provider-code"
        self.requests.append({"verifier": verifier, "client_id": client_id, "redirect_uri": redirect_uri})
        moment = int(now().timestamp())
        claims = {
            "iss": "https://oauth.telegram.org",
            "aud": client_id,
            "sub": "opaque-sub-not-telegram-id",
            "id": 70000000000000001,
            "preferred_username": "oidc_test_user",
            "nonce": self.nonce,
            "iat": moment,
            "exp": moment + 300,
        } | self.overrides
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": "synthetic-key"})

    def keys(self):
        value = jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key(), as_dict=True)
        return {"keys": [value | {"kid": "synthetic-key", "alg": "RS256", "use": "sig"}]}


@pytest.fixture
def oidc_context(mfa_context):
    _, app, _, _, _, _, _ = mfa_context
    config = app.state.settings
    config.telegram_login_client_id = "123456789"
    config.test_bot_ids = "123456789"
    config.telegram_login_client_secret = "synthetic-oidc-client-secret"  # noqa: S105 -- synthetic fixture
    config.telegram_login_redirect_uri = "http://testserver/api/v1/auth/telegram/callback"
    config.test_telegram_recipients = "70000000000000001"
    provider = SyntheticTelegram()
    app.state.telegram_auth.transport = provider
    yield mfa_context, provider


def begin(context, *, referral_code=None):
    (db, app, client, _, _, _, documents), provider = context
    response = client.post(
        "/api/v1/auth/telegram/start",
        json={"legal_accepted": True, "documents": documents, "referral_code": referral_code},
    )
    assert response.status_code == 200, response.text
    uri = urlsplit(response.json()["authorization_url"])
    params = parse_qs(uri.query)
    assert uri.scheme == "https" and uri.hostname == "oauth.telegram.org" and uri.path == "/auth"
    assert params["scope"] == ["openid profile"]
    assert params["code_challenge_method"] == ["S256"]
    assert len(params["code_challenge"][0]) == 43
    provider.nonce = params["nonce"][0]
    return params["state"][0]


def test_oidc_keeps_referral_in_bound_server_flow_and_assigns_new_account_once(oidc_context):
    (db, _, client, _, _, _, _), _ = oidc_context
    with db.transaction() as tx:
        inviter = Account(email="inviter@example.test", email_verified=True)
        other = Account(email="other-inviter@example.test", email_verified=True)
        tx.add_all([inviter, other])
        tx.flush()
        inviter_id, other_id = inviter.id, other.id
    first = begin(oidc_context, referral_code="ref_" + str(inviter_id))
    response = client.get(
        "/api/v1/auth/telegram/callback",
        params={"code": "synthetic-provider-code", "state": first, "referral_code": "ref_" + str(other_id)},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/"
    with db.transaction() as tx:
        account = tx.scalar(select(Account).where(Account.telegram_id == 70000000000000001))
        assert account.referrer_id == inviter_id
    second = begin(oidc_context, referral_code="ref_" + str(other_id))
    assert callback(client, second).headers["location"] == "/"
    with db.transaction() as tx:
        account = tx.scalar(select(Account).where(Account.telegram_id == 70000000000000001))
        assert account.referrer_id == inviter_id


def callback(client, state):
    return client.get(
        "/api/v1/auth/telegram/callback",
        params={"code": "synthetic-provider-code", "state": state},
        follow_redirects=False,
    )


def test_oidc_pkce_browser_state_nonce_and_signed_profile_id_create_the_canonical_session(oidc_context):
    (db, _, client, _, _, _, _), provider = oidc_context
    state = begin(oidc_context)
    response = callback(client, state)
    assert response.status_code == 303 and response.headers["location"] == "/"
    assert "sunrise_session" in response.cookies
    assert "client_secret" not in response.text
    me = client.get("/api/v1/auth/me")
    assert me.status_code == 200 and me.json()["account"]["telegram_id"] == "70000000000000001"
    assert me.json()["account"]["email_verified"] is False
    assert len(provider.requests[0]["verifier"]) >= 43
    with db.transaction() as s:
        session = s.scalar(select(Session))
        assert session.email_confirmed_at is None
        assert s.get(Account, session.account_id).telegram_username == "oidc_test_user"
    assert callback(client, state).headers["location"].startswith("/?telegram_login_error=")


@pytest.mark.parametrize(
    "overrides",
    [
        {"iss": "https://attacker.example.test"},
        {"aud": "another-client"},
        {"nonce": "wrong-nonce"},
        {"exp": 1},
        {"id": True},
        {"id": "070000000000000001"},
        {"id": 9223372036854775808},
    ],
)
def test_oidc_invalid_signed_claims_never_create_a_session(oidc_context, overrides):
    (db, _, client, _, _, _, _), provider = oidc_context
    state = begin(oidc_context)
    provider.overrides = overrides
    response = callback(client, state)
    assert response.status_code == 303 and response.headers["location"].startswith("/?telegram_login_error=")
    assert "sunrise_session" not in response.cookies
    with db.transaction() as s:
        assert s.scalar(select(Account)) is None and s.scalar(select(Session)) is None


def test_oidc_decimal_string_and_integer_id_resume_the_same_canonical_account(oidc_context):
    (db, app, client, _, _, _, _), provider = oidc_context
    target = app.state.commerce.resolve_telegram(70000000000000001, username="oidc_test_user")
    for profile_id in ("70000000000000001", 70000000000000001):
        state = begin(oidc_context)
        provider.overrides = {"id": profile_id}
        response = callback(client, state)
        assert response.status_code == 303 and response.headers["location"] == "/"
        account = client.get("/api/v1/auth/me").json()["account"]
        assert account["id"] == target["id"]
        assert account["telegram_id"] == "70000000000000001"
    with db.transaction() as session:
        accounts = list(session.scalars(select(Account)))
        assert len(accounts) == 1 and accounts[0].id == int(target["id"])
        assert len(list(session.scalars(select(Session).where(Session.account_id == accounts[0].id)))) == 2


def test_oidc_callback_requires_the_same_browser_cookie_before_consuming_flow(oidc_context):
    (_, app, client, _, _, _, _), provider = oidc_context
    state = begin(oidc_context)
    with TestClient(app, client=("127.0.0.1", 50001)) as other:
        assert callback(other, state).headers["location"].startswith("/?telegram_login_error=")
    assert not provider.requests
    assert callback(client, state).headers["location"] == "/"


def test_oidc_callback_logs_only_a_bounded_failure_code(oidc_context, caplog):
    (_, _, client, _, _, _, _), _ = oidc_context
    with caplog.at_level("WARNING", logger="sunrise.telegram_login"):
        response = client.get(
            "/api/v1/auth/telegram/callback",
            params={"code": "private-provider-code-marker", "state": "private-state-marker"},
            follow_redirects=False,
        )
    assert response.status_code == 303
    records = [r.getMessage() for r in caplog.records if r.name == "sunrise.telegram_login"]
    assert "telegram_login_callback_denied code=telegram_auth_invalid" in records
    assert not any(
        "private-provider-code-marker" in text or "private-state-marker" in text for text in records
    )


def test_oidc_final_legal_version_check_rejects_updates_after_the_checkbox(oidc_context):
    (db, app, client, _, _, _, documents), _ = oidc_context
    state = begin(oidc_context)
    app.state.commerce.legal.configure(
        None, [d | {"version": "newer"} for d in documents], "Обновление документов после начала входа"
    )
    response = callback(client, state)
    assert response.headers["location"] == "/?telegram_login_error=legal_versions_stale"
    with db.transaction() as s:
        assert s.scalar(select(Account)) is None and s.scalar(select(Session)) is None


def test_oidc_invalid_callback_budget_blocks_after_30_failed_logins(oidc_context):
    (_, app, client, _, _, _, documents), _ = oidc_context
    for _ in range(20):
        callback(client, "unknown-state")
    for key in app.state.telegram_auth.redis.scan_iter("sunrise:oidc-budget:callback:*"):
        app.state.telegram_auth.redis.delete(key)
    for _ in range(10):
        callback(client, "unknown-state")
    response = client.post(
        "/api/v1/auth/telegram/start", json={"legal_accepted": True, "documents": documents}
    )
    assert response.status_code == 429


@pytest.mark.parametrize("operation", ["start", "callback"])
def test_oidc_request_budget_precedes_state_allocation_and_provider_calls(oidc_context, operation):
    (_, app, client, _, _, _, documents), provider = oidc_context
    state = begin(oidc_context) if operation == "callback" else None
    for _ in range(20):
        if operation == "start":
            begin(oidc_context)
        else:
            callback(client, "unknown-state")
    flows_before = list(app.state.telegram_auth.redis.scan_iter("sunrise:oidc-flow:*"))
    response = (
        client.post("/api/v1/auth/telegram/start", json={"legal_accepted": True, "documents": documents})
        if operation == "start"
        else callback(client, state)
    )
    if operation == "start":
        assert response.status_code == 429
    else:
        assert response.headers["location"] == "/?telegram_login_error=telegram_auth_rate_limited"
    assert list(app.state.telegram_auth.redis.scan_iter("sunrise:oidc-flow:*")) == flows_before
    assert not provider.requests


def test_oidc_budget_redis_failure_and_flow_collision_fail_closed(oidc_context, monkeypatch):
    from redis.exceptions import ConnectionError

    (_, app, client, _, _, _, documents), provider = oidc_context
    redis = app.state.telegram_auth.redis

    def unavailable(*args, **kwargs):
        raise ConnectionError("synthetic Redis failure")

    with monkeypatch.context() as patch:
        patch.setattr(redis, "eval", unavailable)
        response = client.post(
            "/api/v1/auth/telegram/start", json={"legal_accepted": True, "documents": documents}
        )
        assert response.status_code == 503
        assert not list(redis.scan_iter("sunrise:oidc-flow:*"))
    with monkeypatch.context() as patch:
        patch.setattr(redis, "set", lambda *args, **kwargs: False)
        response = client.post(
            "/api/v1/auth/telegram/start", json={"legal_accepted": True, "documents": documents}
        )
        assert response.status_code == 503
    assert not provider.requests


def test_oidc_client_id_must_be_explicitly_approved_test_bot(oidc_context):
    (_, app, client, _, _, _, documents), provider = oidc_context
    app.state.settings.test_bot_ids = "987654321"
    response = client.post(
        "/api/v1/auth/telegram/start", json={"legal_accepted": True, "documents": documents}
    )
    assert response.status_code == 503
    assert not provider.requests


@pytest.mark.parametrize(
    "mode", ["wrong_signature", "wrong_algorithm", "unknown_kid", "duplicate_state", "expired_state"]
)
def test_oidc_invalid_signature_keys_or_callback_state_never_authenticate(oidc_context, mode, monkeypatch):
    from sunrise_infrastructure.security import digest

    (db, app, client, _, _, _, _), provider = oidc_context
    state = begin(oidc_context)
    if mode == "wrong_signature":
        other = SyntheticTelegram()
        other.nonce = provider.nonce
        monkeypatch.setattr(provider, "token", other.token)
    if mode == "wrong_algorithm":
        monkeypatch.setattr(
            provider,
            "token",
            lambda **kwargs: jwt.encode({"id": 70000000000000001}, "s" * 48, algorithm="HS256"),
        )
    if mode == "unknown_kid":
        monkeypatch.setattr(provider, "keys", lambda: {"keys": []})
    if mode == "expired_state":
        import json

        key = "sunrise:oidc-flow:" + digest(state)
        flow = json.loads(app.state.telegram_auth.redis.get(key))
        flow["expires_at"] = 1
        app.state.telegram_auth.redis.set(key, json.dumps(flow))
    response = (
        client.get(
            "/api/v1/auth/telegram/callback",
            params=[("code", "synthetic-provider-code"), ("state", state), ("state", state)],
            follow_redirects=False,
        )
        if mode == "duplicate_state"
        else callback(client, state)
    )
    assert response.headers["location"].startswith("/?telegram_login_error=")
    with db.transaction() as s:
        assert s.scalar(select(Account)) is None and s.scalar(select(Session)) is None
