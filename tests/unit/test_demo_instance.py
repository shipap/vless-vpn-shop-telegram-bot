import importlib

import pytest
from dotenv import dotenv_values


def implementation():
    assert importlib.util.find_spec("scripts.run_demo_instance") is not None, "Missing isolated demo launcher"
    return importlib.import_module("scripts.run_demo_instance")


def test_demo_environment_drops_private_identity_and_credentials(tmp_path):
    path = tmp_path / ".env.demo"
    path.write_text("APP_NAME='VPN Shop'\nSUNRISE_LOCAL_TEST=true\n", encoding="utf-8")
    result = implementation().clean_environment(
        {
            "PATH": "system-path",
            "APP_NAME": "Private",
            "SUNRISE_PANEL_TOKEN": "synthetic-private-token",
            "WEB_API_BASE_URL": "http://127.0.0.1:8100",
        },
        dotenv_values(path),
    )
    assert result["APP_NAME"] == "VPN Shop"
    assert "SUNRISE_PANEL_TOKEN" not in result and "WEB_API_BASE_URL" not in result
    assert result["PATH"] == "system-path"


def test_schema_database_guard_keeps_isolation_and_rejects_unknown_targets():
    schema = "demo_instance_" + "a" * 32
    schema_database_url = implementation().schema_database_url
    url = schema_database_url("postgresql+psycopg://example:example@127.0.0.1:55439/sunrise_test", schema)
    from sqlalchemy.engine import make_url

    parsed = make_url(url)
    assert parsed.database == "sunrise_test_backend"
    assert parsed.query["options"] == "-csearch_path=" + schema
    with pytest.raises(ValueError):
        schema_database_url("postgresql+psycopg://example:example@remote.example.test/db", schema)
    with pytest.raises(ValueError):
        schema_database_url(url, "public")


def test_demo_values_refuse_any_external_authorization(tmp_path):
    path = tmp_path / ".env.demo"
    path.write_text("SUNRISE_PANEL_TEST_AUTHORIZED=true\n", encoding="utf-8")
    with pytest.raises(ValueError):
        implementation().demo_values(path, project_root=tmp_path)


def test_typed_demo_settings_ignore_ambient_private_instance(monkeypatch):
    monkeypatch.setenv("APP_NAME", "Private name")
    monkeypatch.setenv("SUNRISE_PANEL_TEST_AUTHORIZED", "true")
    values = {
        "APP_NAME": "Нейтральный сервис",
        "SUNRISE_LOCAL_TEST": "true",
        "SUNRISE_DATABASE_URL": "postgresql+psycopg://example:example@127.0.0.1:55439/sunrise_test",
        "SUNRISE_SECRET_KEY": "synthetic-demo-signing-key-" * 2,
    }
    loader = getattr(implementation(), "settings_for_demo", None)
    assert loader is not None, "Missing typed isolated demo settings"
    settings = loader(values, "demo_instance_" + "b" * 32, 3102, 3202)
    assert settings.instance.app_name == "Нейтральный сервис"
    assert settings.instance.hero_asset is None
    assert settings.instance_config_path is None
    assert settings.panel_test_authorized is False and settings.smtp_test_authorized is False
    assert settings.instance.dashboard_url == "http://localhost:3202"
    assert settings.email_allowlist == settings.admin_email_allowlist == []
    assert settings.owner_email is None


def test_demo_allows_only_explicit_synthetic_customer_admin_and_owner_emails(tmp_path):
    from uuid import uuid4

    customer, admin = uuid4().hex + "@example.test", uuid4().hex + "@example.test"
    path = tmp_path / ".env.demo"
    path.write_text(
        "SUNRISE_LOCAL_TEST=true\nSUNRISE_DATABASE_URL=postgresql+psycopg://synthetic:synthetic@127.0.0.1:55439/sunrise_test_backend\n"
        + "SUNRISE_SECRET_KEY="
        + "synthetic-demo-signing-key-" * 2
        + "\n"
        + 'SUNRISE_EMAIL_ALLOWLIST=["'
        + customer
        + '","'
        + admin
        + '"]\n'
        + 'SUNRISE_ADMIN_EMAIL_ALLOWLIST=["'
        + admin
        + '"]\nSUNRISE_OWNER_EMAIL='
        + admin
        + "\n",
        encoding="utf-8",
    )
    module = implementation()
    configured = module.settings_for_demo(
        module.demo_values(path, project_root=tmp_path), "demo_instance_" + uuid4().hex, 3102, 3202
    )
    assert configured.email_allowlist == [customer, admin]
    assert configured.admin_email_allowlist == [admin] and configured.owner_email == admin
    assert configured.owner_id is None and configured.admin_telegram_allowlist == []
    assert configured.smtp_test_authorized is False


@pytest.mark.parametrize(
    "emails,admins,owner",
    [
        ('["person@example.com"]', "[]", ""),
        ('["person@example.test"]', '["other@example.test"]', ""),
        ('["person@example.test"]', "[]", "person@example.com"),
        ('["person@example.test"]', "[]", "other@example.test"),
        ('"person@example.test"', "[]", ""),
        ("[1]", "[]", ""),
    ],
)
def test_demo_email_guard_cannot_be_bypassed_by_direct_typed_loader(emails, admins, owner):
    values = {
        "SUNRISE_LOCAL_TEST": "true",
        "SUNRISE_DATABASE_URL": "postgresql+psycopg://synthetic:synthetic@127.0.0.1:55439/sunrise_test_backend",
        "SUNRISE_SECRET_KEY": "synthetic-demo-signing-key-" * 2,
        "SUNRISE_EMAIL_ALLOWLIST": emails,
        "SUNRISE_ADMIN_EMAIL_ALLOWLIST": admins,
        "SUNRISE_OWNER_EMAIL": owner,
    }
    with pytest.raises(ValueError):
        implementation().settings_for_demo(values, "demo_instance_" + "d" * 32, 3102, 3202)


def test_demo_start_plan_uses_separate_sources_ports_and_public_metadata():
    module = implementation()
    planner = getattr(module, "startup_plan", None)
    assert planner is not None, "Missing guarded demo startup plan"
    schema = "demo_instance_" + "c" * 32
    plan = planner(schema, 8102, 3102, 3202)
    assert plan["schema"] == schema
    assert plan["urls"] == {
        "api": "http://127.0.0.1:8102",
        "landing": "http://localhost:3102",
        "dashboard": "http://localhost:3202",
    }
    assert len(plan["commands"]) == 3
    assert plan["commands"][0][-1] == "api"
    assert plan["commands"][1][-4:] == ["--config", "apps/web/landing/vite.config.ts", "--host", "localhost"]
    assert plan["commands"][2][-4:] == [
        "--config",
        "apps/web/dashboard/vite.config.ts",
        "--host",
        "localhost",
    ]
    assert plan["web_environment"]["WEB_API_BASE_URL"] == "http://127.0.0.1:8102"
    assert plan["web_environment"]["WEB_INSTANCE_ID"] == "demo-" + "c" * 32
    assert "DATABASE_URL" not in str(plan) and "SECRET_KEY" not in str(plan)
    with pytest.raises(ValueError):
        planner("public", 8102, 3102, 3202)
    for forbidden in [8100, 8150, 3100, 3200, 3110, 3210]:
        with pytest.raises(ValueError):
            planner(schema, forbidden, 3102, 3202)


@pytest.mark.parametrize(
    "injection",
    [
        "SUNRISE_OWNER_ID=123",
        'SUNRISE_ADMIN_EMAIL_ALLOWLIST=["owner@example.test"]',
        "SUNRISE_PANEL_TOKEN=synthetic-token",
        "SUNRISE_PANEL_WEBHOOK_SECRET=synthetic-secret",
        "SUNRISE_TELEGRAM_MINIAPP_ENABLED=true",
        "SUNRISE_VISUAL_PROFILE=private",
        "SUNRISE_LOGO_ASSET_PATH=private.png",
    ],
)
def test_demo_rejects_external_credentials_and_private_targets(tmp_path, injection):
    path = tmp_path / ".env.demo"
    path.write_text("SUNRISE_LOCAL_TEST=true\n" + injection + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        implementation().demo_values(path, project_root=tmp_path)


def test_demo_filesystem_guard_denies_outside_and_reparse_parent(tmp_path, monkeypatch):
    module = implementation()
    guard = getattr(module, "safe_path", None)
    assert guard is not None, "Missing demo filesystem containment guard"
    assert (
        guard(tmp_path / ".private/demo-instance.json", project_root=tmp_path)
        == tmp_path / ".private/demo-instance.json"
    )
    with pytest.raises(ValueError):
        guard(tmp_path / "../outside/demo-instance.json", project_root=tmp_path)
    monkeypatch.setattr(
        "sunrise_infrastructure.instance_assets.reparse", lambda path: path == tmp_path / ".private"
    )
    with pytest.raises(ValueError):
        guard(tmp_path / ".private/demo-instance.json", project_root=tmp_path)


def test_demo_private_paths_reject_real_windows_junction(tmp_path):
    import os
    import shutil
    import subprocess

    if os.name != "nt":
        pytest.skip("Windows junction behavior")
    module = implementation()
    guard = getattr(module, "safe_path", None)
    assert guard is not None, "Missing demo filesystem containment guard"
    original = tmp_path / "ordinary"
    original.mkdir()
    (original / "demo-instance.json").write_text('{"untouched":true}', encoding="utf-8")
    link = tmp_path / ".private"
    result = subprocess.run(  # noqa: S603 -- fixed local junction argv within owned temp root
        [shutil.which("cmd.exe"), "/c", "mklink", "/J", str(link), str(original)],
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0
    try:
        assert link.is_junction()
        with pytest.raises(ValueError):
            guard(link / "demo-instance.json", project_root=tmp_path)
        assert (original / "demo-instance.json").read_text(encoding="utf-8") == '{"untouched":true}'
    finally:
        link.rmdir()


def test_demo_mail_sink_rejects_real_windows_junction_before_api_composition(tmp_path, monkeypatch):
    import os
    import shutil
    import subprocess

    if os.name != "nt":
        pytest.skip("Windows junction behavior")
    module = implementation()
    root = tmp_path / "demo-root"
    (root / ".private").mkdir(parents=True)
    outside = tmp_path / "outside-sink"
    outside.mkdir()
    sentinel = outside / "untouched.json"
    sentinel.write_text('{"untouched":true}', encoding="utf-8")
    link = root / ".private/demo-mail"
    result = subprocess.run(  # noqa: S603 -- fixed local junction argv; both paths owned by temp fixture
        [shutil.which("cmd.exe"), "/c", "mklink", "/J", str(link), str(outside)],
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0
    monkeypatch.setattr(module, "ROOT", root)
    values = {
        "SUNRISE_LOCAL_TEST": "true",
        "SUNRISE_DATABASE_URL": "postgresql+psycopg://synthetic:synthetic@127.0.0.1:55439/sunrise_test_backend",
        "SUNRISE_SECRET_KEY": "synthetic-demo-key-" * 3,
    }
    try:
        assert link.is_junction()
        with pytest.raises(ValueError):
            module.settings_for_demo(values, "demo_instance_" + "f" * 32, 3102, 3202)
        assert sentinel.read_text(encoding="utf-8") == '{"untouched":true}'
        assert list(outside.iterdir()) == [sentinel]
    finally:
        link.rmdir()


@pytest.mark.parametrize(
    "options",
    [
        "-csearch_path=public",
        "-csearch_path=demo_instance_" + "a" * 32 + ",public",
        "-csearch_path=demo_instance_" + "a" * 32 + " -cstatement_timeout=0",
    ],
)
def test_demo_schema_query_never_allows_public_or_additional_postgres_options(options):
    from sqlalchemy.engine import make_url

    url = make_url("postgresql+psycopg://synthetic:synthetic@127.0.0.1:55439/sunrise_test_backend")
    with pytest.raises(ValueError):
        implementation()._scoped_database(
            url.set(query={"options": options}).render_as_string(hide_password=False)
        )


def test_direct_demo_loader_rejects_webhook_secret():
    with pytest.raises(ValueError):
        implementation().settings_for_demo(
            {
                "SUNRISE_LOCAL_TEST": "true",
                "SUNRISE_DATABASE_URL": "postgresql+psycopg://synthetic:synthetic@127.0.0.1:55439/sunrise_test_backend",
                "SUNRISE_SECRET_KEY": "synthetic-demo-key-" * 3,
                "SUNRISE_PANEL_WEBHOOK_SECRET": "synthetic-webhook-secret",
            },
            "demo_instance_" + "e" * 32,
            3102,
            3202,
        )


def test_demo_lifecycle_terminates_only_its_owned_child():
    import os
    import subprocess
    import sys

    cleanup = getattr(implementation(), "stop_children", None)
    assert cleanup is not None, "Missing owned demo process cleanup"
    child = subprocess.Popen(  # noqa: S603 -- fixed synthetic sleep child, no credentials/network or shell
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        assert child.poll() is None
        cleanup([child])
        assert child.poll() is not None
        assert os.getpid() > 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_demo_auto_selects_unused_distinct_ports_without_stopping_services(monkeypatch):
    module = implementation()
    chooser = getattr(module, "choose_demo_ports", None)
    assert chooser is not None, "Automatic unused-port selection is required"
    queried = []

    def available(port):
        queried.append(port)
        return port not in {8102, 3102, 3202, 8103}

    monkeypatch.setattr(module, "port_available", available)
    selected = chooser({"api": 8102, "landing": 3102, "dashboard": 3202})
    assert selected == {"api": 8104, "landing": 3103, "dashboard": 3203}
    assert len(set(selected.values())) == 3
    assert not set(selected.values()) & module.FORBIDDEN_PORTS
    assert queried


def test_demo_auto_ports_fail_closed_if_no_unused_ports(monkeypatch):
    module = implementation()
    chooser = getattr(module, "choose_demo_ports", None)
    assert chooser is not None
    monkeypatch.setattr(module, "port_available", lambda port: False)
    with pytest.raises(ValueError):
        chooser({"api": 8102, "landing": 3102, "dashboard": 3202})


@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows process tree lifecycle")
def test_demo_shutdown_reaps_owned_grandchild(tmp_path):
    import os
    import signal
    import socket
    import subprocess
    import sys
    import time

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    marker = tmp_path / "owned-child.json"
    grandchild = (
        "import socket,time;s=socket.socket();s.bind(('127.0.0.1',"
        + str(port)
        + "));s.listen();time.sleep(120)"
    )
    parent = (
        "import subprocess,sys,time,pathlib;child=subprocess.Popen([sys.executable,'-c',"
        + repr(grandchild)
        + "]);pathlib.Path("
        + repr(str(marker))
        + ").write_text(str(child.pid));time.sleep(120)"
    )
    process = subprocess.Popen(  # noqa: S603 -- fixed synthetic owned process tree
        [sys.executable, "-c", parent],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )  # noqa: S603 -- synthetic owned tree

    def reachable():
        with socket.socket() as client:
            client.settimeout(0.1)
            return client.connect_ex(("127.0.0.1", port)) == 0

    try:
        for _ in range(100):
            if marker.exists() and reachable():
                break
            time.sleep(0.02)
        assert marker.exists() and reachable()
        implementation().stop_children([process])
        for _ in range(100):
            if not reachable():
                break
            time.sleep(0.02)
        assert not reachable(), "Owned Vite-style grandchild survived shutdown"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if marker.exists() and reachable():
            os.kill(int(marker.read_text()), signal.SIGTERM)
