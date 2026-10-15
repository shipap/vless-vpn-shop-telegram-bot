# ruff: noqa: S608 -- identifiers are fixture-generated UUID schemas or this runner's validated owned schemas
import importlib
import os
import subprocess
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema, DropSchema
from sunrise_infrastructure.db import Account, Product


def runner_module():
    try:
        return importlib.import_module("scripts.run_isolated_tests")
    except ModuleNotFoundError:
        pytest.fail("Fresh owned-schema test runner is missing")


@pytest.fixture
def runner_environment():
    value = os.getenv("SUNRISE_TEST_DATABASE_URL")
    if not value:
        pytest.skip("Synthetic loopback PostgreSQL required")
    parsed = make_url(value)
    assert (parsed.host, parsed.port, parsed.database) == ("127.0.0.1", 55439, "sunrise_test_backend")
    backend = parsed.set(query={})
    bot = backend.set(database="sunrise_test_bot")
    env = dict(
        os.environ,
        SUNRISE_TEST_DATABASE_URL=backend.render_as_string(hide_password=False),
        SUNRISE_BOT_TEST_DATABASE_URL=bot.render_as_string(hide_password=False),
        SUNRISE_TEST_REDIS_URL="redis://127.0.0.1:56379/0",
    )
    engines = [create_engine(backend, hide_parameters=True), create_engine(bot, hide_parameters=True)]
    try:
        yield env, engines
    finally:
        for engine in engines:
            engine.dispose()


def schema_exists(engine, name):
    with engine.connect() as connection:
        return connection.scalar(
            text("SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=:name)"), {"name": name}
        )


@pytest.mark.parametrize("result", [0, 7])
def test_run_scopes_both_databases_seeds_clean_catalog_and_cleans_only_owned_schemas(
    runner_environment,
    monkeypatch,
    result,
):
    module = runner_module()
    env, engines = runner_environment
    foreign = "runner_foreign_" + uuid4().hex
    for engine in engines:
        with engine.begin() as connection:
            connection.execute(CreateSchema(foreign))
            connection.execute(text(f'CREATE TABLE "{foreign}".sentinel(value text)'))
            connection.execute(
                text(f'INSERT INTO "{foreign}".sentinel VALUES (:value)'), {"value": "preserve"}
            )
    seen = []

    def child(command, *, env, cwd, check):
        assert command[1:3] == ["-m", "pytest"]
        assert command[3:5] == ["tests/unit/test_staff_sale_content.py", "-q"]
        assert check is False
        assert env["SUNRISE_DATABASE_URL"] == env["SUNRISE_TEST_DATABASE_URL"]
        assert env["SUNRISE_REDIS_URL"] == env["SUNRISE_TEST_REDIS_URL"] == "redis://127.0.0.1:56379/0"
        urls = [make_url(env[key]) for key in ("SUNRISE_TEST_DATABASE_URL", "SUNRISE_BOT_TEST_DATABASE_URL")]
        assert [url.database for url in urls] == ["sunrise_test_backend", "sunrise_test_bot"]
        for url in urls:
            name = url.query["options"].removeprefix("-csearch_path=")
            assert name != "public" and "," not in name
            seen.append(name)
            current = create_engine(url, hide_parameters=True)
            with current.begin() as connection:
                assert connection.scalar(text("SELECT current_schema()")) == name
                assert connection.scalar(text("SELECT count(*) FROM v5_products")) == 2
                assert connection.scalar(text("SELECT count(*) FROM v5_accounts")) == 0
                assert set(connection.scalars(select(Product.id))) == {"standard", "universal"}
                connection.execute(Account.__table__.insert().values(email="runner@example.test"))
            current.dispose()
        assert seen[0] != seen[1]
        assert env["SUNRISE_PANEL_TEST_AUTHORIZED"] == env["SUNRISE_SMTP_TEST_AUTHORIZED"] == "false"
        return subprocess.CompletedProcess(command, result)

    monkeypatch.setattr(module.subprocess, "run", child)
    try:
        assert module.run_tests(["tests/unit/test_staff_sale_content.py", "-q"], environ=env) == result
        for engine, name in zip(engines, seen, strict=True):
            assert not schema_exists(engine, name)
            with engine.connect() as connection:
                assert connection.scalar(text(f'SELECT value FROM "{foreign}".sentinel')) == "preserve"
    finally:
        for engine in engines:
            with engine.begin() as connection:
                connection.execute(DropSchema(foreign, cascade=True))


def test_failed_bootstrap_cleans_created_schemas_without_launching_tests(runner_environment, monkeypatch):
    module = runner_module()
    env, engines = runner_environment
    seen = []

    def fail(db):
        seen.append(db.engine.url.query["options"].removeprefix("-csearch_path="))
        raise RuntimeError("synthetic_bootstrap_failure")

    monkeypatch.setattr(module, "bootstrap", fail)
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: pytest.fail("Child launched"))
    with pytest.raises(RuntimeError, match="synthetic_bootstrap_failure"):
        module.run_tests([], environ=env)
    assert len(seen) == 1
    assert not schema_exists(engines[0], seen[0])
    assert not schema_exists(engines[1], seen[0].replace("_backend_", "_bot_"))


def test_child_exception_cleans_both_schemas(runner_environment, monkeypatch):
    module = runner_module()
    env, engines = runner_environment
    seen = []

    def child(command, *, env, cwd, check):
        seen.extend(
            make_url(env[key]).query["options"].removeprefix("-csearch_path=")
            for key in ("SUNRISE_TEST_DATABASE_URL", "SUNRISE_BOT_TEST_DATABASE_URL")
        )
        raise KeyboardInterrupt

    monkeypatch.setattr(module.subprocess, "run", child)
    with pytest.raises(KeyboardInterrupt):
        module.run_tests([], environ=env)
    assert len(seen) == 2
    assert all(not schema_exists(engine, name) for engine, name in zip(engines, seen, strict=True))


def test_replaced_schema_identity_is_preserved_and_cleanup_fails_closed(runner_environment, monkeypatch):
    module = runner_module()
    env, engines = runner_environment
    seen = []

    def child(command, *, env, cwd, check):
        seen.extend(
            make_url(env[key]).query["options"].removeprefix("-csearch_path=")
            for key in ("SUNRISE_TEST_DATABASE_URL", "SUNRISE_BOT_TEST_DATABASE_URL")
        )
        with engines[0].begin() as connection:
            connection.execute(DropSchema(seen[0], cascade=True))
            connection.execute(CreateSchema(seen[0]))
            connection.execute(text(f'CREATE TABLE "{seen[0]}".sentinel(value text)'))
            connection.execute(
                text(f'INSERT INTO "{seen[0]}".sentinel VALUES (:value)'), {"value": "replacement"}
            )
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(module.subprocess, "run", child)
    try:
        with pytest.raises(RuntimeError, match="identity_changed_cleanup_denied"):
            module.run_tests([], environ=env)
        assert not schema_exists(engines[1], seen[1])
        with engines[0].connect() as connection:
            assert connection.scalar(text(f'SELECT value FROM "{seen[0]}".sentinel')) == "replacement"
    finally:
        if seen:
            with engines[0].begin() as connection:
                connection.execute(DropSchema(seen[0], cascade=True))


def test_foreign_view_dependency_blocks_cascade_without_altering_foreign_schema(
    runner_environment,
    monkeypatch,
):
    module = runner_module()
    env, engines = runner_environment
    foreign = "runner_view_" + uuid4().hex
    seen = []
    with engines[0].begin() as connection:
        connection.execute(CreateSchema(foreign))

    def child(command, *, env, cwd, check):
        seen.extend(
            make_url(env[key]).query["options"].removeprefix("-csearch_path=")
            for key in ("SUNRISE_TEST_DATABASE_URL", "SUNRISE_BOT_TEST_DATABASE_URL")
        )
        with engines[0].begin() as connection:
            connection.execute(
                text(f'CREATE VIEW "{foreign}".catalog AS SELECT id FROM "{seen[0]}".v5_products')
            )
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(module.subprocess, "run", child)
    try:
        with pytest.raises(RuntimeError, match="foreign_schema_dependency_cleanup_denied"):
            module.run_tests([], environ=env)
        assert not schema_exists(engines[1], seen[1])
        with engines[0].connect() as connection:
            assert connection.scalar(text(f'SELECT count(*) FROM "{foreign}".catalog')) == 2
    finally:
        with engines[0].begin() as connection:
            connection.execute(DropSchema(foreign, cascade=True))
            if seen and schema_exists(engines[0], seen[0]):
                connection.execute(DropSchema(seen[0], cascade=True))


def test_existing_schema_collision_never_claims_or_removes_foreign_schema(runner_environment, monkeypatch):
    module = runner_module()
    env, engines = runner_environment
    nonce = UUID("11111111-2222-4333-8444-555555555555")
    foreign = "pytest_run_backend_" + nonce.hex
    with engines[0].begin() as connection:
        connection.execute(CreateSchema(foreign))
        connection.execute(text(f'CREATE TABLE "{foreign}".sentinel(value text)'))
        connection.execute(text(f'INSERT INTO "{foreign}".sentinel VALUES (:value)'), {"value": "foreign"})
    monkeypatch.setattr(module, "uuid4", lambda: nonce)
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: pytest.fail("Child launched"))
    try:
        with pytest.raises(RuntimeError, match="already_exists"):
            module.run_tests([], environ=env)
        with engines[0].connect() as connection:
            assert connection.scalar(text(f'SELECT value FROM "{foreign}".sentinel')) == "foreign"
    finally:
        with engines[0].begin() as connection:
            connection.execute(DropSchema(foreign, cascade=True))
