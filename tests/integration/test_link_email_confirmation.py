import json
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError
from sqlalchemy import select
from sunrise_infrastructure.db import Account, Challenge, LegalConsent, Session, now
from sunrise_infrastructure.security import digest

from tests.integration.test_v10_admin_mfa import login

pytest_plugins = ["tests.integration.test_v10_admin_mfa"]


def stale_login(context):
    db, app, _, email, _, _, _ = context
    # Linking needs an unbound email source; the shared MFA login otherwise seeds a Telegram owner.
    with db.transaction() as tx:
        if tx.scalar(select(Account).where(Account.email == email)) is None:
            tx.add(Account(email=email))
    auth = login(context)
    assert auth["account"]["telegram_id"] is None and auth["is_admin"] is False
    with db.transaction() as tx:
        session = tx.scalar(select(Session))
        session.confirmed_at = session.email_confirmed_at = now() - timedelta(minutes=6)
    app.state.accounts.guard.redis.delete("sunrise:resend:" + digest(email))
    return auth


def request(context, auth):
    _, _, client, _, _, folder, _ = context
    response = client.post(
        "/api/v1/account/link/email/request", json={}, headers={"X-CSRF-Token": auth["csrf_token"]}
    )
    assert response.status_code == 200, response.text
    cid = response.json()["challenge_id"]
    message = json.loads((folder / f"{cid}.json").read_text(encoding="utf-8"))
    return response, cid, message


def verify(client, auth, cid, code):
    return client.post(
        "/api/v1/account/link/email/verify",
        json={"challenge_id": cid, "code": code},
        headers={"X-CSRF-Token": auth["csrf_token"]},
    )


def test_link_email_confirmation_preserves_session_cookie_csrf_legal_and_admin_assurance(mfa_context):
    db, app, client, email, _, _, _ = mfa_context
    auth = stale_login(mfa_context)
    target = app.state.commerce.resolve_telegram(70000000000000001, username="synthetic_target")
    headers = {"X-CSRF-Token": auth["csrf_token"]}
    identity = {"telegram_username": "@SYNTHETIC_TARGET"}
    denied = client.post("/api/v1/account/link/request", json=identity, headers=headers)
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "fresh_login_required"
    cookie = client.cookies.get("sunrise_session")
    with db.transaction() as tx:
        existing = tx.scalar(select(Session))
        before = (
            existing.id,
            existing.csrf,
            existing.confirmed_at,
            existing.email_confirmed_at,
            existing.mfa_at,
        )
        consents = len(list(tx.scalars(select(LegalConsent))))
    response, cid, message = request(mfa_context, auth)
    assert response.json() == {"challenge_id": cid, "resend_after": 120, "expires_in": 600}
    assert message["to"] == email and "привязки Telegram" in message["subject"]
    good = verify(client, auth, cid, message["code"])
    assert good.status_code == 200 and good.json() == {"confirmed": True, "expires_in": 300}
    assert "set-cookie" not in good.headers and client.cookies.get("sunrise_session") == cookie
    assert client.post("/api/v1/admin/mfa/enroll", headers=headers).status_code == 404
    proof = client.post("/api/v1/account/link/request", json=identity, headers=headers)
    assert proof.status_code == 200 and proof.json()["requires_merge"] is True
    with db.transaction() as tx:
        existing = tx.scalar(select(Session))
        assert (
            existing.id,
            existing.csrf,
            existing.confirmed_at,
            existing.email_confirmed_at,
            existing.mfa_at,
        ) == before
        assert len(list(tx.scalars(select(Session)))) == 1
        assert len(list(tx.scalars(select(LegalConsent)))) == consents
        assert tx.get(Account, int(auth["account"]["id"])).telegram_id is None
        assert tx.get(Account, int(target["id"])).email is None
    assert verify(client, auth, cid, message["code"]).status_code == 400


def test_link_confirmation_requires_csrf_and_never_accepts_an_arbitrary_email(mfa_context):
    _, _, client, _, _, _, _ = mfa_context
    assert client.post("/api/v1/account/link/email/request", json={}).status_code == 401
    auth = stale_login(mfa_context)
    assert client.post("/api/v1/account/link/email/request", json={}).status_code == 403
    assert (
        client.post(
            "/api/v1/account/link/email/request",
            json={"email": "other@example.test"},
            headers={"X-CSRF-Token": auth["csrf_token"]},
        ).status_code
        == 422
    )


@pytest.mark.parametrize("binding", ["other_session", "other_account", "other_ip"])
def test_link_code_is_bound_to_exact_current_account_session_and_direct_ip(mfa_context, binding):
    db, app, client, _, outsider, _, _ = mfa_context
    auth = stale_login(mfa_context)
    _, cid, message = request(mfa_context, auth)
    with TestClient(app, client=("127.0.0.2" if binding == "other_ip" else "127.0.0.1", 50001)) as other:
        if binding == "other_ip":
            other.cookies.update(client.cookies)
            other_auth = auth
        else:
            token = uuid4().hex
            with db.transaction() as tx:
                account_id = int(auth["account"]["id"])
                if binding == "other_account":
                    account = Account(email=outsider, email_verified=True)
                    tx.add(account)
                    tx.flush()
                    account_id = account.id
                tx.add(
                    Session(
                        id=digest(token),
                        account_id=account_id,
                        csrf="other-fixture",
                        expires_at=now() + timedelta(hours=1),
                    )
                )
            other.cookies.set("sunrise_session", token)
            other_auth = {"csrf_token": "other-fixture"}
        assert verify(other, other_auth, cid, message["code"]).status_code == 400
    with db.transaction() as tx:
        assert not tx.get(Challenge, cid).used
    assert verify(client, auth, cid, message["code"]).status_code == 200


def test_link_code_and_ordinary_login_code_cannot_cross_purposes(mfa_context):
    db, app, client, email, _, folder, documents = mfa_context
    auth = stale_login(mfa_context)
    ordinary = client.post(
        "/api/v1/auth/email/request", json={"email": email, "turnstile_token": "local-test"}
    )
    cid = ordinary.json()["challenge_id"]
    code = json.loads((folder / f"{cid}.json").read_text())["code"]
    assert verify(client, auth, cid, code).status_code == 400
    app.state.accounts.guard.redis.delete("sunrise:resend:" + digest(email))
    _, link_cid, message = request(mfa_context, auth)
    rejected = client.post(
        "/api/v1/auth/email/verify",
        json={
            "challenge_id": link_cid,
            "code": message["code"],
            "legal_accepted": True,
            "documents": documents,
        },
    )
    assert rejected.status_code == 400 and "set-cookie" not in rejected.headers
    with db.transaction() as tx:
        assert len(list(tx.scalars(select(Session)))) == 1
        assert not tx.get(Challenge, link_cid).used
    assert verify(client, auth, link_cid, message["code"]).status_code == 200


def test_link_code_cooldown_expiry_and_tenth_wrong_attempt_keep_existing_guards(mfa_context):
    db, _, client, _, _, _, _ = mfa_context
    auth = stale_login(mfa_context)
    _, cid, message = request(mfa_context, auth)
    resend = client.post(
        "/api/v1/account/link/email/request", json={}, headers={"X-CSRF-Token": auth["csrf_token"]}
    )
    assert resend.status_code == 429 and resend.json()["error"]["code"] == "resend_cooldown"
    wrong = "000000" if message["code"] != "000000" else "111111"
    for attempt in range(10):
        assert verify(client, auth, cid, wrong).status_code == (429 if attempt == 9 else 400)
    assert verify(client, auth, cid, message["code"]).status_code == 429
    with db.transaction() as tx:
        assert tx.get(Challenge, cid).used


def test_link_grant_expires_without_extending_general_session_freshness(mfa_context, monkeypatch):
    _, app, client, _, _, _, _ = mfa_context
    auth = stale_login(mfa_context)
    target = app.state.commerce.resolve_telegram(70000000000000001)
    _, cid, message = request(mfa_context, auth)
    assert verify(client, auth, cid, message["code"]).status_code == 200
    moment = now()
    monkeypatch.setattr("sunrise_infrastructure.accounts.now", lambda: moment + timedelta(minutes=6))
    response = client.post(
        "/api/v1/account/link/request",
        json={"telegram_id": target["telegram_id"]},
        headers={"X-CSRF-Token": auth["csrf_token"]},
    )
    assert response.status_code == 403 and response.json()["error"]["code"] == "fresh_login_required"


def test_link_redis_failure_does_not_consume_correct_code_or_authorize_link(mfa_context, monkeypatch):
    db, app, client, _, _, _, _ = mfa_context
    auth = stale_login(mfa_context)
    _, cid, message = request(mfa_context, auth)

    def unavailable(*args, **kwargs):
        raise ConnectionError("synthetic Redis failure")

    with monkeypatch.context() as patch:
        patch.setattr(app.state.accounts.guard.redis, "set", unavailable)
        assert verify(client, auth, cid, message["code"]).status_code == 503
    with db.transaction() as tx:
        assert not tx.get(Challenge, cid).used
    assert verify(client, auth, cid, message["code"]).status_code == 200


@pytest.mark.parametrize("state", ["expired", "changed_email", "unverified_email"])
def test_link_code_expiry_or_changed_current_email_never_refreshes_authorization(mfa_context, state):
    db, _, client, _, _, _, _ = mfa_context
    auth = stale_login(mfa_context)
    _, cid, message = request(mfa_context, auth)
    with db.transaction() as tx:
        if state == "expired":
            tx.get(Challenge, cid).expires_at = now() - timedelta(seconds=1)
        elif state == "changed_email":
            tx.get(Account, int(auth["account"]["id"])).email = "changed@example.test"
        else:
            tx.get(Account, int(auth["account"]["id"])).email_verified = False
    assert verify(client, auth, cid, message["code"]).status_code == (
        403 if state == "unverified_email" else 400
    )
    with db.transaction() as tx:
        assert not tx.get(Challenge, cid).used


def test_link_grant_requires_committed_verified_challenge_and_redis_read(mfa_context, monkeypatch):
    db, app, client, email, _, _, _ = mfa_context
    auth = stale_login(mfa_context)
    _, cid, _ = request(mfa_context, auth)
    with db.transaction() as tx:
        session_id = tx.scalar(select(Session)).id
    verified = app.state.accounts._link_context(
        auth["account"]["id"], session_id, email, "127.0.0.1", "verified"
    )
    # Redis may survive a failed SQL commit; a pending or failed challenge cannot authorize it.
    app.state.accounts.guard.redis.set("sunrise:link-confirmed:" + verified, cid, ex=300)
    target = app.state.commerce.resolve_telegram(70000000000000001)
    identity = {"telegram_id": target["telegram_id"]}
    headers = {"X-CSRF-Token": auth["csrf_token"]}
    assert client.post("/api/v1/account/link/request", json=identity, headers=headers).status_code == 403
    with db.transaction() as tx:
        tx.get(Challenge, cid).used = True  # Ten failures also mark a pending challenge used.
    assert client.post("/api/v1/account/link/request", json=identity, headers=headers).status_code == 403

    def unavailable(*args, **kwargs):
        raise ConnectionError("synthetic Redis read failure")

    with monkeypatch.context() as patch:
        patch.setattr(app.state.accounts.guard.redis, "get", unavailable)
        response = client.post("/api/v1/account/link/request", json=identity, headers=headers)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "protection_unavailable"
