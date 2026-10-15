import os
from uuid import uuid4

from fastapi.testclient import TestClient
from redis import Redis
from sunrise_api.main import create_app

from tests.e2e.local_auth_server import SyntheticSettings


def test_asset_success_cache_and_unavailable_assets_do_not_block_login(tmp_path):
    email = str(uuid4()) + "@example.test"
    settings = SyntheticSettings(
        _env_file=None,
        database_url=os.environ["SUNRISE_TEST_DATABASE_URL"],
        secret_key="s" * 48,
        local_test=True,
        turnstile_test_bypass=True,
        allowed_hosts=["testserver"],
        mail_dir=str(tmp_path),
        email_allowlist=[email],
    )

    class Assets:
        def get(self, value):
            return (b"\x89PNG\r\n\x1a\nfixture", "image/png") if value == "90071992547409930001" else None

    redis = Redis.from_url(os.environ["SUNRISE_TEST_REDIS_URL"])
    app = create_app(settings, redis_client=redis, emoji_assets=Assets())
    ip = "2001:db8::" + uuid4().hex[:4]
    with TestClient(app, client=(ip, 50000)) as c:
        image = c.get("/api/v1/content/emoji/90071992547409930001")
        assert image.status_code == 200 and image.headers["Cache-Control"] == "private, max-age=300"
        assert image.headers["Content-Type"] == "image/png"
        for _ in range(3):
            for i in range(20):
                assert c.get("/api/v1/content/emoji/" + str(i)).status_code == 404
        result = c.post(
            "/api/v1/auth/email/request",
            json={"email": email, "turnstile_token": "local-test"},
        )
        assert result.status_code == 200, "Known unavailable asset routes must not block checkout/login IP"
        assert result.headers["Cache-Control"] == "no-store"
    app.state.commerce.db.engine.dispose()
    redis.close()
