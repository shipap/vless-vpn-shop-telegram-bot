"""Fresh-only установщик сохраняет точный допуск и отказывает до записи при смене области."""

import hashlib
import importlib.util
import json
import shutil
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

ROOT = Path(__file__).resolve().parents[2]


def installer():
    path = ROOT / "scripts/install_production_instance.py"
    assert path.is_file(), "Нет переносимого fresh-only установщика production"
    spec = importlib.util.spec_from_file_location("scripts.install_production_instance", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def identity():
    return {
        "name": "shop_fresh", "role": "shop_app", "schema": "shop", "database_oid": 12000,
        "schema_oid": 22000, "schema_owner": "shop_app", "search_path": "shop",
        "database_owner": "shop_app", "superuser": False, "createdb": False, "createrole": False,
    }


@pytest.fixture
def installation(tmp_path, monkeypatch):
    from sunrise_infrastructure import instance_config

    monkeypatch.setattr(instance_config, "PROJECT_ROOT", tmp_path)
    for name in (".gitignore", ".dockerignore", "pyproject.toml", "requirements.lock"):
        (tmp_path / name).write_bytes((ROOT / name).read_bytes())
    for directory in ("apps/synthetic", "packages/synthetic", "scripts", "config", ".private/production"):
        (tmp_path / directory).mkdir(parents=True, exist_ok=True)
    (tmp_path / "apps/synthetic/main.py").write_text("# Synthetic fixture.\n", encoding="utf-8")
    for name in ("install_production_instance.py", "render_closed_expand.py", "setup_local_docker.py", "render_production_runtime.py"):
        (tmp_path / "scripts" / name).write_bytes((ROOT / "scripts" / name).read_bytes())
    shutil.copytree(ROOT / "migrations", tmp_path / "migrations", ignore=shutil.ignore_patterns("__pycache__"))
    (tmp_path / "config/bootstrap.example.json").write_bytes((ROOT / "config/bootstrap.example.json").read_bytes())
    (tmp_path / "config/instance.json").write_text(json.dumps({
        "app_name": "Synthetic Shop", "public_site_url": "https://shop.example.test",
        "dashboard_url": "https://cabinet.example.test",
    }), encoding="utf-8")
    scope = {
        "version": 1, "scope_id": "synthetic-shop", "database_schema": "shop",
        "redis_key_prefix": "synthetic:production:",
        "panel": {"version": "3.4.5", "internal_squads": ["11111111-1111-4111-8111-111111111111"],
                  "external_squads": [], "subscription_hosts": ["sub.example.test"], "node_uuids": []},
    }
    (tmp_path / ".private/production/install-input.json").write_text(json.dumps(scope), encoding="utf-8")
    (tmp_path / ".private/production/install-input.json").chmod(0o600)
    values = {
        "SUNRISE_DATABASE_URL": "postgresql+psycopg://shop_app:synthetic@postgres:5432/shop_fresh",
        "SUNRISE_REDIS_URL": "redis://:synthetic@redis:6379/0", "INSTANCE_CONFIG_PATH": "config/instance.json",
        "SUNRISE_SECRET_KEY": "synthetic-install-secret-key-123456789", "SUNRISE_RUNTIME_MODE": "production",
        "SUNRISE_COOKIE_SECURE": "true", "SUNRISE_ALLOWED_HOSTS": '["shop.example.test","cabinet.example.test"]',
        "SUNRISE_WEBHOOK_BASE_URL": "https://cabinet.example.test",
        "SUNRISE_BOT_TOKEN": "100001:synthetic-token", "SUNRISE_ROLLYPAY_API_KEY": "synthetic-api",
        "SUNRISE_ROLLYPAY_SIGNING_SECRET": "synthetic-signing", "SUNRISE_ROLLYPAY_TERMINAL": "synthetic-terminal",
        "SUNRISE_ROLLYPAY_BASE_URL": "https://bank.example.test", "SUNRISE_PAYMENT_ALLOWED_HOSTS": '["pay.example.test"]',
        "SUNRISE_PANEL_BASE_URL": "https://panel.example.test", "SUNRISE_PANEL_CONTRACT": "3.4_id",
        "SUNRISE_PANEL_ALLOWED_HOSTS": '["panel.example.test"]', "SUNRISE_PANEL_TOKEN": "synthetic-panel",
        "SUNRISE_SMTP_HOST": "smtp.example.test", "SUNRISE_SMTP_PORT": "587",
        "SUNRISE_SMTP_MODE": "starttls", "SUNRISE_SMTP_SENDER": "shop@example.test",
        "SUNRISE_SMTP_USERNAME": "shop@example.test", "SUNRISE_SMTP_PASSWORD": "synthetic-password",
    }

    def write_env(overrides=None):
        contents = values | (overrides or {})
        (tmp_path / ".env.production").write_text(
            "".join(f"{key}='{value}'\n" for key, value in contents.items()), encoding="utf-8"
        )
        (tmp_path / ".env.production").chmod(0o600)

    write_env()
    return tmp_path, write_env


def test_full_preflight_uses_current_settings_and_cleans_its_temporary_files(installation):
    root, _ = installation
    module = installer()
    inputs = module.read_inputs(root, ".env.production", module.SCOPE)
    admission = module.admission_document(inputs, identity())
    module.preflight(root, inputs, {"admission": admission})
    assert all(value is False for value in admission["maximum_capabilities"].values())
    assert sorted(path.name for path in (root / ".private/production").iterdir()) == ["install-input.json"]


def test_short_secret_is_rejected_before_plan_database_access(installation, monkeypatch):
    root, write_env = installation
    write_env({"SUNRISE_SECRET_KEY": "x"})
    module = installer()
    monkeypatch.setattr(module, "create_engine", lambda *_a, **_k: pytest.fail("БД до проверки секрета"))
    with pytest.raises(ValueError):
        module.plan(root)
    assert not (root / module.PLAN).exists()


@pytest.mark.parametrize("override", [
    {"SUNRISE_ALLOWED_HOSTS": '["*"]'}, {"SUNRISE_PANEL_ALLOWED_HOSTS": '["foreign.example.test"]'},
    {"SUNRISE_ALLOWED_ORIGINS": "invalid-json"}, {"APP_NAME": "x" * 121},
])
def test_full_runtime_preflight_rejects_invalid_overrides_without_ddl(installation, override):
    root, write_env = installation
    write_env(override)
    module = installer()
    inputs = module.read_inputs(root, ".env.production", module.SCOPE)
    admission = module.admission_document(inputs, identity())
    with pytest.raises(ValueError):
        module.preflight(root, inputs, {"admission": admission})
    assert not (root / module.PLAN).exists()
    assert not (root / module.ADMISSION).exists()


@pytest.mark.parametrize("drift", ["SUNRISE_SECRET_KEY", "SUNRISE_ADMIN_TELEGRAM_ALLOWLIST"])
def test_verify_denies_drift_in_materialized_environment_before_database_access(installation, monkeypatch, drift):
    root, _ = installation
    module = installer()
    inputs = module.read_inputs(root, ".env.production", module.SCOPE)
    document = {"admission": module.admission_document(inputs, identity()), "manifest": inputs["manifest"]}
    module.materialize(root, inputs, document)
    path = root / module.RUNTIME_ENV
    original = path.read_text(encoding="utf-8")
    if drift == "SUNRISE_SECRET_KEY":
        original = original.replace("synthetic-install-secret-key-123456789", "changed-install-secret-key-12345678900")
    else:
        original += "SUNRISE_ADMIN_TELEGRAM_ALLOWLIST='[\"999999\"]'\n"
    path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(module, "load_plan", lambda *_a: (document, "a" * 64))
    monkeypatch.setattr(module, "checked_inputs", lambda *_a: inputs)
    monkeypatch.setattr(module, "create_engine", lambda *_a, **_k: pytest.fail("БД после изменения runtime.env"))
    with pytest.raises(ValueError):
        module.verify(root, "a" * 64)


@pytest.mark.parametrize("changed", [
    {"schema_owner": "foreign"}, {"database_owner": "foreign"}, {"search_path": '"$user", public'},
    {"superuser": True}, {"createdb": True}, {"createrole": True}, {"schema": "foreign"},
])
def test_target_identity_rejects_foreign_ownership_and_broad_roles(changed):
    with pytest.raises(ValueError):
        installer().validate_identity(
            identity() | changed,
            "postgresql+psycopg://shop_app:synthetic@postgres:5432/shop_fresh",
            "shop",
        )


def test_exact_owned_database_identity_is_accepted_without_fabricating_oids():
    proof = installer().validate_identity(
        identity(), "postgresql+psycopg://shop_app:synthetic@postgres:5432/shop_fresh", "shop"
    )
    assert proof == identity()


@pytest.mark.parametrize("schema,want", [("shop", 1), ("pgshop", 1), ("pgx", 1), ("pg_catalog", 0)])
def test_fresh_database_guard_counts_user_schemas_starting_with_pg(schema, want):
    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE pg_namespace (oid INT, nspname TEXT)")
            connection.exec_driver_sql("CREATE TABLE pg_class (relnamespace INT)")
            connection.exec_driver_sql("CREATE TABLE pg_proc (pronamespace INT)")
            connection.exec_driver_sql("CREATE TABLE pg_type (typnamespace INT)")
            connection.exec_driver_sql("CREATE TABLE pg_event_trigger (id INT)")
            connection.exec_driver_sql("INSERT INTO pg_namespace VALUES (1, ?)", (schema,))
            connection.exec_driver_sql("INSERT INTO pg_class VALUES (1)")
            assert installer().user_relation_count(connection) == want
    finally:
        engine.dispose()


def test_plan_pin_is_checked_before_any_database_access(tmp_path, monkeypatch):
    module = installer()
    destination = tmp_path / ".private/production/install-plan.json"
    destination.parent.mkdir(parents=True)
    destination.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(module, "create_engine", lambda *_a, **_k: pytest.fail("БД не должна открываться"))
    with pytest.raises(ValueError):
        module.initialize(tmp_path, "0" * 64)


def test_protected_file_write_never_clobbers_owner_values(tmp_path):
    module = installer()
    root = tmp_path / ".private/production"
    root.mkdir(parents=True)
    target = root / "runtime.env"
    target.write_bytes(b"OWNER_SECRET=preserve\n")
    target.chmod(0o600)
    with pytest.raises(ValueError):
        module.write_private(tmp_path, ".private/production/runtime.env", b"OWNER_SECRET=replace\n")
    assert target.read_bytes() == b"OWNER_SECRET=preserve\n"
    module.write_private(tmp_path, ".private/production/runtime.env", b"OWNER_SECRET=preserve\n")


def test_sql_application_keeps_transaction_control_with_the_guarded_caller():
    module = installer()
    commands = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, statement, *, prepare):
            commands.append((statement, prepare))

    connection = SimpleNamespace(connection=SimpleNamespace(driver_connection=SimpleNamespace(cursor=Cursor)))
    module.apply_migration_sql(connection, "BEGIN;\nCREATE TABLE synthetic (id INT);\nCOMMIT;\n")
    assert commands == [("CREATE TABLE synthetic (id INT);\n", False)]
    for invalid in ("COMMIT;\nDROP TABLE synthetic;", "BEGIN;\nCOMMIT;\nCOMMIT;\n"):
        with pytest.raises(ValueError):
            module.apply_migration_sql(connection, invalid)
    assert len(commands) == 1


@pytest.mark.parametrize("changed,relations", [({"database_oid": 12001}, 0), ({}, 1)])
def test_changed_database_or_existing_relations_stop_migration_and_seed(tmp_path, monkeypatch, changed, relations):
    module = installer()
    document = {
        "identity": identity(), "database_url_sha256": "unused", "head": "v11_0019",
        "admission": {"database": {"schema": "shop"}},
    }
    plan = json.dumps(document).encode()
    state = {"committed": False, "rolled_back": False}

    @contextmanager
    def transaction():
        try:
            yield SimpleNamespace(exec_driver_sql=lambda *_a, **_k: None)
        except Exception:
            state["rolled_back"] = True
            raise
        else:
            state["committed"] = True

    monkeypatch.setattr(module, "load_plan", lambda *_a: (document, hashlib.sha256(plan).hexdigest()))
    monkeypatch.setattr(module, "checked_inputs", lambda *_a: {"env": {"SUNRISE_DATABASE_URL": "synthetic"}})
    monkeypatch.setattr(module, "preflight", lambda *_a: None)
    monkeypatch.setattr(module, "create_engine", lambda *_a, **_k: SimpleNamespace(begin=transaction, dispose=lambda: None))
    monkeypatch.setattr(module, "database_identity", lambda _c: identity() | changed)
    monkeypatch.setattr(module, "user_relation_count", lambda _c: relations)
    monkeypatch.setattr(module, "apply_migration_sql", lambda *_a: pytest.fail("DDL после проваленной защиты"))
    monkeypatch.setattr(module, "seed_and_receipt", lambda *_a: pytest.fail("Seed после проваленной защиты"))
    with pytest.raises(ValueError):
        module.initialize(tmp_path, hashlib.sha256(plan).hexdigest())
    assert state == {"committed": False, "rolled_back": True}


@pytest.mark.parametrize("seed_failure", [False, True])
def test_actual_migration_chain_and_seed_share_the_guarded_outer_transaction(installation, monkeypatch, seed_failure):
    root, _ = installation
    module = installer()
    state = {"committed": False, "rolled_back": False, "sql": [], "seed": 0}

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, statement, *, prepare):
            assert prepare is False
            state["sql"].append(statement)

    connection = SimpleNamespace(
        exec_driver_sql=lambda *_a, **_k: None,
        connection=SimpleNamespace(driver_connection=SimpleNamespace(cursor=Cursor)),
    )

    @contextmanager
    def transaction():
        try:
            yield connection
        except Exception:
            state["rolled_back"] = True
            raise
        else:
            state["committed"] = True

    def seed(current, inputs, document, pin):
        assert current is connection and document["head"] == "v11_0019"
        assert inputs["seed"]["settings"]["recurring"]["enabled"] is False
        assert len(pin) == 64
        state["seed"] += 1
        if seed_failure:
            raise RuntimeError("Synthetic seed failure")

    monkeypatch.setattr(module, "create_engine", lambda *_a, **_k: SimpleNamespace(begin=transaction, dispose=lambda: None))
    monkeypatch.setattr(module, "database_identity", lambda _c: identity())
    monkeypatch.setattr(module, "user_relation_count", lambda _c: 0)
    pin = module.plan(root)
    state["committed"] = False
    monkeypatch.setattr(module, "seed_and_receipt", seed)
    if seed_failure:
        with pytest.raises(RuntimeError, match="Synthetic seed failure"):
            module.initialize(root, pin)
    else:
        assert module.initialize(root, pin) == pin
    assert state["committed"] is not seed_failure
    assert state["rolled_back"] is seed_failure
    assert state["seed"] == 1 and len(state["sql"]) == 1
    assert "CREATE TABLE v5_accounts" in state["sql"][0]
    assert "'v11_0019'" in state["sql"][0]
    assert not state["sql"][0].startswith("BEGIN;")
    assert not state["sql"][0].rstrip().endswith("COMMIT;")


def test_source_drift_after_plan_stops_before_database_access(installation, monkeypatch):
    root, _ = installation
    module = installer()

    @contextmanager
    def transaction():
        yield SimpleNamespace(exec_driver_sql=lambda *_a, **_k: None)

    monkeypatch.setattr(module, "create_engine", lambda *_a, **_k: SimpleNamespace(begin=transaction, dispose=lambda: None))
    monkeypatch.setattr(module, "database_identity", lambda _c: identity())
    monkeypatch.setattr(module, "user_relation_count", lambda _c: 0)
    pin = module.plan(root)
    (root / "apps/synthetic/main.py").write_text("# Owner changed source.\n", encoding="utf-8")
    monkeypatch.setattr(module, "create_engine", lambda *_a, **_k: pytest.fail("БД после смены исходников"))
    with pytest.raises(ValueError):
        module.initialize(root, pin)


def test_invalid_cli_arguments_never_echo_secret_values(capsys):
    assert installer().main(["init", "--unknown-secret", "sensitive-synthetic-value"]) == 1
    output = capsys.readouterr()
    assert "sensitive-synthetic-value" not in output.out + output.err


@pytest.mark.parametrize("owner,mode", [(1002, 0o600), (1001, 0o644), (1001, 0o640)])
def test_owner_only_secret_permissions_reject_other_owner_or_readable_mode(monkeypatch, owner, mode):
    module = installer()
    monkeypatch.setattr(module, "os", SimpleNamespace(name="posix", geteuid=lambda: 1001))
    with pytest.raises(ValueError, match="owner_only_permissions"):
        module.check_secret_permissions(SimpleNamespace(stat=lambda: SimpleNamespace(st_uid=owner, st_mode=mode)))
    module.check_secret_permissions(SimpleNamespace(stat=lambda: SimpleNamespace(st_uid=1001, st_mode=0o600)))


def test_original_environment_permissions_are_checked_before_database_connection(installation, monkeypatch):
    root, _ = installation
    module = installer()
    seen = []

    def require_permissions(path):
        seen.append(path.name)
        if path.name == ".env.production":
            raise ValueError("synthetic_owner_only_denial")

    monkeypatch.setattr(module, "check_secret_permissions", require_permissions)
    monkeypatch.setattr(module, "create_engine", lambda *_a, **_k: pytest.fail("DB before permissions"))
    with pytest.raises(ValueError, match="synthetic_owner_only_denial"):
        module.plan(root)
    assert seen == [".env.production"]
