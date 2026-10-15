"""Локальные отрицательные проверки протокола CAPTCHA; внешняя Cloudflare не вызывается."""

import os
from uuid import uuid4

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sunrise_api.main import create_app
from sunrise_infrastructure.settings import Settings


@pytest.fixture
def captcha_client(tmp_path):
    url = os.getenv("SUNRISE_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Нужна отдельная синтетическая PostgreSQL БД")
    settings = Settings(
        database_url=url,
        local_test=True,
        secret_key="s" * 48,
        turnstile_secret="c" * 40,
        turnstile_hostname="isolated.sunrise.test",
        email_allowlist=[],
        mail_dir=str(tmp_path),
        allowed_hosts=["testserver"],
        allowed_origins=["http://testserver"],
    )
    with TestClient(create_app(settings, redis_client=fakeredis.FakeRedis())) as client:
        yield client


@pytest.mark.parametrize(
    "result",
    [
        {"success": True, "hostname": "attacker.test", "action": "login"},
        {"success": True, "hostname": "isolated.sunrise.test", "action": "checkout"},
        {"success": False, "hostname": "isolated.sunrise.test", "action": "login"},
    ],
)
def test_captcha_rejects_other_hostname_action_and_invalid_result(captcha_client, monkeypatch, result):
    class Response:
        def json(self):
            return result

    monkeypatch.setattr("sunrise_api.main.httpx.post", lambda *args, **kwargs: Response())
    response = captcha_client.post(
        "/api/v1/auth/email/request",
        json={"email": f"{uuid4()}@example.test", "turnstile_token": "synthetic-untrusted-proof"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "captcha_invalid"


def test_captcha_replay_is_revalidated_and_spoofed_ip_is_not_forwarded(captcha_client, monkeypatch):
    calls = []

    class Response:
        def json(self):
            return {"success": len(calls) == 1, "hostname": "isolated.sunrise.test", "action": "login"}

    def verify(endpoint, *, data, timeout):
        assert endpoint == "https://challenges.cloudflare.com/turnstile/v0/siteverify"
        assert data["remoteip"] == "testclient"
        assert data["response"] == "synthetic-single-use-proof"
        assert timeout == 10
        calls.append(True)
        return Response()

    monkeypatch.setattr("sunrise_api.main.httpx.post", verify)
    for attempt, expected in enumerate((200, 400)):
        response = captcha_client.post(
            "/api/v1/auth/email/request",
            headers={"X-Forwarded-For": "203.0.113.77", "CF-Connecting-IP": "203.0.113.88"},
            json={
                "email": f"captcha-{attempt}-{uuid4()}@example.test",
                "turnstile_token": "synthetic-single-use-proof",
            },
        )
        assert response.status_code == expected
    assert len(calls) == 2
