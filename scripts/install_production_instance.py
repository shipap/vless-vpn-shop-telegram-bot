"""Явная fresh-only подготовка собственного экземпляра с отдельной первой активацией.

Подготовка оставляет все возможности OFF; первая активация требует отдельного плана.
Не обновляет существующие БД, не вызывает провайдеров, рассылки или платежи.
План и последующие команды привязаны к точным файлам и PostgreSQL OID.
"""

import argparse
import hashlib
import io
import json
import os
import re
import sys
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from dotenv import dotenv_values
from dotenv.parser import parse_stream
from pydantic import AliasChoices, Field, StrictBool
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from sunrise_infrastructure.bootstrap_config import (
    bootstrap_instance,
    initial_legal_documents,
    load_bootstrap_seed,
)
from sunrise_infrastructure.db import Audit, Base, Setting
from sunrise_infrastructure.instance_config import load_instance_config
from sunrise_infrastructure.settings import (
    PRODUCTION_CAPABILITIES,
    ProductionAdmission,
    RuntimeGates,
    RuntimePolicy,
    Settings,
    StrictScope,
)

if __package__:
    from .render_closed_expand import render
    from .setup_local_docker import check_ignored, owned_path
else:
    from render_closed_expand import render
    from setup_local_docker import check_ignored, owned_path

ROOT = Path(__file__).resolve().parents[1]
PLAN = ".private/production/install-plan.json"
SCOPE = ".private/production/install-input.json"
MANIFEST = ".private/production/source-manifest.json"
ADMISSION = ".private/production/admission.json"
RUNTIME_ENV = ".private/production/runtime.env"
ACTIVATION_PLAN = ".private/production/first-activation-plan.json"
ACTIVE_ADMISSION = ".private/production/first-activation-admission.json"
ACTIVE_ENV = ".private/production/first-activation-runtime.env"


def digest(value):
    return hashlib.sha256(value).hexdigest()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def unique_json(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate_configuration_key")
        value[key] = item
    return value


def file_path(root, relative):
    name = Path(relative)
    if name.is_absolute() or ".." in name.parts or "\\" in str(relative):
        raise ValueError("installer_path_denied")
    root = owned_path(root)
    target = owned_path(root / name)
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError("installer_path_denied")
    return target


def read_file(root, relative, limit=2 * 1024 * 1024):
    path = file_path(root, relative)
    if not path.is_file() or path.stat().st_size > limit:
        raise ValueError("installer_file_denied")
    if relative.startswith(".private/"):
        check_secret_permissions(path)
    return path.read_bytes()


def check_secret_permissions(path):
    # Windows ACLs are not represented by POSIX mode bits. Production operators
    # must verify those separately; the Linux/Docker path enforces owner-only files.
    if os.name != "nt":
        observed = path.stat()
        if observed.st_uid != os.geteuid() or observed.st_mode & 0o077:
            raise ValueError("installer_owner_only_permissions_required")


def write_private(root, relative, body):
    if not relative.startswith(".private/production/"):
        raise ValueError("installer_output_denied")
    path = file_path(root, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    file_path(root, relative)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if read_file(root, relative) != body:
            raise ValueError("installer_output_exists") from None
        return
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())


def read_inputs(root, env_file, scope_file):
    if not re.fullmatch(r"\.env\.[a-z0-9_-]+", env_file) or scope_file != SCOPE:
        raise ValueError("installer_input_path_denied")
    check_ignored(root, env_file=env_file, include_private=True)
    raw = read_file(root, env_file, 65536)
    check_secret_permissions(file_path(root, env_file))
    source = raw.decode("utf-8")
    if "${" in source:
        raise ValueError("installer_ambient_interpolation_denied")
    seen = set()
    for binding in parse_stream(io.StringIO(source)):
        if binding.error or binding.key in seen:
            raise ValueError("installer_environment_invalid")
        if binding.key:
            seen.add(binding.key)
    env = dict(dotenv_values(stream=io.StringIO(source), interpolate=False))
    allowed = set()
    for name, field in Settings.model_fields.items():
        if isinstance(field.validation_alias, AliasChoices):
            allowed.update(field.validation_alias.choices)
            if len({env[key] for key in field.validation_alias.choices if key in env}) > 1:
                raise ValueError("installer_environment_alias_conflict")
        else:
            allowed.add("SUNRISE_" + name.upper())
    if set(env) - allowed or any(value is None or "\n" in value or "\r" in value for value in env.values()):
        raise ValueError("installer_environment_invalid")
    if (
        env.get("SUNRISE_RUNTIME_MODE") != "production"
        or env.get("SUNRISE_LOCAL_TEST", "false").lower() != "false"
        or env.get("SUNRISE_COOKIE_SECURE", "").lower() != "true"
        or env.get("SUNRISE_TURNSTILE_TEST_BYPASS", "false").lower() != "false"
        or any(value.lower() != "false" for key, value in env.items() if key.endswith("_TEST_AUTHORIZED"))
        or any(key.startswith("SUNRISE_PRODUCTION_ADMISSION_") for key in env)
    ):
        raise ValueError("installer_production_configuration_required")
    for name in ("SUNRISE_SECRET_KEY", "SUNRISE_BOT_TOKEN", "SUNRISE_ROLLYPAY_API_KEY",
                 "SUNRISE_ROLLYPAY_SIGNING_SECRET", "SUNRISE_PANEL_TOKEN", "SUNRISE_SMTP_PASSWORD"):
        if not env.get(name) or name == "SUNRISE_SECRET_KEY" and len(env[name]) < 32:
            raise ValueError("installer_credentials_missing")
    scope_raw = read_file(root, scope_file, 65536)
    scope = json.loads(scope_raw, object_pairs_hook=unique_json)
    if set(scope) != {"version", "scope_id", "database_schema", "redis_key_prefix", "panel"} or scope["version"] != 1:
        raise ValueError("installer_scope_invalid")
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", scope["database_schema"]):
        raise ValueError("installer_schema_invalid")
    instance_file = env.get("INSTANCE_CONFIG_PATH", env.get("SUNRISE_INSTANCE_CONFIG_PATH", ""))
    load_instance_config(instance_file, project_root=root)
    instance_raw = read_file(root, instance_file, 65536)
    seed_raw = read_file(root, "config/bootstrap.example.json", 65536)
    seed = load_bootstrap_seed(json.loads(seed_raw, object_pairs_hook=unique_json))
    records = source_records(root)
    sql, proof = render(root / "migrations/versions")
    return {
        "env": env, "env_raw": raw, "env_file": env_file, "scope_file": scope_file, "scope": scope,
        "seed": seed.model_dump(mode="json"), "sql": sql, "migration": proof, "manifest": records,
        "fingerprints": {"env": digest(raw), "scope": digest(scope_raw), "instance": digest(instance_raw),
                         "seed": digest(seed_raw), "manifest": digest(encoded(records)), "sql": proof["sql_sha256"]},
    }


def source_records(root):
    paths = {
        path.relative_to(root).as_posix()
        for base in ("apps", "packages") for path in (root / base).rglob("*.py")
        if "__pycache__" not in path.parts
    } | {
        "scripts/install_production_instance.py", "scripts/render_closed_expand.py", "scripts/setup_local_docker.py",
        "scripts/render_production_runtime.py",
        "pyproject.toml", "requirements.lock",
    } | {path.relative_to(root).as_posix() for path in (root / "migrations").rglob("*.py")}
    return [{"path": path, "sha256": digest(read_file(root, path))} for path in sorted(paths)]


def database_identity(connection):
    return dict(connection.execute(text("""SELECT current_database() AS name, current_user AS role,
      current_schema() AS schema, d.oid::bigint AS database_oid, n.oid::bigint AS schema_oid,
      pg_get_userbyid(n.nspowner) AS schema_owner, current_setting('search_path') AS search_path,
      pg_get_userbyid(d.datdba) AS database_owner,
      r.rolsuper AS superuser, r.rolcreatedb AS createdb, r.rolcreaterole AS createrole
      FROM pg_database d CROSS JOIN pg_namespace n CROSS JOIN pg_roles r
      WHERE d.datname=current_database() AND n.nspname=current_schema() AND r.rolname=current_user""")).mappings().one())


def validate_identity(identity, database_url, schema):
    url = make_url(database_url)
    if (
        url.drivername != "postgresql+psycopg" or not url.host or not url.port or url.query
        or identity["name"] != url.database or identity["role"] != url.username
        or identity["schema"] != schema or identity["search_path"] != schema
        or identity["schema_owner"] != url.username or identity["database_owner"] != url.username
        or any(identity[key] is not False for key in ("superuser", "createdb", "createrole"))
        or type(identity["database_oid"]) is not int or identity["database_oid"] < 1
        or type(identity["schema_oid"]) is not int or identity["schema_oid"] < 1
    ):
        raise ValueError("installer_database_ownership_denied")
    return identity


def user_relation_count(connection):
    return connection.execute(text("""SELECT
      (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
       WHERE substr(n.nspname, 1, 3) <> 'pg_' AND n.nspname <> 'information_schema') +
      (SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
       WHERE substr(n.nspname, 1, 3) <> 'pg_' AND n.nspname <> 'information_schema') +
      (SELECT count(*) FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace
       WHERE substr(n.nspname, 1, 3) <> 'pg_' AND n.nspname <> 'information_schema') +
      (SELECT count(*) FROM pg_event_trigger)""")).scalar_one()


def admission_document(inputs, identity):
    env, supplied = inputs["env"], inputs["scope"]
    db, redis = make_url(env["SUNRISE_DATABASE_URL"]), urlparse(env["SUNRISE_REDIS_URL"])
    if redis.scheme != "redis" or redis.query or redis.fragment or not re.fullmatch(r"/[0-9]+", redis.path):
        raise ValueError("installer_redis_invalid")
    database = {key: identity[key] for key in ("name", "role", "schema", "database_oid", "schema_oid", "schema_owner", "search_path")}
    database.update(host=db.host, port=db.port)
    admission = ProductionAdmission.model_validate({
        "version": 1, "scope_id": supplied["scope_id"], "database": database,
        "redis": {"host": redis.hostname, "port": redis.port, "database": int(redis.path[1:]),
                  "key_prefix": supplied["redis_key_prefix"]},
        "instance_sha256": inputs["fingerprints"]["instance"],
        "source_manifest_sha256": inputs["fingerprints"]["manifest"],
        "bot_id": env["SUNRISE_BOT_TOKEN"].split(":", 1)[0],
        "rollypay": {"origin": env["SUNRISE_ROLLYPAY_BASE_URL"].rstrip("/"),
                     "terminal": env["SUNRISE_ROLLYPAY_TERMINAL"],
                     "checkout_hosts": json.loads(env["SUNRISE_PAYMENT_ALLOWED_HOSTS"])},
        "panel": supplied["panel"] | {"origin": env["SUNRISE_PANEL_BASE_URL"], "contract": env["SUNRISE_PANEL_CONTRACT"]},
        "smtp": {"host": env["SUNRISE_SMTP_HOST"], "port": int(env["SUNRISE_SMTP_PORT"]),
                 "mode": env["SUNRISE_SMTP_MODE"], "sender": env["SUNRISE_SMTP_SENDER"]},
        "maximum_capabilities": dict.fromkeys(PRODUCTION_CAPABILITIES, False),
    })
    return admission.model_dump(mode="json", by_alias=True)


def plan(root=ROOT, env_file=".env.production", scope_file=SCOPE):
    root = owned_path(root)
    inputs = read_inputs(root, env_file, scope_file)
    engine = create_engine(inputs["env"]["SUNRISE_DATABASE_URL"], hide_parameters=True)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            identity = validate_identity(database_identity(connection), inputs["env"]["SUNRISE_DATABASE_URL"], inputs["scope"]["database_schema"])
            if user_relation_count(connection):
                raise ValueError("installer_existing_database_denied")
    finally:
        engine.dispose()
    document = {"version": 1, "env_file": env_file, "scope_file": scope_file, "identity": identity,
                "fingerprints": inputs["fingerprints"], "manifest": inputs["manifest"],
                "migration": inputs["migration"], "head": inputs["migration"]["revisions"][-1],
                "admission": admission_document(inputs, identity)}
    preflight(root, inputs, document)
    raw = encoded(document)
    write_private(root, PLAN, raw)
    return digest(raw)


def load_plan(root, expected):
    if not re.fullmatch(r"[a-f0-9]{64}", expected or ""):
        raise ValueError("installer_plan_pin_required")
    raw = read_file(root, PLAN)
    if digest(raw) != expected:
        raise ValueError("installer_plan_changed")
    return json.loads(raw, object_pairs_hook=unique_json), expected


def checked_inputs(root, document):
    inputs = read_inputs(root, document["env_file"], document["scope_file"])
    if (inputs["fingerprints"] != document["fingerprints"] or inputs["manifest"] != document["manifest"]
            or inputs["migration"] != document["migration"]
            or admission_document(inputs, document["identity"]) != document["admission"]):
        raise ValueError("installer_inputs_changed")
    return inputs


def guard_database(connection, document, *, empty=False):
    if database_identity(connection) != document["identity"]:
        raise ValueError("installer_database_changed")
    if empty and user_relation_count(connection):
        raise ValueError("installer_existing_database_denied")


def apply_migration_sql(connection, sql):
    if not sql.startswith("BEGIN;\n") or not sql.endswith("COMMIT;\n"):
        raise ValueError("installer_transaction_wrapper_invalid")
    body = sql[len("BEGIN;\n"):-len("COMMIT;\n")]
    if re.search(r"(?mi)^\s*(?:BEGIN|COMMIT|ROLLBACK)\s*;", body):
        raise ValueError("installer_nested_transaction_denied")
    # psycopg simple protocol supports the literal, reviewed multi-statement chain.
    # The outer SQLAlchemy transaction retains advisory lock, seed and rollback ownership.
    with connection.connection.driver_connection.cursor() as cursor:
        cursor.execute(body, prepare=False)


class BoundDatabase:
    def __init__(self, connection):
        self.connection, self.engine = connection, connection.engine

    @contextmanager
    def transaction(self):
        with Session(bind=self.connection, join_transaction_mode="create_savepoint") as session, session.begin():
            yield session


def receipt(document, pin):
    return {"version": 1, "plan_sha256": pin, "head": document["head"],
            "scope_id": document["admission"]["scope_id"]}


def seed_and_receipt(connection, inputs, document, pin):
    database = BoundDatabase(connection)
    bootstrap_instance(database, inputs["bootstrap_settings"], seed=inputs["seed"])
    with database.transaction() as session:
        session.add(Setting(key="production_install_receipt", value=receipt(document, pin)))
        session.add(Audit(action="production_fresh_install", target=document["admission"]["scope_id"],
                          reason="Явная инициализация проверенной пустой собственной БД; все возможности OFF.",
                          details=receipt(document, pin)))


def initialize(root, expected):
    document, pin = load_plan(root, expected)
    inputs = checked_inputs(root, document)
    inputs["bootstrap_settings"] = preflight(root, inputs, document)
    engine = create_engine(inputs["env"]["SUNRISE_DATABASE_URL"], hide_parameters=True)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
            connection.exec_driver_sql("SELECT pg_advisory_xact_lock(hashtext('vpn-shop:fresh-install'))")
            guard_database(connection, document, empty=True)
            apply_migration_sql(connection, inputs["sql"])
            seed_and_receipt(connection, inputs, document, pin)
    finally:
        engine.dispose()
    return pin


def check_receipt(connection, document, pin):
    head = connection.exec_driver_sql("SELECT version_num FROM v5_alembic_version").scalar_one()
    with BoundDatabase(connection).transaction() as session:
        row = session.get(Setting, "production_install_receipt")
        if head != document["head"] or row is None or row.value != receipt(document, pin):
            raise ValueError("installer_initialization_not_verified")


class FileOnlySettings(Settings):
    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings):
        return init_settings, dotenv_settings


def preflight(root, inputs, document):
    """Полная штатная Settings-валидация до DDL; временный допуск не имеет DB marker."""
    suffix = os.urandom(16).hex()
    admission_file = f".private/production/preflight-admission-{suffix}.json"
    env_file = f".private/production/preflight-runtime-{suffix}.env"
    raw = encoded(document["admission"])
    created = []
    try:
        write_private(root, admission_file, raw)
        created.append(admission_file)
        write_private(root, env_file, inputs["env_raw"])
        created.append(env_file)
        settings = FileOnlySettings(
            _env_file=file_path(root, env_file),
            production_admission_file=admission_file,
            production_admission_sha256=digest(raw),
        )
        initial_legal_documents(settings)
        return settings
    finally:
        # Только два временных файла этого вызова; каталоги и чужие данные сохраняются.
        for relative in created:
            file_path(root, relative).unlink()


def materialized_files(inputs, document):
    admission = encoded(document["admission"])
    body = inputs["env_raw"].rstrip(b"\r\n") + (
        "\nSUNRISE_PRODUCTION_ADMISSION_FILE=" + ADMISSION
        + "\nSUNRISE_PRODUCTION_ADMISSION_SHA256=" + digest(admission) + "\n"
    ).encode("utf-8")
    return {MANIFEST: encoded(document["manifest"]), ADMISSION: admission, RUNTIME_ENV: body}


def materialize(root, inputs, document):
    for relative, body in materialized_files(inputs, document).items():
        write_private(root, relative, body)
    return FileOnlySettings(_env_file=file_path(root, RUNTIME_ENV))


def admit(root, expected):
    document, pin = load_plan(root, expected)
    inputs = checked_inputs(root, document)
    engine = create_engine(inputs["env"]["SUNRISE_DATABASE_URL"], hide_parameters=True)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
            connection.exec_driver_sql("SELECT pg_advisory_xact_lock(hashtext('vpn-shop:fresh-install'))")
            guard_database(connection, document)
            check_receipt(connection, document, pin)
            settings = materialize(root, inputs, document)
            policy = RuntimePolicy.for_settings(settings)
            values = {
                "production_admission": policy._marker(),
                "production_runtime_gates": {"version": 1, "scope_id": policy.scope.scope_id, "generation": 1,
                                             "gates": dict.fromkeys(PRODUCTION_CAPABILITIES, False)},
            }
            with BoundDatabase(connection).transaction() as session:
                for key, value in values.items():
                    existing = session.get(Setting, key)
                    if existing and existing.value != value:
                        raise ValueError("installer_existing_admission_denied")
                    if existing is None:
                        session.add(Setting(key=key, value=value))
            policy.admit(BoundDatabase(connection))
    finally:
        engine.dispose()
    return pin


def verify(root, expected):
    document, pin = load_plan(root, expected)
    inputs = checked_inputs(root, document)
    for relative, body in materialized_files(inputs, document).items():
        if read_file(root, relative) != body:
            raise ValueError("installer_materialized_file_changed")
    settings = FileOnlySettings(_env_file=file_path(root, RUNTIME_ENV))
    policy = RuntimePolicy.for_settings(settings)
    if any(policy.scope.maximum_capabilities.values()):
        raise ValueError("installer_capabilities_must_remain_off")
    engine = create_engine(settings.database_url, hide_parameters=True)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            guard_database(connection, document)
            check_receipt(connection, document, pin)
            database = BoundDatabase(connection)
            policy.admit(database)
            with database.transaction() as session:
                if any(policy._check_current(session).values()):
                    raise ValueError("installer_gates_must_remain_off")
    finally:
        engine.dispose()
    return pin


def requested_capabilities(values):
    if (
        not isinstance(values, list) or not values or len(set(values)) != len(values)
        or not set(values) <= PRODUCTION_CAPABILITIES - {"broadcast_delivery", "push_delivery", "retention"}
    ):
        raise ValueError("installer_first_activation_capabilities_denied")
    if "invoice_create" in values and not {"payment_reconcile", "financial_apply", "panel_mutation"} <= set(values):
        raise ValueError("installer_complete_purchase_pipeline_required")
    return sorted(values)


class ReviewedArtifact(StrictScope):
    check_id: str
    kind: Literal["owner_reviewed_external_artifact"]
    path: str = Field(pattern=r"^\.private/production/evidence/[A-Za-z0-9_./-]+$")
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    summary: str = Field(min_length=20, max_length=1000)


class ActivationEvidence(StrictScope):
    version: int = Field(ge=1, le=1)
    scope_id: str
    install_plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    admission_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    capabilities: list[str]
    reviewed_at: str
    owner_review_confirmed: StrictBool
    checks: list[ReviewedArtifact] = Field(min_length=1, max_length=20)


def required_checks(capabilities):
    checks = {"tls_ingress"}
    selected = set(capabilities)
    if selected & {"auth_mail", "notice_email"}:
        checks.add("smtp_delivery")
    if selected & {"invoice_create", "payment_reconcile", "financial_apply"}:
        checks |= {"payment_merchant", "payment_callback"}
    if "panel_mutation" in selected:
        checks.add("panel_ownership")
    if "notice_telegram" in selected:
        checks.add("telegram_identity")
    if selected & {"existing_recurring_status", "existing_recurring_cancel"}:
        checks.add("existing_recurring_only")
    return checks


def read_activation_evidence(root, relative, document, pin, capabilities):
    if not relative.startswith(".private/production/"):
        raise ValueError("installer_evidence_path_denied")
    raw = read_file(root, relative, 65536)
    value = ActivationEvidence.model_validate(json.loads(raw, object_pairs_hook=unique_json))
    reviewed = datetime.fromisoformat(value.reviewed_at)
    if (
        value.owner_review_confirmed is not True
        or value.scope_id != document["admission"]["scope_id"] or value.install_plan_sha256 != pin
        or value.admission_sha256 != digest(encoded(document["admission"]))
        or requested_capabilities(value.capabilities) != requested_capabilities(capabilities)
        or reviewed.tzinfo is None or reviewed.utcoffset() != timedelta(0)
        or reviewed > datetime.now(UTC) + timedelta(minutes=5)
        or len({item.check_id for item in value.checks}) != len(value.checks)
        or {item.check_id for item in value.checks} != required_checks(capabilities)
    ):
        raise ValueError("installer_evidence_scope_denied")
    for item in value.checks:
        artifact = read_file(root, item.path)
        if not artifact or digest(artifact) != item.sha256:
            raise ValueError("installer_evidence_changed")
    # These are owner-supplied attestations. No provider operation or external
    # verification is performed or claimed by this installer.
    return {"path": relative, "sha256": digest(raw), "attestation": value.model_dump(mode="json")}


def require_no_activity(connection):
    for table in Base.metadata.sorted_tables:
        if table.name not in {"v5_settings", "v5_products", "v5_audit"}:
            if connection.execute(select(func.count()).select_from(table)).scalar_one():
                raise ValueError("installer_first_activation_activity_denied")


def lock_first_activation_tables(connection):
    names = sorted(Base.metadata.tables)
    if any(not re.fullmatch(r"[a-z][a-z0-9_]*", name) for name in names):
        raise ValueError("installer_model_table_invalid")
    # A normal INSERT/UPDATE/DELETE takes ROW EXCLUSIVE. These table locks block
    # those writers through the outer commit; lock_timeout aborts conflicting work.
    quoted = ", ".join('"' + name + '"' for name in names)
    connection.exec_driver_sql("LOCK TABLE " + quoted + " IN SHARE ROW EXCLUSIVE MODE")


def validate_first_activation_rows(marker, gates, recurring, existing, expected_marker, scope_id):
    current = RuntimeGates.model_validate(gates)
    if (
        marker != expected_marker or current.scope_id != scope_id or current.generation != 1
        or any(current.gates.values()) or existing is not None
        or not isinstance(recurring, dict) or recurring.get("enabled") is not False
    ):
        raise ValueError("installer_first_activation_stage_denied")


def require_first_stage(connection, document, pin, settings):
    from sunrise_infrastructure.legal import validate_documents

    guard_database(connection, document)
    check_receipt(connection, document, pin)
    require_no_activity(connection)
    database = BoundDatabase(connection)
    policy = RuntimePolicy.for_settings(settings).admit(database)
    with database.transaction() as session:
        rows = {key: session.get(Setting, key) for key in (
            "production_admission", "production_runtime_gates", "recurring", "production_first_activation", "legal"
        )}
        validate_first_activation_rows(
            rows["production_admission"].value if rows["production_admission"] else None,
            rows["production_runtime_gates"].value if rows["production_runtime_gates"] else {},
            rows["recurring"].value if rows["recurring"] else None,
            rows["production_first_activation"], policy._marker(), policy.scope.scope_id,
        )
        validate_documents(rows["legal"].value.get("documents") if rows["legal"] else None)


def activation_files(inputs, document, activation):
    capabilities = requested_capabilities(activation["capabilities"])
    admission = dict(document["admission"], maximum_capabilities={
        key: key in capabilities for key in PRODUCTION_CAPABILITIES
    })
    raw = encoded(admission)
    body = inputs["env_raw"].rstrip(b"\r\n") + (
        "\nSUNRISE_PRODUCTION_ADMISSION_FILE=" + ACTIVE_ADMISSION
        + "\nSUNRISE_PRODUCTION_ADMISSION_SHA256=" + digest(raw) + "\n"
    ).encode("utf-8")
    return {ACTIVE_ADMISSION: raw, ACTIVE_ENV: body}


def activation_plan(root, expected, scope_id, capabilities, evidence_file):
    document, pin = load_plan(root, expected)
    capabilities = requested_capabilities(capabilities)
    if scope_id != document["admission"]["scope_id"]:
        raise ValueError("installer_scope_confirmation_required")
    inputs = checked_inputs(root, document)
    evidence = read_activation_evidence(root, evidence_file, document, pin, capabilities)
    verify(root, pin)
    settings = FileOnlySettings(_env_file=file_path(root, RUNTIME_ENV))
    engine = create_engine(settings.database_url, hide_parameters=True)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            require_first_stage(connection, document, pin, settings)
    finally:
        engine.dispose()
    activation = {"version": 1, "install_plan_sha256": pin, "scope_id": scope_id,
                  "capabilities": capabilities, "evidence": evidence,
                  "provider_verification": "not_performed_by_installer"}
    activation["artifacts"] = {
        path: digest(body) for path, body in activation_files(inputs, document, activation).items()
    }
    raw = encoded(activation)
    write_private(root, ACTIVATION_PLAN, raw)
    return digest(raw)


def load_activation(root, expected, activation_pin):
    document, pin = load_plan(root, expected)
    inputs = checked_inputs(root, document)
    if not re.fullmatch(r"[a-f0-9]{64}", activation_pin or ""):
        raise ValueError("installer_activation_pin_required")
    raw = read_file(root, ACTIVATION_PLAN)
    if digest(raw) != activation_pin:
        raise ValueError("installer_activation_plan_changed")
    activation = json.loads(raw, object_pairs_hook=unique_json)
    if activation["install_plan_sha256"] != pin or activation["scope_id"] != document["admission"]["scope_id"]:
        raise ValueError("installer_activation_scope_denied")
    evidence = read_activation_evidence(root, activation["evidence"]["path"], document, pin, activation["capabilities"])
    artifacts = activation_files(inputs, document, activation)
    if evidence != activation["evidence"] or {path: digest(body) for path, body in artifacts.items()} != activation["artifacts"]:
        raise ValueError("installer_activation_inputs_changed")
    for relative, body in materialized_files(inputs, document).items():
        if read_file(root, relative) != body:
            raise ValueError("installer_staged_files_changed")
    return document, pin, activation, artifacts


def activation_receipt(pin, activation_pin, activation):
    return {"version": 1, "install_plan_sha256": pin, "activation_plan_sha256": activation_pin,
            "scope_id": activation["scope_id"], "artifacts": activation["artifacts"],
            "provider_verification": "not_performed_by_installer"}


def active_rows(policy, activation):
    return {
        "production_admission": policy._marker(),
        "production_runtime_gates": {"version": 1, "scope_id": activation["scope_id"], "generation": 2,
                                     "gates": {key: key in activation["capabilities"] for key in PRODUCTION_CAPABILITIES}},
    }


def check_active_rows(session, policy, activation, expected_receipt):
    row = session.get(Setting, "production_first_activation", populate_existing=True)
    if row is None or row.value != expected_receipt or type(row.value.get("version")) is not int:
        raise ValueError("installer_activation_receipt_changed")
    recurring = session.get(Setting, "recurring", populate_existing=True)
    if recurring is None or not isinstance(recurring.value, dict) or recurring.value.get("enabled") is not False:
        raise ValueError("installer_recurring_enrollment_must_remain_off")
    for key, expected in active_rows(policy, activation).items():
        current = session.get(Setting, key, populate_existing=True)
        if current is None or current.value != expected:
            raise ValueError("installer_activation_stage_changed")


def activate(root, expected, activation_pin, scope_id, confirmed):
    document, pin, activation, artifacts = load_activation(root, expected, activation_pin)
    if confirmed is not True or scope_id != activation["scope_id"]:
        raise ValueError("installer_explicit_first_activation_required")
    # Immutable files precede the DB transaction. Interruption leaves closed
    # guards; exact retry is allowed, but existing files are never overwritten.
    for path, body in artifacts.items():
        write_private(root, path, body)
    settings = FileOnlySettings(_env_file=file_path(root, ACTIVE_ENV))
    policy = RuntimePolicy.for_settings(settings)
    expected_receipt = activation_receipt(pin, activation_pin, activation)
    engine = create_engine(settings.database_url, hide_parameters=True)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
            connection.exec_driver_sql("SELECT pg_advisory_xact_lock(hashtext('vpn-shop:fresh-install'))")
            guard_database(connection, document)
            check_receipt(connection, document, pin)
            lock_first_activation_tables(connection)
            require_no_activity(connection)
            database = BoundDatabase(connection)
            with database.transaction() as session:
                existing = session.get(Setting, "production_first_activation")
                if existing is not None:
                    check_active_rows(session, policy, activation, expected_receipt)
                else:
                    staged = FileOnlySettings(_env_file=file_path(root, RUNTIME_ENV))
                    require_first_stage(connection, document, pin, staged)
                    for key, value in active_rows(policy, activation).items():
                        session.get(Setting, key).value = value
                    session.add(Setting(key="production_first_activation", value=expected_receipt))
                    session.add(Audit(action="production_first_activation", target=activation["scope_id"],
                                      reason="Владелец явно запросил первую активацию указанных возможностей.",
                                      details=expected_receipt | {"capabilities": activation["capabilities"]}))
            policy.admit(database)
    finally:
        engine.dispose()
    return activation_pin


def verify_activation(root, expected, activation_pin):
    document, pin, activation, artifacts = load_activation(root, expected, activation_pin)
    for path, body in artifacts.items():
        if read_file(root, path) != body:
            raise ValueError("installer_active_files_changed")
    settings = FileOnlySettings(_env_file=file_path(root, ACTIVE_ENV))
    policy = RuntimePolicy.for_settings(settings)
    engine = create_engine(settings.database_url, hide_parameters=True)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            guard_database(connection, document)
            check_receipt(connection, document, pin)
            database = BoundDatabase(connection)
            policy.admit(database)
            with database.transaction() as session:
                check_active_rows(session, policy, activation, activation_receipt(pin, activation_pin, activation))
                recurring = session.get(Setting, "recurring", populate_existing=True)
                if recurring is None or recurring.value.get("enabled") is not False:
                    raise ValueError("installer_recurring_enrollment_must_remain_off")
    finally:
        engine.dispose()
    return activation_pin


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("installer_arguments_invalid")


def main(argv=None):
    parser = SafeParser(description="Новая пустая собственная PostgreSQL: подготовка OFF и отдельная первая активация")
    commands = parser.add_subparsers(dest="command", required=True)
    planner = commands.add_parser("plan", help="Проверить пустую собственную БД и закрепить план")
    planner.add_argument("--env-file", default=".env.production")
    planner.add_argument("--scope-file", default=SCOPE)
    for command in ("init", "admit", "verify"):
        commands.add_parser(command).add_argument("--expected-plan-sha256", required=True)
    first_plan = commands.add_parser("activation-plan", help="План только первой явной активации")
    first_plan.add_argument("--expected-plan-sha256", required=True)
    first_plan.add_argument("--scope-id", required=True)
    first_plan.add_argument("--capability", action="append", required=True)
    first_plan.add_argument("--evidence-file", required=True)
    first_apply = commands.add_parser("activate", help="Применить точный план первой активации")
    first_verify = commands.add_parser("verify-activation", help="Проверить применённую первую активацию без записи")
    for command in (first_apply, first_verify):
        command.add_argument("--expected-plan-sha256", required=True)
        command.add_argument("--expected-activation-sha256", required=True)
    first_apply.add_argument("--scope-id", required=True)
    first_apply.add_argument("--confirm-first-activation", action="store_true", required=True)
    try:
        args = parser.parse_args(argv)
        if args.command == "plan":
            pin = plan(ROOT, args.env_file, args.scope_file)
        elif args.command == "activation-plan":
            pin = activation_plan(ROOT, args.expected_plan_sha256, args.scope_id, args.capability, args.evidence_file)
        elif args.command == "activate":
            pin = activate(ROOT, args.expected_plan_sha256, args.expected_activation_sha256,
                           args.scope_id, args.confirm_first_activation)
        elif args.command == "verify-activation":
            pin = verify_activation(ROOT, args.expected_plan_sha256, args.expected_activation_sha256)
        else:
            pin = {"init": initialize, "admit": admit, "verify": verify}[args.command](ROOT, args.expected_plan_sha256)
        output = {"command": args.command, "plan_sha256": pin, "capabilities": "all_off"}
        if args.command in {"activation-plan", "activate", "verify-activation"}:
            output = {"command": args.command, "activation_plan_sha256": pin,
                      "provider_verification": "not_performed_by_installer"}
        print(json.dumps(output))
        return 0
    except Exception:  # noqa: BLE001 -- CLI boundary must not print credentials, DSNs or raw SQL errors
        print("Операция остановлена защитной проверкой; проверьте конфигурацию, план и область пустой БД.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
