"""Signed callbacks acknowledge only committed receipts and expose retryable DB failure."""

import hashlib
import hmac
import json
import time

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.exc import OperationalError
from sunrise_api.main import create_app
from sunrise_infrastructure.db import Inbox, PanelEvent

from tests.integration.test_subscription_engagement import paid_creation  # noqa: F401
from tests.integration.test_v11_panel_events import owned_panel, signed_event  # noqa: F401


@pytest.mark.parametrize("provider", ["remnawave", "rollypay"])
def test_signed_webhook_commit_failure_returns_503_then_retry_and_duplicate_ack_200(paid_creation, provider):  # noqa: F811
    db, panel, commerce, config, state, _, _, _, _ = paid_creation
    assert commerce.provisioning.run_jobs() == 1
    config.allowed_hosts = ["testserver"]
    config.rollypay_signing_secret = "s" * 48
    app = create_app(config, redis_client=fakeredis.FakeRedis(), panel_provider=panel)
    if provider == "remnawave":
        raw, headers = signed_event()
        model = PanelEvent
    else:
        raw = json.dumps(
            {
                "payment_id": "synthetic-payment",
                "order_id": "synthetic-order",
                "status": "paid",
                "amount": "10.00",
                "currency": "RUB",
                "test": True,
            }
        ).encode()
        timestamp = str(int(time.time()))
        headers = {
            "X-Timestamp": timestamp,
            "X-Signature": hmac.new(
                config.rollypay_signing_secret.encode(), timestamp.encode() + b"." + raw, hashlib.sha256
            ).hexdigest(),
        }
        model = Inbox
    path = "/api/v1/webhooks/" + provider
    state["calls"].clear()
    with TestClient(app) as client:
        invalid = dict(headers)
        invalid["X-Remnawave-Signature" if provider == "remnawave" else "X-Signature"] = "0" * 64
        assert client.post(path, content=raw, headers=invalid).status_code in {401, 403}

        def fail_commit(session):
            raise OperationalError(None, None, RuntimeError("synthetic-private-database-detail"))

        event.listen(app.state.commerce.db.sessions, "before_commit", fail_commit)
        try:
            failed = client.post(path, content=raw, headers=headers)
        finally:
            event.remove(app.state.commerce.db.sessions, "before_commit", fail_commit)
        assert failed.status_code == 503
        assert failed.json()["error"]["retryable"] is True
        assert "synthetic-private-database-detail" not in failed.text
        with db.transaction() as session:
            assert session.scalar(select(func.count()).select_from(model)) == 0
        for _ in range(2):
            accepted = client.post(path, content=raw, headers=headers)
            assert accepted.status_code == 200
        with db.transaction() as session:
            assert session.scalar(select(func.count()).select_from(model)) == 1
    assert state["calls"] == []
    app.state.commerce.db.engine.dispose()
