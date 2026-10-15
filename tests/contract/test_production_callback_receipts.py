import hashlib
import hmac
import json
import time
from contextlib import contextmanager
from types import SimpleNamespace

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sunrise_infrastructure.settings import Settings


@pytest.fixture
def callback_client(monkeypatch):
    from sunrise_api import main
    from sunrise_infrastructure import providers, security, settings

    receipts = []
    session = SimpleNamespace(execute=lambda statement: receipts.append(statement.compile().params))

    @contextmanager
    def transaction():
        yield session

    policy = SimpleNamespace(
        production=True,
        scope=SimpleNamespace(
            scope_id="synthetic-project", rollypay=SimpleNamespace(terminal="synthetic-terminal")
        ),
        require=lambda *args: None,
        require_current=lambda *args: None,
    )
    config = Settings(
        _env_file=None,
        instance_config_path=None,
        local_test=False,
        database_url="postgresql+psycopg://synthetic@127.0.0.1:55439/sunrise_test",
        secret_key="synthetic-callback-session-key-1234567890",  # noqa: S106
        rollypay_signing_secret="synthetic-callback-signature-key",  # noqa: S106
        allowed_hosts=["testserver"],
    )
    monkeypatch.setattr(main, "Database", lambda *args: SimpleNamespace(transaction=transaction))

    def configured(config, database):
        object.__setattr__(config, "_runtime_policy", policy)
        return policy

    monkeypatch.setattr(settings, "configured_runtime", configured)
    monkeypatch.setattr(security, "configured_redis", lambda *args, **kwargs: fakeredis.FakeRedis())
    monkeypatch.setattr(providers, "configured_payments", lambda *args: None)
    monkeypatch.setattr(main, "configured_mail", lambda *args, **kwargs: SimpleNamespace())
    monkeypatch.setattr(main, "configured_media_store", lambda *args: SimpleNamespace())
    monkeypatch.setattr(main, "configured_panel", lambda *args: None)
    client = TestClient(main.create_app(settings=config))

    def send(event):
        raw = json.dumps(event, separators=(",", ":")).encode()
        timestamp = str(int(time.time()))
        signature = hmac.new(
            config.rollypay_signing_secret.encode(), timestamp.encode() + b"." + raw, hashlib.sha256
        ).hexdigest()
        return client.post(
            "/api/v1/webhooks/rollypay",
            content=raw,
            headers={"x-timestamp": timestamp, "x-signature": signature},
        ), raw

    return send, receipts


@pytest.mark.parametrize("test_field", [None, False])
def test_signed_official_live_callback_without_environment_is_durable_without_financial_effect(
    callback_client, test_field
):
    send, receipts = callback_client
    event = {
        "event_type": "payment.completed",
        "payment_id": "synthetic-unknown-payment",
        "order_id": "synthetic-unknown-order",
        "status": "completed",
        "amount": "1.00",
        "currency": "RUB",
    }
    if test_field is not None:
        event["test"] = test_field
    response, raw = send(event)
    assert response.status_code == 200
    assert len(receipts) == 1
    payload = receipts[0]["payload"]
    assert "environment" not in payload
    assert payload["receipt"]["signed_body"] == raw.decode()
    assert payload["receipt"]["scope_id"] == "synthetic-project"


@pytest.mark.parametrize(
    "delta", [{"test": True}, {"test": 0}, {"test": None}, {"environment": "sandbox"}, {"environment": None}]
)
def test_conflicting_or_malformed_live_callback_context_denies_intake(callback_client, delta):
    send, receipts = callback_client
    response, _ = send({"payment_id": "synthetic-payment", "order_id": "synthetic-order"} | delta)
    assert response.status_code == 403 and receipts == []
