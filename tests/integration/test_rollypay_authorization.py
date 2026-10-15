import hashlib
import hmac
import json
import os
import time
from uuid import uuid4

import fakeredis
import pytest
import sunrise_api.main as api
import sunrise_infrastructure.providers as provider_module
from fastapi.testclient import TestClient
from sqlalchemy import select
from sunrise_infrastructure.db import Inbox
from sunrise_infrastructure.security import digest
from sunrise_infrastructure.settings import Settings


def test_disabled_outbound_integration_still_durably_accepts_signed_test_callback(tmp_path, monkeypatch):
    url = os.getenv("SUNRISE_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Synthetic PostgreSQL required")

    def forbidden(*args, **kwargs):
        pytest.fail("Callback acceptance must not compose an outbound provider")

    monkeypatch.setattr(provider_module, "RollyPay", forbidden)
    signing = "s" * 48
    config = Settings(
        _env_file=None,
        database_url=url,
        secret_key="x" * 32,
        local_test=True,
        mail_dir=str(tmp_path),
        allowed_hosts=["testserver"],
        rollypay_test_authorized=False,
        rollypay_api_key="synthetic-api-key",
        rollypay_signing_secret=signing,
        rollypay_terminal="synthetic-terminal",
    )
    app = api.create_app(config, redis_client=fakeredis.FakeRedis())
    payload = {
        "payment_id": "synthetic-" + str(uuid4()),
        "order_id": str(uuid4()),
        "status": "paid",
        "amount": "1.00",
        "currency": "RUB",
        "test": True,
    }
    raw = json.dumps(payload).encode()
    timestamp = str(int(time.time()))
    signature = hmac.new(signing.encode(), timestamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
    with TestClient(app) as client:
        for _ in range(2):
            response = client.post(
                "/api/v1/webhooks/rollypay",
                content=raw,
                headers={"X-Signature": signature, "X-Timestamp": timestamp},
            )
            assert response.status_code == 200
    with app.state.commerce.db.transaction() as session:
        rows = list(session.scalars(select(Inbox).where(Inbox.event_key == digest(raw.decode()))))
        assert len(rows) == 1
        assert rows[0].payload == payload
    app.state.commerce.db.engine.dispose()
