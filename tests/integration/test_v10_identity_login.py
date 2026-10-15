import json
from datetime import timedelta
from uuid import uuid4

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sunrise_api.main import create_app
from sunrise_core.errors import DomainError
from sunrise_infrastructure.accounts import Accounts
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import (
    Account,
    Challenge,
    LegalConsent,
    MergeProof,
    Session,
    Setting,
    now,
)
from sunrise_infrastructure.security import RateGuard

from tests.e2e.local_auth_server import SyntheticSettings
from tests.isolated_database import isolated_database


@pytest.fixture
def context(tmp_path):
    with isolated_database("v10_identity_test") as db:
        email = f"{uuid4()}@example.test"
        settings = SyntheticSettings(
            _env_file=None,
            database_url=db.engine.url.render_as_string(hide_password=False),
            secret_key="s" * 48,
            local_test=True,
            email_allowlist=[email],
            mail_dir=str(tmp_path),
            allowed_hosts=["testserver"],
            allowed_origins=["http://testserver"],
            turnstile_test_bypass=True,
        )
        commerce = Commerce(db)
        documents = [
            {"document_type": "offer", "version": "v10-synthetic", "url": "https://example.test/offer"},
            {"document_type": "privacy", "version": "v10-synthetic", "url": "https://example.test/privacy"},
        ]
        commerce.legal.configure(None, documents, "????????????? ???????? ?????")
        accounts = Accounts(db, RateGuard(fakeredis.FakeRedis()), settings)
        with TestClient(
            create_app(settings, redis_client=fakeredis.FakeRedis()), client=("127.0.0.1", 50000)
        ) as client:
            yield db, accounts, commerce, client, email, tmp_path, documents


def request_otp(client, email, path):
    response = client.post(
        "/api/v1/auth/email/request", json={"email": email, "turnstile_token": "local-test"}
    )
    assert response.status_code == 200
    cid = response.json()["challenge_id"]
    return {"challenge_id": cid, "code": json.loads((path / f"{cid}.json").read_text())["code"]}


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("missing", "legal_consent_required"),
        ("unchecked", "legal_consent_required"),
        ("stale", "legal_versions_stale"),
        ("unconfigured", "legal_not_configured"),
    ],
)
def test_login_denied_consent_preserves_otp_and_never_creates_an_authenticated_session(
    context, mode, expected
):
    db, _, _, client, email, path, documents = context
    payload = request_otp(client, email, path)
    if mode != "missing":
        payload |= {"legal_accepted": mode != "unchecked", "documents": documents}
    if mode == "stale":
        payload["documents"] = [d | {"version": "older"} for d in documents]
    if mode == "unconfigured":
        with db.transaction() as s:
            s.delete(s.get(Setting, "legal"))
    result = client.post("/api/v1/auth/email/verify", json=payload)
    assert result.status_code in {409, 503}
    assert result.json()["error"]["code"] == expected
    assert "sunrise_session" not in result.cookies
    assert client.get("/api/v1/auth/me").status_code == 401
    with db.transaction() as s:
        assert s.get(Challenge, payload["challenge_id"]).used is False
        assert s.scalar(select(Account).where(Account.email == email)) is None


def test_valid_otp_and_explicit_current_consent_commit_together(context):
    db, _, _, client, email, path, documents = context
    payload = request_otp(client, email, path) | {"legal_accepted": True, "documents": documents}
    result = client.post("/api/v1/auth/email/verify", json=payload)
    assert result.status_code == 200, result.text
    aid = int(result.json()["account"]["id"])
    with db.transaction() as s:
        assert s.get(Challenge, payload["challenge_id"]).used is True
        rows = list(s.scalars(select(LegalConsent).where(LegalConsent.account_id == aid)))
        assert {(r.document_type, r.document_version, r.source, r.context) for r in rows} == {
            ("offer", "v10-synthetic", "website", "registration"),
            ("privacy", "v10-synthetic", "website", "registration"),
        }
        assert s.scalar(select(func.count()).select_from(Session).where(Session.account_id == aid)) == 1
    assert client.post("/api/v1/auth/email/verify", json=payload).status_code == 400


def test_failed_session_storage_rolls_back_verification_otp_and_legal_evidence(context):
    db, accounts, _, client, email, path, documents = context
    payload = request_otp(client, email, path)

    def fail_session_write(session, _flush_context, _instances):
        if any(isinstance(row, Session) for row in session.new):
            raise RuntimeError("synthetic-session-storage-failure")

    event.listen(db.sessions, "before_flush", fail_session_write)
    try:
        with pytest.raises(RuntimeError, match="synthetic-session-storage-failure"):
            accounts.verify_code(
                payload["challenge_id"],
                payload["code"],
                "127.0.0.1",
                legal_accepted=True,
                documents=documents,
            )
    finally:
        event.remove(db.sessions, "before_flush", fail_session_write)
    with db.transaction() as s:
        assert s.get(Challenge, payload["challenge_id"]).used is False
        assert s.scalar(select(Account).where(Account.email == email)) is None
    _, auth = accounts.verify_code(
        payload["challenge_id"],
        payload["code"],
        "127.0.0.1",
        legal_accepted=True,
        documents=documents,
    )
    assert auth["account"]["email_verified"] is True


def test_missing_consent_does_not_verify_an_existing_unverified_email(context):
    db, _, _, client, email, path, _ = context
    with db.transaction() as s:
        account = Account(email=email, email_verified=False)
        s.add(account)
        s.flush()
        aid = account.id
    payload = request_otp(client, email, path)
    assert client.post("/api/v1/auth/email/verify", json=payload).status_code == 409
    with db.transaction() as s:
        assert s.get(Account, aid).email_verified is False
        assert s.scalar(select(Session).where(Session.account_id == aid)) is None
        assert s.scalar(select(LegalConsent).where(LegalConsent.account_id == aid)) is None


def new_source(db):
    with db.transaction() as s:
        account = Account(email=f"{uuid4()}@example.test", email_verified=True)
        s.add(account)
        s.flush()
        return account.id


def test_username_link_only_issues_pair_bound_proof_and_requires_correct_bot_owner(context):
    db, accounts, commerce, _, _, _, _ = context
    aid = new_source(db)
    name = "v10_" + uuid4().hex[:20]
    tid = uuid4().int % 1000000000000 + 100000
    target = commerce.resolve_telegram(tid, username=name)
    proof = accounts.request_link(aid, None, now(), target_telegram_username="@" + name.upper())["proof"]
    with db.transaction() as s:
        assert s.get(Account, aid).telegram_id is None
        record = s.get(MergeProof, proof.split(".")[0])
        assert record.source_id == aid
        assert record.target_id == int(target["id"])
    with pytest.raises(DomainError) as wrong:
        accounts.confirm_telegram_link(aid, proof)
    assert wrong.value.code == "link_invalid"
    assert accounts.confirm_telegram_link(target["id"], proof)["telegram_id"] == str(tid)
    with pytest.raises(DomainError):
        accounts.confirm_telegram_link(target["id"], proof)


@pytest.mark.parametrize("mode", ["unknown", "ambiguous", "renamed", "no_telegram"])
def test_unknown_or_ambiguous_username_link_fails_without_creating_or_merging_accounts(context, mode):
    db, accounts, commerce, _, _, _, _ = context
    aid = new_source(db)
    name = "v10_" + uuid4().hex[:20]
    tid = uuid4().int % 1000000000000 + 100000
    if mode in {"ambiguous", "renamed"}:
        commerce.resolve_telegram(tid, username=name)
    if mode == "ambiguous":
        commerce.resolve_telegram(tid + 1, username=name.upper())
    if mode == "renamed":
        commerce.resolve_telegram(tid, username=name + "x")
    if mode == "no_telegram":
        with db.transaction() as s:
            s.add(Account(telegram_username=name))
    with db.transaction() as s:
        before = s.scalar(select(func.count()).select_from(Account))
    with pytest.raises(DomainError) as exc:
        accounts.request_link(aid, None, now(), target_telegram_username=name)
    assert exc.value.code == "link_target_unavailable"
    with db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(Account)) == before
        assert s.get(Account, aid).telegram_id is None
        assert s.scalar(select(MergeProof).where(MergeProof.source_id == aid)) is None


def test_link_requires_fresh_source_identity_before_username_resolution(context):
    db, accounts, _, _, _, _, _ = context
    with pytest.raises(DomainError) as exc:
        accounts.request_link(
            new_source(db), None, now() - timedelta(minutes=6), target_telegram_username="known"
        )
    assert exc.value.code == "fresh_login_required"


def test_unknown_numeric_target_does_not_create_a_placeholder_identity(context):
    db, accounts, _, _, _, _, _ = context
    aid = new_source(db)
    tid = str(uuid4().int % 1000000000000 + 100000)
    with pytest.raises(DomainError) as exc:
        accounts.request_link(aid, tid, now())
    assert exc.value.code == "link_target_unavailable"
    with db.transaction() as s:
        assert s.scalar(select(Account).where(Account.telegram_id == int(tid))) is None


def test_username_link_api_requires_session_csrf_and_returns_the_existing_proof_contract(context):
    db, _, commerce, client, email, path, documents = context
    name = "v10_" + uuid4().hex[:20]
    target = commerce.resolve_telegram(uuid4().int % 1000000000000 + 100000, username=name)
    payload = {"telegram_username": "@" + name.upper()}
    assert client.post("/api/v1/account/link/request", json=payload).status_code == 401
    login = client.post(
        "/api/v1/auth/email/verify",
        json=request_otp(client, email, path) | {"legal_accepted": True, "documents": documents},
    )
    assert login.status_code == 200
    assert client.post("/api/v1/account/link/request", json=payload).status_code == 403
    response = client.post(
        "/api/v1/account/link/request",
        json=payload,
        headers={"X-CSRF-Token": login.json()["csrf_token"]},
    )
    assert response.status_code == 200
    assert response.json()["expires_in"] == 300
    assert response.json()["requires_merge"] is True
    with db.transaction() as s:
        proof = s.get(MergeProof, response.json()["proof"].split(".")[0])
        assert proof.target_id == int(target["id"])
        assert s.get(Account, int(login.json()["account"]["id"])).telegram_id is None
