"""Только первая явная активация, без провайдеров и автоматического включения возможностей."""

import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import event, func, select
from sunrise_infrastructure.db import Account, Audit, Setting
from test_explicit_bootstrap_legal import database  # noqa: F401 -- shared synthetic fixture
from test_production_installer import identity, installation, installer  # noqa: F401 -- synthetic fixture


@pytest.mark.parametrize("requested", [[], ["unknown"], ["auth_mail", "auth_mail"],
                                       ["broadcast_delivery"], ["push_delivery"], ["retention"],
                                       ["invoice_create"], ["invoice_create", "financial_apply", "panel_mutation"]])
def test_capabilities_require_explicit_nonduplicate_supported_first_activation_list(requested):
    module = installer()
    assert hasattr(module, "requested_capabilities"), "Нет ограниченной первой активации"
    with pytest.raises(ValueError):
        module.requested_capabilities(requested)


def test_complete_purchase_pipeline_requires_each_dependency_explicitly():
    requested = ["invoice_create", "payment_reconcile", "financial_apply", "panel_mutation"]
    assert set(installer().requested_capabilities(requested)) == set(requested)
    assert installer().requested_capabilities(["auth_mail"]) == ["auth_mail"]


def test_real_table_lock_statement_covers_all_model_tables_in_deterministic_write_blocking_mode():
    from sunrise_infrastructure.db import Base

    statements = []
    installer().lock_first_activation_tables(SimpleNamespace(exec_driver_sql=statements.append))
    names = ", ".join('"' + name + '"' for name in sorted(Base.metadata.tables))
    assert statements == ["LOCK TABLE " + names + " IN SHARE ROW EXCLUSIVE MODE"]


def test_first_activation_never_promotes_unrequested_capabilities_or_overwrites_staged_files(installation):  # noqa: F811
    root, _ = installation
    module = installer()
    assert hasattr(module, "activation_files"), "Нет неизменяемых файлов первой активации"
    inputs = module.read_inputs(root, ".env.production", module.SCOPE)
    document = {"admission": module.admission_document(inputs, identity()), "manifest": inputs["manifest"]}
    module.materialize(root, inputs, document)
    originals = {relative: (root / relative).read_bytes() for relative in (module.MANIFEST, module.ADMISSION, module.RUNTIME_ENV)}
    selected = ["auth_mail", "invoice_create", "payment_reconcile", "financial_apply", "panel_mutation"]
    artifacts = module.activation_files(inputs, document, {"capabilities": selected})
    for relative, body in artifacts.items():
        module.write_private(root, relative, body)
    new_admission = json.loads(artifacts[module.ACTIVE_ADMISSION])
    assert {key for key, value in new_admission["maximum_capabilities"].items() if value} == set(selected)
    for relative, body in originals.items():
        assert (root / relative).read_bytes() == body
    assert all(value is False for value in document["admission"]["maximum_capabilities"].values())


def evidence(root, module, document, pin, *, kind="owner_reviewed_external_artifact"):
    directory = root / ".private/production/evidence"
    directory.mkdir()
    report = directory / "owner-review.txt"
    report.write_text("Синтетическая фикстура unit-теста; не является доказательством проверки провайдера.", encoding="utf-8")
    report.chmod(0o600)
    result = {
        "version": 1, "scope_id": document["admission"]["scope_id"], "install_plan_sha256": pin,
        "admission_sha256": hashlib.sha256(module.encoded(document["admission"])).hexdigest(),
        "capabilities": ["auth_mail"], "reviewed_at": datetime.now(UTC).isoformat(),
        "owner_review_confirmed": True,
        "checks": [{"check_id": name, "kind": kind, "path": ".private/production/evidence/owner-review.txt",
                    "sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
                    "summary": "Владелец проверил привязку внешнего артефакта к своему экземпляру."}
                   for name in ("tls_ingress", "smtp_delivery")],
    }
    path = root / ".private/production/first-activation-evidence.json"
    path.write_text(json.dumps(result), encoding="utf-8")
    path.chmod(0o600)
    return path, result


@pytest.mark.parametrize("change", ["mock_kind", "wrong_scope", "wrong_install_pin", "changed_artifact", "missing_check"])
def test_evidence_is_scope_bound_and_mock_kinds_cannot_authorize_activation(installation, change):  # noqa: F811
    root, _ = installation
    module = installer()
    assert hasattr(module, "read_activation_evidence"), "Нет проверки области внешних артефактов"
    inputs = module.read_inputs(root, ".env.production", module.SCOPE)
    document = {"admission": module.admission_document(inputs, identity())}
    path, value = evidence(root, module, document, "a" * 64)
    if change == "mock_kind":
        value["checks"][0]["kind"] = "mock"
    elif change == "wrong_scope":
        value["scope_id"] = "foreign-shop"
    elif change == "wrong_install_pin":
        value["install_plan_sha256"] = "b" * 64
    elif change == "missing_check":
        value["checks"].pop()
    else:
        (root / value["checks"][0]["path"]).write_text("changed", encoding="utf-8")
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError):
        module.read_activation_evidence(root, path.relative_to(root).as_posix(), document, "a" * 64, ["auth_mail"])


def test_activity_guard_denies_even_a_new_account_before_first_activation(database):  # noqa: F811
    module = installer()
    assert hasattr(module, "require_no_activity"), "Нет запрета активации поверх текущей активности"
    with database.engine.begin() as connection:
        module.require_no_activity(connection)
    with database.transaction() as session:
        session.add(Account(id=1, email="synthetic@example.test", email_verified=False))
    with database.engine.begin() as connection, pytest.raises(ValueError):
        module.require_no_activity(connection)


@pytest.mark.parametrize("change", ["generation", "gate", "recurring", "existing_receipt"])
def test_only_original_generation_one_all_off_stage_can_be_promoted(change):
    module = installer()
    assert hasattr(module, "validate_first_activation_rows"), "Нет проверки состояния первой активации"
    from sunrise_infrastructure.settings import PRODUCTION_CAPABILITIES

    marker = {"scope_id": "synthetic-shop"}
    gates = {"version": 1, "scope_id": "synthetic-shop", "generation": 1,
             "gates": dict.fromkeys(PRODUCTION_CAPABILITIES, False)}
    recurring, existing = {"enabled": False}, None
    if change == "generation":
        gates["generation"] = 2
    elif change == "gate":
        gates["gates"]["invoice_create"] = True
    elif change == "recurring":
        recurring = {"enabled": True}
    else:
        existing = {"activation_sha256": "foreign"}
    with pytest.raises(ValueError):
        module.validate_first_activation_rows(marker, gates, recurring, existing, marker, "synthetic-shop")


@pytest.fixture
def staged_installation(installation, database, monkeypatch):  # noqa: F811
    """Real ORM/transactions on SQLite; only PostgreSQL catalog/SET operations are doubled."""
    root, write_env = installation
    write_env({"OFFER_URL": "https://example.test/offer", "OFFER_VERSION": "v1",
               "PRIVACY_URL": "https://example.test/privacy", "PRIVACY_VERSION": "v1"})
    module = installer()

    @event.listens_for(database.engine, "begin")
    def explicit_outer_transaction(connection):
        connection.exec_driver_sql("BEGIN")

    @event.listens_for(database.engine, "before_cursor_execute", retval=True)
    def postgres_session_settings(_connection, _cursor, statement, parameters, _context, _many):
        if statement.startswith(("SET ", "SELECT pg_advisory_xact_lock(", "LOCK TABLE ")):
            return "SELECT 1", ()
        return statement, parameters

    monkeypatch.setattr(module, "create_engine", lambda *_a, **_k: SimpleNamespace(begin=database.engine.begin, dispose=lambda: None))
    monkeypatch.setattr(module, "database_identity", lambda _c: identity())
    monkeypatch.setattr(module, "user_relation_count", lambda _c: 0)

    def database_catalog_boundary(policy, session):
        assert getattr(session.get_bind(), "engine", session.get_bind()) is policy._database.engine

    monkeypatch.setattr(module.RuntimePolicy, "_check_database", database_catalog_boundary)
    pin = module.plan(root)
    document, _ = module.load_plan(root, pin)
    inputs = module.checked_inputs(root, document)
    inputs["bootstrap_settings"] = module.preflight(root, inputs, document)
    with database.engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE v5_alembic_version (version_num VARCHAR(32) PRIMARY KEY)")
        connection.exec_driver_sql("INSERT INTO v5_alembic_version VALUES ('v11_0019')")
        module.seed_and_receipt(connection, inputs, document, pin)
    module.admit(root, pin)
    path, _ = evidence(root, module, document, pin)
    activation_pin = module.activation_plan(root, pin, "synthetic-shop", ["auth_mail"], path.relative_to(root).as_posix())
    return module, root, pin, activation_pin, database


def test_first_activation_end_to_end_is_idempotent_before_activity_and_verify_remains_readonly(staged_installation):
    module, root, pin, activation_pin, store = staged_installation
    original = (root / module.RUNTIME_ENV).read_bytes()
    assert module.activate(root, pin, activation_pin, "synthetic-shop", True) == activation_pin
    with store.transaction() as session:
        audits = session.scalar(select(func.count()).select_from(Audit))
        assert session.get(Setting, "recurring").value["enabled"] is False
        assert {key for key, value in session.get(Setting, "production_runtime_gates").value["gates"].items() if value} == {"auth_mail"}
    assert module.activate(root, pin, activation_pin, "synthetic-shop", True) == activation_pin
    with store.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Audit)) == audits
        session.add(Account(id=1, email="synthetic@example.test", email_verified=False))
    with pytest.raises(ValueError):
        module.activate(root, pin, activation_pin, "synthetic-shop", True)
    assert module.verify_activation(root, pin, activation_pin) == activation_pin
    with pytest.raises(ValueError):
        module.verify(root, pin)
    assert (root / module.RUNTIME_ENV).read_bytes() == original


def test_failed_database_activation_keeps_closed_stage_and_exact_retry_can_resume(staged_installation, monkeypatch):
    module, root, pin, activation_pin, store = staged_installation
    previous = module.RuntimePolicy.admit

    def interrupt_before_commit(policy, bound):
        if policy.config.production_admission_file == module.ACTIVE_ADMISSION:
            raise RuntimeError("Synthetic interruption before database commit")
        return previous(policy, bound)

    monkeypatch.setattr(module.RuntimePolicy, "admit", interrupt_before_commit)
    with pytest.raises(RuntimeError, match="Synthetic interruption"):
        module.activate(root, pin, activation_pin, "synthetic-shop", True)
    with store.transaction() as session:
        assert session.get(Setting, "production_first_activation") is None
        assert session.get(Setting, "production_runtime_gates").value["generation"] == 1
        assert not any(session.get(Setting, "production_runtime_gates").value["gates"].values())
    assert (root / module.ACTIVE_ENV).is_file()
    monkeypatch.setattr(module.RuntimePolicy, "admit", previous)
    assert module.activate(root, pin, activation_pin, "synthetic-shop", True) == activation_pin


def test_mutating_activation_locks_writers_before_the_first_activity_scan(staged_installation, monkeypatch):
    module, root, pin, activation_pin, _store = staged_installation
    assert hasattr(module, "lock_first_activation_tables"), "Нет блокировки обычных записей при активации"
    events = []
    lock, scan = module.lock_first_activation_tables, module.require_no_activity

    def lock_writers(connection):
        events.append("lock")
        return lock(connection)

    def check_empty(connection):
        events.append("scan")
        return scan(connection)

    monkeypatch.setattr(module, "lock_first_activation_tables", lock_writers)
    monkeypatch.setattr(module, "require_no_activity", check_empty)
    module.activate(root, pin, activation_pin, "synthetic-shop", True)
    assert events[:2] == ["lock", "scan"]


def test_activation_evidence_does_not_coerce_numeric_owner_confirmation(installation):  # noqa: F811
    root, _ = installation
    module = installer()
    inputs = module.read_inputs(root, ".env.production", module.SCOPE)
    document = {"admission": module.admission_document(inputs, identity())}
    path, value = evidence(root, module, document, "a" * 64)
    value["owner_review_confirmed"] = 1
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError):
        module.read_activation_evidence(root, path.relative_to(root).as_posix(), document, "a" * 64, ["auth_mail"])


def test_retry_never_accepts_new_recurring_enrollment(staged_installation):
    module, root, pin, activation_pin, store = staged_installation
    module.activate(root, pin, activation_pin, "synthetic-shop", True)
    with store.transaction() as session:
        session.get(Setting, "recurring").value = {"enabled": True}
    with pytest.raises(ValueError):
        module.activate(root, pin, activation_pin, "synthetic-shop", True)
