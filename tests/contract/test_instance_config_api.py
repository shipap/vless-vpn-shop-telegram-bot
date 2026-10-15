import fakeredis
from fastapi.testclient import TestClient
from sunrise_api.main import create_app
from sunrise_infrastructure import instance_config

from tests.e2e.local_auth_server import SyntheticSettings


def test_public_config_exposes_explicit_neutral_instance_without_secrets():
    settings = SyntheticSettings(
        _env_file=None,
        database_url="postgresql+psycopg://synthetic:synthetic@127.0.0.1:55439/sunrise_test_backend",
        secret_key="synthetic-instance-config-key-" * 2,
        local_test=True,
        allowed_hosts=["testserver"],
        instance_config_path=None,
    )
    app = create_app(settings, redis_client=fakeredis.FakeRedis())
    try:
        with TestClient(app) as client:
            response = client.get("/api/v1/public-config")
        assert response.status_code == 200
        data = response.json()
        assert data.get("instance") == {
            "app_name": "VLESS VPN Shop",
            "app_short_name": "VLESS VPN Shop",
            "app_slug": "vpn-shop",
            "app_description": "Подписки VPN и управление подключением.",
            "visual_profile": "neutral",
            "search_indexing_enabled": False,
            "accent_color": "#F0A45D",
            "public_site_url": None,
            "dashboard_url": None,
            "telegram_bot_username": None,
            "support_url": None,
            "support_email": None,
            "sender_name": "VLESS VPN Shop",
            "logo_url": None,
            "favicon_url": None,
            "hero_url": None,
            "og_image_url": None,
        }
        assert "synthetic-instance-config-key" not in response.text
        assert "synthetic:synthetic" not in response.text
    finally:
        app.state.commerce.db.engine.dispose()


def test_json_brand_and_explicit_links_have_one_public_and_legacy_projection(tmp_path, monkeypatch):
    monkeypatch.setattr(instance_config, "PROJECT_ROOT", tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config/instance.json").write_text(
        '{"app_name":"Другой магазин","dashboard_url":"https://dashboard.example.test",'
        '"telegram_bot_username":"example_test_bot","support_url":"https://support.example.test"}',
        encoding="utf-8",
    )
    settings = SyntheticSettings(
        _env_file=None,
        instance_config_path="config/instance.json",
        database_url="postgresql+psycopg://synthetic:synthetic@127.0.0.1:55439/sunrise_test_backend",
        secret_key="synthetic-instance-config-key-" * 2,
        local_test=True,
        allowed_hosts=["testserver"],
        dashboard_url="http://127.0.0.1:3210",
    )
    app = create_app(settings, redis_client=fakeredis.FakeRedis())
    try:
        with TestClient(app) as client:
            data = client.get("/api/v1/public-config").json()
        assert data["instance"]["app_name"] == app.title == "Другой магазин"
        assert data["dashboard_url"] == data["instance"]["dashboard_url"] == "http://127.0.0.1:3210"
        assert data["bot_url"] == "https://t.me/" + data["instance"]["telegram_bot_username"]
        assert data["support_url"] == data["instance"]["support_url"] == "https://support.example.test"
    finally:
        app.state.commerce.db.engine.dispose()
