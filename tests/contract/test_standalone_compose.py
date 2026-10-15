"""Compose-конфигурация согласована с настоящими runtime guards и не открывает провайдеров."""

import importlib.util
import json
import os
import re
from pathlib import Path
from unittest.mock import patch

import pytest
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]


def local_setup():
    spec = importlib.util.spec_from_file_location("setup_local_docker", ROOT / "scripts/setup_local_docker.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def local_profile(tmp_path):
    for name in (".gitignore", ".dockerignore"):
        (tmp_path / name).write_text(".env.*\n!.env.example\n", encoding="utf-8")
    local_setup().prepare(tmp_path)
    environment = dict(dotenv_values(tmp_path / ".env.docker"))
    source = (ROOT / "infra/compose.standalone.json").read_text(encoding="utf-8")
    resolved = re.sub(r"\$\{([A-Z_]+)(?::[^}]*)?\}", lambda match: environment[match[1]], source)
    return environment, json.loads(resolved)


def test_backend_services_pass_real_settings_and_leave_external_adapters_disabled(local_profile):
    from sunrise_infrastructure.mail import configured_mail
    from sunrise_infrastructure.panel import configured_panel
    from sunrise_infrastructure.providers import configured_payments
    from sunrise_infrastructure.settings import Settings, configured_runtime

    environment, compose = local_profile
    for name in ("init", "api", "worker"):
        combined = environment | compose["services"][name]["environment"]
        with patch.dict(os.environ, combined, clear=True):
            settings = Settings(_env_file=None)
            policy = configured_runtime(settings, object())
            assert settings.local_test and not policy.production
            assert configured_mail(settings) is None
            assert configured_panel(settings) is None
            assert configured_payments(settings) is None
            assert settings.admin_telegram_allowlist == []
            assert settings.email_allowlist == []


def test_local_namespace_publishes_only_loopback_web_and_denies_egress(local_profile):
    environment, compose = local_profile
    services = compose["services"]
    assert compose["networks"]["local"]["internal"] is True
    assert "ports" not in services["postgres"]
    assert services["ingress"]["ports"] == [
        f"127.0.0.1:{environment['LOCAL_LANDING_PORT']}:3100",
        f"127.0.0.1:{environment['LOCAL_DASHBOARD_PORT']}:3200",
    ]
    for name, service in services.items():
        if name not in {"postgres", "ingress"}:
            assert service["network_mode"] == "service:postgres"
            assert "ports" not in service and "networks" not in service
            assert service["read_only"] is True
            assert service["cap_drop"] == ["ALL"]
            assert service["security_opt"] == ["no-new-privileges:true"]
    ingress = services["ingress"]
    assert ingress["networks"] == ["local", "edge"]
    assert "env_file" not in ingress and "environment" not in ingress
    assert ingress["read_only"] is True
    assert ingress["cap_drop"] == ["ALL"]
    assert ingress["security_opt"] == ["no-new-privileges:true"]
    assert compose["networks"]["edge"].get("internal", False) is False
    relay = (ROOT / "infra/nginx.local.conf").read_text(encoding="utf-8")
    assert "proxy_pass http://postgres:3100;" in relay
    assert "proxy_pass http://postgres:3200;" in relay
    assert "proxy_pass $" not in relay
    for temporary_path in ("client_body", "proxy", "fastcgi", "uwsgi", "scgi"):
        assert f"{temporary_path}_temp_path /tmp/" in relay
    for name in ("landing", "dashboard"):
        assert "env_file" not in services[name]
        assert services[name]["environment"] == {"WEB_API_BASE_URL": "http://127.0.0.1:8100"}


def test_failed_initialization_prevents_application_and_frontend_start(local_profile):
    _, compose = local_profile
    services = compose["services"]
    assert services["init"]["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert services["init"]["depends_on"]["redis"]["condition"] == "service_healthy"
    assert services["api"]["depends_on"]["init"]["condition"] == "service_completed_successfully"
    for name in ("worker", "landing", "dashboard"):
        assert services[name]["depends_on"]["api"]["condition"] == "service_healthy"


@pytest.mark.parametrize("override", [{"SUNRISE_LOCAL_TEST": "false"}, {"SUNRISE_PANEL_TEST_AUTHORIZED": "true"}])
def test_init_rejects_nonlocal_or_authorized_transport_before_subprocess(local_profile, override):
    environment, _ = local_profile
    with patch.dict(os.environ, environment | override, clear=True), patch(
        "subprocess.run", side_effect=AssertionError("Недопустимый запуск миграции")
    ), pytest.raises(ValueError):
        local_setup().initialize()
