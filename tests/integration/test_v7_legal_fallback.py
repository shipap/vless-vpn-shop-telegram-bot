import json

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sunrise_api.main import create_app
from sunrise_core.errors import DomainError
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import Account, LegalConsent, Session, Setting

from tests.e2e.local_auth_server import SyntheticSettings
from tests.isolated_database import isolated_database

DOCUMENTS = [
    {"document_type": "offer", "version": "owner-v1", "url": "https://example.test/offer"},
    {"document_type": "privacy", "version": "owner-v1", "url": "https://example.test/privacy"},
]
EMPTY_DOCUMENTS = [
    {"document_type": "offer", "version": "", "url": ""},
    {"document_type": "privacy", "version": "", "url": ""},
]


@pytest.fixture
def context():
    with isolated_database("legal_fallback") as db:
        commerce = Commerce(db, local_test=True)
        with db.transaction() as session:
            row = session.get(Setting, "legal")
            if row:
                session.delete(row)
            account = Account(email="legal-fallback@example.test")
            session.add(account)
            session.flush()
            account_id = str(account.id)
        yield commerce, account_id


def test_missing_public_documents_and_real_otp_auth_fail_closed(context, tmp_path):
    commerce, account_id = context
    settings = SyntheticSettings(
        _env_file=None,
        database_url=commerce.db.engine.url.render_as_string(hide_password=False),
        secret_key="synthetic-legal-fallback-key-" * 2,
        local_test=True,
        turnstile_test_bypass=True,
        allowed_hosts=["testserver"],
        email_allowlist=["legal-fallback@example.test"],
        mail_dir=str(tmp_path),
        # Instance values do not silently configure/overwrite DB documents during reads.
        legal_offer_url="https://example.test/instance-offer",
        legal_privacy_url="https://example.test/instance-privacy",
        legal_offer_version="instance-v1",
        legal_privacy_version="instance-v1",
    )
    app = create_app(settings, redis_client=fakeredis.FakeRedis())
    try:
        with TestClient(app) as client:
            response = client.get("/api/v1/legal")
            assert response.status_code == 200
            assert response.json()["configured"] is False
            assert response.json()["documents"] == EMPTY_DOCUMENTS
            assert "sunrise" not in response.text.casefold()
            requested = client.post(
                "/api/v1/auth/email/request",
                json={"email": "legal-fallback@example.test", "turnstile_token": "local-test"},
            )
            assert requested.status_code == 200
            cid = requested.json()["challenge_id"]
            code = json.loads((tmp_path / (cid + ".json")).read_text())["code"]
            denied = client.post(
                "/api/v1/auth/email/verify",
                json={"challenge_id": cid, "code": code, "legal_accepted": True, "documents": DOCUMENTS},
            )
            assert denied.status_code == 503
            assert denied.json()["error"]["code"] == "legal_not_configured"
            assert "sunrise_session" not in client.cookies
        with commerce.db.transaction() as session:
            assert session.get(Setting, "legal") is None
            assert session.scalar(select(func.count()).select_from(LegalConsent)) == 0
            assert session.scalar(select(func.count()).select_from(Session)) == 0
            assert not session.get(Account, int(account_id)).email_verified
    finally:
        app.state.commerce.db.engine.dispose()


def test_explicit_setup_preserves_existing_valid_documents_and_consent_history(context):
    commerce, account_id = context
    configured = commerce.legal.configure(None, DOCUMENTS, "Явная настройка владельцем", only_if_missing=True)
    assert configured["configured"]
    accepted = commerce.accept_legal(account_id, DOCUMENTS, "telegram", "first_use")
    different = [d | {"url": d["url"] + "-other", "version": "another-v2"} for d in DOCUMENTS]
    assert (
        commerce.legal.configure(None, different, "Повторная настройка экземпляра", only_if_missing=True)
        == configured
    )
    state = commerce.legal_status(account_id)
    assert state["documents"] == DOCUMENTS and state["accepted"]
    assert state["history"] == accepted["history"]
    assert commerce.legal.missing()["total"] == 0


@pytest.mark.parametrize(
    "stored",
    [
        [],
        {"documents": [DOCUMENTS[0]]},
        {"documents": [DOCUMENTS[0], DOCUMENTS[1] | {"url": "https://example.test/\nprivate"}]},
    ],
)
def test_invalid_database_row_preserves_history_and_denies_new_acceptance(context, stored):
    commerce, account_id = context
    commerce.legal.configure(None, DOCUMENTS, "Явная настройка документов")
    accepted = commerce.accept_legal(account_id, DOCUMENTS, "website", "registration")
    with commerce.db.transaction() as session:
        session.get(Setting, "legal").value = stored
    state = commerce.legal_status(account_id)
    assert state["configured"] is False and state["accepted"] is False
    assert state["documents"] == EMPTY_DOCUMENTS
    assert state["history"] == accepted["history"]
    assert commerce.legal.missing()["total"] == 1
    with pytest.raises(DomainError) as blocked:
        commerce.accept_legal(account_id, DOCUMENTS, "telegram", "first_use")
    assert blocked.value.code == "legal_not_configured"
    with pytest.raises(DomainError) as blocked:
        with commerce.db.transaction() as session:
            commerce.legal.require_current(session, session.get(Account, int(account_id)))
    assert blocked.value.code == "legal_not_configured"
    with commerce.db.transaction() as session:
        assert session.get(Setting, "legal").value == stored
        assert session.scalar(select(func.count()).select_from(LegalConsent)) == 2
