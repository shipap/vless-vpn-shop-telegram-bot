import importlib
import os

import pytest


def runner_module():
    try:
        return importlib.import_module("scripts.run_isolated_tests")
    except ModuleNotFoundError:
        pytest.fail("Fresh owned-schema test runner is missing")


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://synthetic@127.0.0.1:5432/sunrise_test_backend",
        "postgresql+psycopg://synthetic@production.example.test:55439/sunrise_test_backend",
        "postgresql+psycopg://synthetic@127.0.0.1:55439/sunrise_legacy",
        "postgresql+psycopg://synthetic@127.0.0.1:55439/sunrise_test",
        "sqlite:///test.db",
        "postgresql+psycopg://synthetic@127.0.0.1:55439/sunrise_test_backend?options=-csearch_path=public,other",
    ],
)
def test_unapproved_backend_never_opens_a_connection(monkeypatch, url):
    module = runner_module()
    monkeypatch.setattr(module, "create_engine", lambda *args, **kwargs: pytest.fail("Connection attempted"))
    with pytest.raises(ValueError):
        module.run_tests(
            [],
            environ={
                "SUNRISE_TEST_DATABASE_URL": url,
                "SUNRISE_BOT_TEST_DATABASE_URL": "postgresql+psycopg://127.0.0.1:55439/sunrise_test_bot",
            },
        )


def test_unapproved_bot_is_rejected_before_backend_schema_creation(monkeypatch):
    module = runner_module()
    monkeypatch.setattr(module, "create_engine", lambda *args, **kwargs: pytest.fail("Connection attempted"))
    with pytest.raises(ValueError):
        module.run_tests(
            [],
            environ={
                "SUNRISE_TEST_DATABASE_URL": "postgresql+psycopg://127.0.0.1:55439/sunrise_test_backend",
                "SUNRISE_BOT_TEST_DATABASE_URL": "postgresql+psycopg://127.0.0.1:55439/sunrise_legacy",
            },
        )


def test_missing_explicit_urls_never_load_runtime_dotenv_or_connect(monkeypatch):
    module = runner_module()
    import dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: pytest.fail("Runtime dotenv loaded"))
    monkeypatch.setattr(module, "create_engine", lambda *args, **kwargs: pytest.fail("Connection attempted"))
    with pytest.raises(ValueError):
        module.run_tests([], environ={})


@pytest.mark.parametrize(
    "redis_url",
    [
        "redis://production.example.test:56379/0",
        "redis://127.0.0.1:6379/0",
        "redis://127.0.0.1:56379/1",
    ],
)
def test_unapproved_redis_is_rejected_before_any_database_connection(monkeypatch, redis_url):
    module = runner_module()
    monkeypatch.setattr(module, "create_engine", lambda *args, **kwargs: pytest.fail("Connection attempted"))
    with pytest.raises(ValueError):
        module.run_tests(
            [],
            environ={
                "SUNRISE_TEST_DATABASE_URL": "postgresql+psycopg://127.0.0.1:55439/sunrise_test_backend",
                "SUNRISE_BOT_TEST_DATABASE_URL": "postgresql+psycopg://127.0.0.1:55439/sunrise_test_bot",
                "SUNRISE_TEST_REDIS_URL": redis_url,
            },
        )


def test_test_environment_disables_external_authorizations_and_preserves_parent(monkeypatch):
    module = runner_module()
    before = dict(os.environ)
    source = {
        "SUNRISE_LOCAL_TEST": "false",
        "SUNRISE_BOT_TEST_TOKEN": "synthetic-token",
        "SUNRISE_SMTP_TUNNEL_PORT": "4222",
        "SUNRISE_EMAIL_ALLOWLIST": '["live@example.org", "safe@example.test"]',
        "SUNRISE_TELEGRAM_LOGIN_CLIENT_SECRET": "synthetic-secret",
    }
    source.update(
        {
            f"SUNRISE_{name}_TEST_AUTHORIZED": "true"
            for name in (
                "ROLLYPAY",
                "PANEL",
                "PANEL_WEBHOOK",
                "PUSH",
                "SMTP",
                "EMOJI",
                "RECURRING",
            )
        }
    )
    safe = module.test_environment(source)
    assert safe["SUNRISE_LOCAL_TEST"] == "true"
    assert safe["SUNRISE_BOT_TEST_TOKEN"] == safe["SUNRISE_SMTP_TUNNEL_PORT"] == ""
    assert safe["SUNRISE_TELEGRAM_LOGIN_CLIENT_SECRET"] == ""
    assert safe["SUNRISE_EMAIL_ALLOWLIST"] == '["safe@example.test"]'
    assert all(
        safe[f"SUNRISE_{name}_TEST_AUTHORIZED"] == "false"
        for name in (
            "ROLLYPAY",
            "PANEL",
            "PANEL_WEBHOOK",
            "PUSH",
            "SMTP",
            "EMOJI",
            "RECURRING",
        )
    )
    assert dict(os.environ) == before
    assert source["SUNRISE_LOCAL_TEST"] == "false"
