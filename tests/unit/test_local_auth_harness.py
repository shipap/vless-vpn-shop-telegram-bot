import pytest
from sqlalchemy.engine import make_url


def test_browser_auth_harness_ignores_ambient_credentials_and_authorizations(monkeypatch, tmp_path):
    from tests.e2e.local_auth_server import synthetic_settings

    for service in ("SMTP", "ROLLYPAY", "PANEL", "EMOJI", "RECURRING"):
        monkeypatch.setenv(f"SUNRISE_{service}_TEST_AUTHORIZED", "true")
    monkeypatch.setenv("SUNRISE_SMTP_PASSWORD", "synthetic-ambient-value")
    monkeypatch.setenv("SUNRISE_TELEGRAM_MINIAPP_ENABLED", "true")
    monkeypatch.setenv("SUNRISE_OWNER_ID", "999999999")
    monkeypatch.setenv("SUNRISE_BOT_URL", "https://t.me/ambient_bot")
    config = synthetic_settings(
        "postgresql+psycopg://synthetic@127.0.0.1:55439/sunrise_test_backend",
        tmp_path / "mail",
        "JBSWY3DPEHPK3PXP",
    )
    assert config.local_test and config.turnstile_test_bypass
    assert config.secret_key == "isolated-browser-auth-secret-key-" * 2
    assert set(config.email_allowlist) == {
        "customer@sunrise.test",
        "owner@sunrise.test",
        "credit@sunrise.test",
    }
    assert config.admin_email_allowlist == ["owner@sunrise.test"]
    assert config.admin_totp_secrets == {"owner@sunrise.test": "JBSWY3DPEHPK3PXP"}
    assert config.smtp_password is None and config.bot_test_token is None
    assert config.owner_id is None
    assert config.bot_url == "https://t.me/synthetic_fixture_bot"
    assert config.landing_url == "http://127.0.0.1:3110/"
    assert not config.telegram_miniapp_enabled
    assert config.telegram_login_client_id is None and config.telegram_login_client_secret is None
    assert all(
        not getattr(config, service.lower() + "_test_authorized")
        for service in ("SMTP", "ROLLYPAY", "PANEL", "EMOJI", "RECURRING")
    )


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://synthetic@127.0.0.1:5432/sunrise_test",
        "postgresql+psycopg://synthetic@remote.example.test:55439/sunrise_test",
        "postgresql+psycopg://synthetic@127.0.0.1:55439/sunrise_legacy",
        "postgresql+psycopg://synthetic@127.0.0.1:55439/production",
        "sqlite:///:memory:",
    ],
)
def test_browser_auth_harness_rejects_an_unapproved_database_before_schema_creation(url):
    from tests.e2e.local_auth_server import backend_url

    with pytest.raises(ValueError):
        backend_url(url)


def test_browser_auth_harness_uses_only_exact_backend_database():
    from tests.e2e.local_auth_server import backend_url

    result = make_url(backend_url("postgresql+psycopg://synthetic@127.0.0.1:55439/sunrise_test"))
    assert result.host == "127.0.0.1" and result.port == 55439 and result.database == "sunrise_test_backend"


@pytest.mark.parametrize("schema", ["public", "e2e_auth_test_invalid", "e2e_auth_test_" + "a" * 32 + ";"])
def test_fixture_reset_rejects_foreign_schema_before_touching_database(schema):
    from tests.e2e.local_auth_server import reset_fixture

    with pytest.raises(RuntimeError, match="invalid_isolated_schema"):
        reset_fixture(None, None, schema, "ui", None)


def test_fixture_reset_rejects_foreign_database_before_touching_tables():
    from types import SimpleNamespace

    from tests.e2e.local_auth_server import reset_fixture

    engine = SimpleNamespace(url=make_url("postgresql://synthetic@127.0.0.1:55439/sunrise_test"))
    db = SimpleNamespace(engine=engine)
    with pytest.raises(RuntimeError, match="isolated_database_not_allowed"):
        reset_fixture(db, None, "e2e_auth_test_" + "a" * 32, "ui", None)
