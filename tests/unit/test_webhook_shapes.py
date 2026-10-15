import hashlib
import hmac
import time

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sunrise_api.main import create_app
from sunrise_infrastructure.settings import Settings


@pytest.mark.parametrize("raw", [b"[]", b"null", b'"paid"', b"true", b"123"])
def test_signed_non_object_callback_is_rejected_without_database_access(tmp_path, raw):
    secret = "synthetic-webhook-signing-key"  # noqa: S105
    config = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://synthetic@127.0.0.1:55439/sunrise_test_backend",
        secret_key="synthetic-application-key" * 3,
        local_test=True,
        smtp_test_authorized=False,
        smtp_tunnel_port=None,
        rollypay_test_authorized=False,
        rollypay_signing_secret=secret,
        mail_dir=str(tmp_path),
        allowed_hosts=["testserver"],
    )
    app = create_app(config, redis_client=fakeredis.FakeRedis())
    stamp = str(int(time.time()))
    signature = hmac.new(secret.encode(), stamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/webhooks/rollypay",
                content=raw,
                headers={"X-Timestamp": stamp, "X-Signature": signature},
            )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "event_invalid"
    finally:
        app.state.commerce.db.engine.dispose()
