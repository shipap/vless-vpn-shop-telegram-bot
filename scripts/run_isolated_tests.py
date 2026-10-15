"""Прогон pytest в собственных временных схемах двух локальных синтетических БД."""

import os
import re
import subprocess
import sys
from contextlib import ExitStack, contextmanager
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.schema import CreateSchema, DropSchema
from sunrise_infrastructure.commerce import bootstrap
from sunrise_infrastructure.db import Base, Database

if __package__:
    from .run_local import isolate_test_transports
else:
    from run_local import isolate_test_transports

ROOT = Path(__file__).resolve().parents[1]


def _validate_url(value, database):
    if not value:
        raise ValueError("explicit_test_database_url_required")
    parsed = make_url(value)
    if (
        parsed.drivername not in {"postgresql", "postgresql+psycopg"}
        or parsed.host not in {"127.0.0.1", "localhost"}
        or parsed.port != 55439
        or parsed.database != database
        or set(parsed.query) - {"options"}
        or parsed.query.get("options") is not None
        and not re.fullmatch(r"-csearch_path=[a-z][a-z0-9_]{0,62}", parsed.query["options"])
    ):
        raise ValueError("test_database_not_allowed")
    return parsed.set(drivername="postgresql+psycopg", query={})


def test_environment(source):
    """Использует общий запрет внешних тестовых адаптеров без изменения среды родителя."""
    original = dict(os.environ)
    try:
        os.environ.clear()
        os.environ.update(source)
        isolate_test_transports()
        return dict(os.environ)
    finally:
        os.environ.clear()
        os.environ.update(original)


def _has_foreign_dependencies(connection, schema_oid, schema_name):
    # CASCADE разрешён только когда он не затрагивает объекты чужих схем.
    return connection.scalar(
        text("""
            WITH RECURSIVE edges(classid,objid,objsubid,refclassid,refobjid,refobjsubid) AS (
                SELECT classid,objid,objsubid,refclassid,refobjid,refobjsubid FROM pg_depend
                UNION ALL
                -- Удаление внутреннего объекта (например правила VIEW) удаляет его владельца.
                SELECT refclassid,refobjid,refobjsubid,classid,objid,objsubid
                FROM pg_depend WHERE deptype='i'
            ), dependencies(classid,objid,objsubid) AS (
                SELECT 'pg_namespace'::regclass::oid, CAST(:oid AS oid), 0
                UNION
                SELECT d.classid,d.objid,d.objsubid FROM edges d
                JOIN dependencies p ON d.refclassid=p.classid AND d.refobjid=p.objid
                AND (p.objsubid=0 OR d.refobjsubid=p.objsubid)
            )
            SELECT EXISTS(
                SELECT 1 FROM dependencies d
                CROSS JOIN LATERAL pg_identify_object(d.classid,d.objid,d.objsubid) obj
                WHERE obj.schema IS NOT NULL AND obj.schema NOT IN (:name,'pg_catalog','pg_toast')
            )
        """),
        {"oid": schema_oid, "name": schema_name},
    )


@contextmanager
def _owned_database(url, schema_name):
    if not re.fullmatch(r"pytest_run_(?:backend|bot)_[a-f0-9]{32}", schema_name):
        raise ValueError("invalid_owned_schema")
    control = create_engine(url, hide_parameters=True)
    scoped = None
    identity = None
    try:
        with control.begin() as connection:
            if tuple(connection.execute(text("SELECT current_database(),inet_server_port()")).one()) != (
                url.database,
                55439,
            ):
                raise RuntimeError("test_database_identity_mismatch")
            if connection.scalar(
                text("SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=:name)"), {"name": schema_name}
            ):
                raise RuntimeError("owned_schema_already_exists")
            connection.execute(CreateSchema(schema_name))
            identity = tuple(
                connection.execute(
                    text("SELECT oid,nspowner FROM pg_namespace WHERE nspname=:name"),
                    {"name": schema_name},
                ).one()
            )
        scoped_url = url.set(query={"options": "-csearch_path=" + schema_name})
        scoped = Database(scoped_url)
        with scoped.engine.connect() as connection:
            if connection.scalar(text("SELECT current_schema()")) != schema_name:
                raise RuntimeError("owned_schema_not_current")
        Base.metadata.create_all(scoped.engine)
        yield scoped
    finally:
        try:
            if scoped is not None:
                scoped.engine.dispose()
            if identity is not None:
                with control.begin() as connection:
                    observed = connection.execute(
                        text("SELECT oid,nspowner FROM pg_namespace WHERE nspname=:name"),
                        {"name": schema_name},
                    ).first()
                    if observed is not None:
                        if tuple(observed) != identity:
                            raise RuntimeError("owned_schema_identity_changed_cleanup_denied")
                        if _has_foreign_dependencies(connection, identity[0], schema_name):
                            raise RuntimeError("foreign_schema_dependency_cleanup_denied")
                        connection.execute(DropSchema(schema_name, cascade=True))
        finally:
            control.dispose()


def run_tests(pytest_args, *, environ=None):
    source = dict(os.environ if environ is None else environ)
    # Обе БД проверяются до первого подключения или создания схемы.
    backend = _validate_url(source.get("SUNRISE_TEST_DATABASE_URL"), "sunrise_test_backend")
    bot = _validate_url(source.get("SUNRISE_BOT_TEST_DATABASE_URL"), "sunrise_test_bot")
    redis_url = source.get("SUNRISE_TEST_REDIS_URL") or "redis://127.0.0.1:56379/0"
    redis = make_url(redis_url)
    if (
        redis.drivername != "redis"
        or redis.host not in {"127.0.0.1", "localhost"}
        or redis.port != 56379
        or redis.database != "0"
        or redis.query
    ):
        raise ValueError("test_redis_not_allowed")
    child_env = test_environment(source)
    child_env.update(SUNRISE_REDIS_URL=redis_url, SUNRISE_TEST_REDIS_URL=redis_url)
    nonce = uuid4().hex
    with ExitStack() as stack:
        backend_db = stack.enter_context(_owned_database(backend, "pytest_run_backend_" + nonce))
        bot_db = stack.enter_context(_owned_database(bot, "pytest_run_bot_" + nonce))
        for database in (backend_db, bot_db):
            bootstrap(database)
        backend_url = backend_db.engine.url.render_as_string(hide_password=False)
        child_env.update(
            SUNRISE_DATABASE_URL=backend_url,
            SUNRISE_TEST_DATABASE_URL=backend_url,
            SUNRISE_BOT_TEST_DATABASE_URL=bot_db.engine.url.render_as_string(hide_password=False),
        )
        print("Тесты используют отдельные временные схемы backend/bot; существующие данные не изменяются.")
        result = subprocess.run(  # noqa: S603 -- fixed Python/pytest, validated local resources; explicit caller test arguments
            [sys.executable, "-m", "pytest", *(pytest_args or ["tests"]), "--tb=short"],
            env=child_env,
            cwd=ROOT,
            check=False,
        )
    print("Собственные временные схемы удалены; исходные схемы и backups сохранены.")
    return result.returncode


def main():
    try:
        return run_tests(sys.argv[1:])
    except (ValueError, RuntimeError, SQLAlchemyError):
        # Исключения PostgreSQL могут содержать реквизиты: DSN/trace не печатается.
        print(
            "Изолированный прогон остановлен: проверьте разрешённые локальные URL и владение схемами.",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
