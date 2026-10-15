import json
from datetime import timedelta
from uuid import uuid4

import pyotp
import pytest
from redis.exceptions import ConnectionError
from sqlalchemy import select
from sunrise_core.errors import DomainError
from sunrise_infrastructure.db import Account, AdminAuthenticator, MergeProof, Session, now
from sunrise_infrastructure.security import digest

from tests.integration.test_v10_admin_mfa import login

pytest_plugins = ["tests.integration.test_v10_admin_mfa"]


def prepare(context, *, legacy=False, enroll_mfa=False):
    db, app, client, _, _, _, _ = context
    auth = login(context)
    if enroll_mfa:
        headers = {"X-CSRF-Token": auth["csrf_token"]}
        pending = client.post("/api/v1/admin/mfa/enroll", headers=headers).json()
        assert (
            client.post(
                "/api/v1/admin/mfa/enroll/confirm",
                headers=headers,
                json={"code": pyotp.TOTP(pending["secret"]).now()},
            ).status_code
            == 200
        )
    with db.transaction() as tx:
        source = tx.get(Account, int(auth["account"]["id"]))
        telegram_id = source.telegram_id
        assert telegram_id == int(app.state.settings.owner_id)
        source.telegram_id = None
    target = app.state.commerce.resolve_telegram(telegram_id)
    assert target["id"] != auth["account"]["id"]
    other_source, target_token = uuid4().hex, uuid4().hex
    with db.transaction() as tx:
        initiator = tx.scalar(select(Session))
        initiator_id = initiator.id
        tx.add_all(
            [
                Session(
                    id=digest(other_source),
                    account_id=int(auth["account"]["id"]),
                    csrf="other",
                    expires_at=now() + timedelta(hours=1),
                ),
                Session(
                    id=digest(target_token),
                    account_id=int(target["id"]),
                    csrf="target",
                    expires_at=now() + timedelta(hours=1),
                ),
            ]
        )
    if legacy:
        proof = app.state.accounts.request_link(auth["account"]["id"], target["telegram_id"], now())["proof"]
    else:
        response = client.post(
            "/api/v1/account/link/request",
            json={"telegram_id": target["telegram_id"]},
            headers={"X-CSRF-Token": auth["csrf_token"]},
        )
        assert response.status_code == 200, response.text
        proof = response.json()["proof"]
    return auth, target, proof, initiator_id


def test_successful_bot_merge_keeps_only_initiating_website_session_without_freshness_extension(mfa_context):
    db, app, client, _, _, _, _ = mfa_context
    auth, target, proof, session_id = prepare(mfa_context, enroll_mfa=True)
    source_id = int(auth["account"]["id"])
    with db.transaction() as tx:
        existing = tx.get(Session, session_id)
        before = (existing.csrf, existing.confirmed_at, existing.email_confirmed_at, existing.expires_at)
        record = tx.get(AdminAuthenticator, source_id)
        seed = (record.secret_ciphertext, record.activated_at, record.last_totp_step)
    with pytest.raises(DomainError):
        app.state.accounts.confirm_telegram_link(source_id, proof)
    result = app.state.accounts.confirm_telegram_link(target["id"], proof)
    assert result["id"] == str(source_id)
    me = client.get("/api/v1/auth/me")
    assert me.status_code == 200 and me.json()["account"]["telegram_id"] == target["telegram_id"]
    assert me.json()["csrf_token"] == auth["csrf_token"] and me.json()["mfa_verified"] is False
    assert client.get("/api/v1/admin/overview").status_code == 403
    with db.transaction() as tx:
        sessions = list(tx.scalars(select(Session)))
        assert len(sessions) == 1 and sessions[0].id == session_id
        assert (
            sessions[0].csrf,
            sessions[0].confirmed_at,
            sessions[0].email_confirmed_at,
            sessions[0].expires_at,
        ) == before
        assert sessions[0].mfa_at is None
        record = tx.get(AdminAuthenticator, source_id)
        assert (record.secret_ciphertext, record.activated_at, record.last_totp_step) == seed
        assert record.trusted_ip_digest is None and record.trust_verified_at is None
    with pytest.raises(DomainError):
        app.state.accounts.confirm_telegram_link(target["id"], proof)


@pytest.mark.parametrize("binding", ["missing", "expired_session", "wrong_source", "wrong_session", "legacy"])
def test_merge_never_preserves_an_unbound_expired_or_foreign_session(mfa_context, binding):
    db, app, _, _, _, _, _ = mfa_context
    _, target, proof, session_id = prepare(mfa_context, legacy=binding == "legacy")
    key = "sunrise:link-session:" + proof.split(".")[0]
    redis = app.state.accounts.guard.redis
    if binding == "missing":
        redis.delete(key)
    elif binding == "expired_session":
        with db.transaction() as tx:
            tx.get(Session, session_id).expires_at = now() - timedelta(seconds=1)
    elif binding in {"wrong_source", "wrong_session"}:
        payload = json.loads(redis.get(key))
        with db.transaction() as tx:
            foreign = tx.scalar(select(Session).where(Session.account_id == int(target["id"])))
        payload["source_id" if binding == "wrong_source" else "session_id"] = (
            target["id"] if binding == "wrong_source" else foreign.id
        )
        redis.set(key, json.dumps(payload), ex=300)
    app.state.accounts.confirm_telegram_link(target["id"], proof)
    with db.transaction() as tx:
        assert list(tx.scalars(select(Session))) == []


@pytest.mark.parametrize("operation", ["request", "confirm"])
def test_merge_session_binding_redis_failure_rolls_back_identity_and_proof(
    mfa_context, monkeypatch, operation
):
    db, app, client, _, _, _, _ = mfa_context
    auth, target, proof, session_id = prepare(mfa_context)

    def unavailable(*args, **kwargs):
        raise ConnectionError("synthetic Redis failure")

    with monkeypatch.context() as patch:
        patch.setattr(app.state.accounts.guard.redis, "set" if operation == "request" else "get", unavailable)
        if operation == "request":
            response = client.post(
                "/api/v1/account/link/request",
                json={"telegram_id": target["telegram_id"]},
                headers={"X-CSRF-Token": auth["csrf_token"]},
            )
            assert response.status_code == 503
        else:
            with pytest.raises(DomainError) as error:
                app.state.accounts.confirm_telegram_link(target["id"], proof)
            assert error.value.code == "protection_unavailable"
    with db.transaction() as tx:
        assert tx.get(Account, int(auth["account"]["id"])).telegram_id is None
        assert tx.get(Account, int(target["id"])).telegram_id == int(target["telegram_id"])
        assert tx.get(Session, session_id) is not None
        assert len(list(tx.scalars(select(MergeProof)))) == 1
