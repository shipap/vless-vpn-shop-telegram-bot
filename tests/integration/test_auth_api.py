import json
import os
from uuid import uuid4

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sunrise_api.main import create_app
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import Database
from sunrise_infrastructure.settings import Settings

from tests.legal_fixture import current_test_legal


@pytest.fixture
def client(tmp_path):
    url = os.getenv("SUNRISE_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Synthetic PostgreSQL required")
    email = f"{uuid4()}@example.test"
    settings = Settings(
        database_url=url,
        redis_url="redis://127.0.0.1:56379/0",
        secret_key="s" * 48,
        local_test=True,
        email_allowlist=[email],
        mail_dir=str(tmp_path),
        allowed_hosts=["testserver"],
        allowed_origins=["http://testserver"],
    )
    app = create_app(settings, redis_client=fakeredis.FakeRedis())
    db = Database(url)
    current_test_legal(Commerce(db))
    db.engine.dispose()
    with TestClient(app) as c:
        yield c, email, tmp_path


def login(c, email, path):
    r = c.post("/api/v1/auth/email/request", json={"email": email, "turnstile_token": "local-test"})
    assert r.status_code == 200, r.text
    cid = r.json()["challenge_id"]
    code = json.loads((path / f"{cid}.json").read_text())["code"]
    documents = c.get("/api/v1/legal").json()["documents"]
    r = c.post(
        "/api/v1/auth/email/verify",
        json={
            "challenge_id": cid,
            "code": code,
            "legal_accepted": True,
            "documents": documents,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_real_login_cookie_csrf_and_admin404(client):
    c, email, path = client
    assert c.get("/api/v1/admin/access").status_code == 404
    auth = login(c, email, path)
    assert auth["account"]["email_verified"] is True
    assert isinstance(auth["account"]["id"], str)
    assert c.get("/api/v1/dashboard").status_code == 200
    assert c.get("/api/v1/admin/overview").status_code == 404
    assert c.post("/api/v1/auth/logout").status_code == 403
    assert c.post("/api/v1/auth/logout", headers={"X-CSRF-Token": auth["csrf_token"]}).status_code == 200
    assert c.get("/api/v1/dashboard").status_code == 401


def test_otp_single_use_tenth_attempt_invalidates_challenge(client):
    c, email, path = client
    r = c.post("/api/v1/auth/email/request", json={"email": email, "turnstile_token": "local-test"})
    cid = r.json()["challenge_id"]
    code = json.loads((path / f"{cid}.json").read_text())["code"]
    bad = "000000" if code != "000000" else "111111"
    for _ in range(10):
        r = c.post("/api/v1/auth/email/verify", json={"challenge_id": cid, "code": bad})
        assert r.status_code in {400, 429}
    assert c.post("/api/v1/auth/email/verify", json={"challenge_id": cid, "code": code}).status_code == 429


def test_csrf_cross_origin_login_and_no_secret_validation_echo(client):
    c, email, path = client
    r = c.post(
        "/api/v1/auth/email/request",
        headers={"Origin": "https://evil.example"},
        json={"email": email, "turnstile_token": "local-test"},
    )
    assert r.status_code == 403
    r = c.post("/api/v1/auth/email/verify", json={"challenge_id": "a", "code": "sensitive-not-code"})
    assert "sensitive-not-code" not in r.text


def test_guards_refuse_private_database():
    with pytest.raises(ValueError):
        Settings(database_url="postgresql+psycopg://x:x@127.0.0.1:55439/sunrise_legacy", secret_key="s" * 48)
