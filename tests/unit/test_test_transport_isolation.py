import json
import os


def test_test_runner_disables_live_transports_even_when_local_integrations_enabled(monkeypatch):
    from scripts import run_local

    names = ["ROLLYPAY", "PANEL", "PANEL_WEBHOOK", "PUSH", "SMTP", "EMOJI", "RECURRING"]
    for name in names:
        monkeypatch.setenv(f"SUNRISE_{name}_TEST_AUTHORIZED", "true")
    monkeypatch.setenv("SUNRISE_LOCAL_TEST", "false")
    monkeypatch.setenv("SUNRISE_SMTP_TUNNEL_PORT", "1587")
    monkeypatch.setenv("SUNRISE_TURNSTILE_TEST_BYPASS", "false")
    monkeypatch.setenv("SUNRISE_TELEGRAM_MINIAPP_ENABLED", "true")
    monkeypatch.setenv("SUNRISE_TELEGRAM_LOGIN_CLIENT_SECRET", "synthetic-secret")
    placeholder = "synthetic-do-not-print"
    monkeypatch.setenv("SUNRISE_PANEL_TOKEN", placeholder)
    monkeypatch.setenv("SUNRISE_BOT_TEST_TOKEN", placeholder)
    run_local.isolate_test_transports()
    assert all(os.environ[f"SUNRISE_{name}_TEST_AUTHORIZED"] == "false" for name in names)
    assert os.environ["SUNRISE_LOCAL_TEST"] == "true"
    assert os.environ["SUNRISE_PANEL_TOKEN"] == placeholder
    assert os.environ["SUNRISE_BOT_TEST_TOKEN"] == ""
    assert os.environ["SUNRISE_SMTP_TUNNEL_PORT"] == ""
    assert os.environ["SUNRISE_TURNSTILE_TEST_BYPASS"] == "true"
    assert os.environ["SUNRISE_TELEGRAM_MINIAPP_ENABLED"] == "false"
    assert os.environ["SUNRISE_TELEGRAM_LOGIN_CLIENT_SECRET"] == ""


def test_test_runner_keeps_synthetic_mail_recipients_only(monkeypatch):
    from scripts import run_local

    monkeypatch.setenv("SUNRISE_EMAIL_ALLOWLIST", json.dumps(["customer@sunrise.test", "owner@example.com"]))
    run_local.isolate_test_transports()
    assert json.loads(os.environ["SUNRISE_EMAIL_ALLOWLIST"]) == ["customer@sunrise.test"]
