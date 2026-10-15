"""Проверка собственных снимков в закрытой одноразовой БД без прикладного runtime."""

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL
from sunrise_core.errors import DomainError

MAX_ARCHIVE_BYTES = 49_000_000
MAX_STORAGE_BYTES = 512 * 1024 * 1024
MIN_FREE_BYTES = 200 * 1024 * 1024
DATABASES = {"sunrise_test", "sunrise_test_backend", "sunrise_test_bot"}
TOC_TYPES = (
    "SEQUENCE OWNED BY",
    "SEQUENCE SET",
    "FK CONSTRAINT",
    "TABLE DATA",
    "CONSTRAINT",
    "SEQUENCE",
    "DEFAULT",
    "INDEX",
    "TABLE",
    "SCHEMA",
)


class BackupIncomplete(DomainError):
    def __init__(self):
        super().__init__("backup_incomplete", "Полнота резервной копии не подтверждена.", 503)


def validate_toc(value, schema):
    """Принимает только обычные объекты собственного pg_dump, без исполняемых расширений."""
    entries = []
    for line in value.splitlines():
        if not line or line.startswith(";"):
            continue
        match = re.fullmatch(r"\d+; \d+ \d+ (.+)", line)
        if not match:
            raise ValueError("backup_objects_denied")
        tail = match[1]
        kind = next((kind for kind in TOC_TYPES if tail.startswith(kind + " ")), None)
        if not kind:
            raise ValueError("backup_objects_denied")
        fields = tail[len(kind) + 1 :].split()
        if (
            not fields
            or (kind == "SCHEMA" and fields[:2] != ["-", schema])
            or (kind != "SCHEMA" and fields[0] != schema)
        ):
            raise ValueError("backup_objects_denied")
        entries.append((kind, line))
    if not any(kind == "TABLE DATA" for kind, _ in entries):
        raise ValueError("backup_objects_denied")
    return entries


def snapshot_manifest(connection, schema):
    """Считает все поля и явные финансовые/владельческие связи в одной транзакции снимка."""
    inspector = inspect(connection)
    names = sorted(inspector.get_table_names(schema=schema))
    if not {
        "v5_accounts",
        "v5_orders",
        "v5_payments",
        "v5_subscriptions",
        "v5_credit_ledger",
        "v5_bonus_ledger",
    }.issubset(names):
        raise DomainError("backup_schema_denied", "Схема приложения не подтверждена.", 403)
    quote = connection.dialect.identifier_preparer.quote
    tables, relations = {}, {}
    for name in names:
        qualified = quote(schema) + "." + quote(name)
        digest, count = hashlib.sha256(), 0
        # Metadata identifiers are dialect-quoted; values never enter SQL text or reports.
        rows = connection.execution_options(stream_results=True).execute(
            text(
                f'SELECT row_to_json(t)::text FROM {qualified} t ORDER BY row_to_json(t)::text COLLATE "C"'  # noqa: S608 -- dialect-quoted inspector identifiers only
            )
        )
        for row in rows:
            digest.update(row[0].encode("utf-8") + b"\n")
            count += 1
        tables[name] = {
            "columns": [
                {"name": column["name"], "type": str(column["type"]), "nullable": column["nullable"]}
                for column in inspector.get_columns(name, schema=schema)
            ],
            "rows": count,
            "sha256": digest.hexdigest(),
        }
        for foreign in inspector.get_foreign_keys(name, schema=schema):
            reference_schema = foreign.get("referred_schema") or schema
            if reference_schema != schema:
                raise DomainError("backup_schema_denied", "Внешние зависимости схемы не разрешены.", 403)
            columns, referenced = foreign["constrained_columns"], foreign["referred_columns"]
            child_present = " AND ".join("c." + quote(column) + " IS NOT NULL" for column in columns)
            join = " AND ".join(
                "c." + quote(child) + "=p." + quote(parent)
                for child, parent in zip(columns, referenced, strict=True)
            )
            orphan_count = connection.scalar(
                text(
                    f"SELECT count(*) FROM {qualified} c WHERE {child_present} AND NOT EXISTS(SELECT 1 FROM {quote(schema)}.{quote(foreign['referred_table'])} p WHERE {join})"  # noqa: S608 -- dialect-quoted inspector identifiers only
                )
            )
            relations[name + ":" + foreign["name"]] = {
                "columns": columns,
                "parent": foreign["referred_table"],
                "parent_columns": referenced,
                "orphans": orphan_count,
            }
    sequences = list(
        connection.scalars(
            text(
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=:schema AND c.relkind='S' ORDER BY c.relname"
            ),
            {"schema": schema},
        )
    )
    return {"schema": schema, "tables": tables, "relations": relations, "sequences": sequences}


class BackupVerification:
    def __init__(self, db, settings, *, storage_dir=None):
        self.db, self.settings = db, settings
        self._initial_references = {}
        self._initial_catalog_references = {}
        self.root = Path(__file__).resolve().parents[4]
        self.folder = (
            Path(storage_dir) if storage_dir is not None else self.root / ".private/backups/owner-daily"
        )
        url = db.engine.url
        if (
            not settings.local_test
            or url.host not in {"localhost", "127.0.0.1"}
            or url.port != 55439
            or url.database not in DATABASES
            or set(url.query) - {"options"}
            or url.query.get("options")
            and not re.fullmatch(r"-csearch_path=[a-z][a-z0-9_]{0,62}", url.query["options"])
        ):
            raise DomainError("backup_scope_denied", "Копирование этой базы запрещено.", 403)
        self.resources = json.loads(
            (self.root / ".private/local-resources.json").read_text(encoding="utf-8-sig")
        )
        self.tools = Path(self.resources["pg_bin"])
        self.admin_kwargs = dict(
            host="127.0.0.1", port=55439, user="sunrise_local", password=self.resources["password"]
        )

    def _paths(self, identifier):
        base, _, attempt = identifier.partition("-attempt-")
        if str(UUID(base)) != base or attempt and attempt not in {"2", "3"}:
            raise ValueError("backup_identifier_denied")
        self.folder.mkdir(parents=True, exist_ok=True)
        if any(parent.is_symlink() for parent in (self.folder, *self.folder.parents)):
            raise ValueError("backup_path_denied")
        return self.folder / (identifier + ".dump"), self.folder / (identifier + ".manifest.json")

    def _sign(self, manifest):
        return hmac.new(
            self.resources["password"].encode(),
            json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode(),
            hashlib.sha256,
        ).hexdigest()

    def _run(self, binary, arguments, *, username, password):
        tool = self.tools / (binary + ".exe")
        if not tool.is_file():
            raise DomainError("backup_verify_unavailable", "Инструмент проверки недоступен.", 503)
        try:
            result = subprocess.run(  # noqa: S603 -- fixed PostgreSQL executable and internal argv, no shell/untrusted SQL
                [str(tool), *arguments],
                env=dict(os.environ, PGPASSWORD=password),
                capture_output=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise DomainError(
                "backup_verify_unavailable", "Проверка копии временно недоступна.", 503
            ) from None
        if result.returncode:
            raise DomainError("backup_verify_unavailable", "Проверка копии не завершена.", 503)
        return result.stdout.decode("utf-8")

    def _connection_args(self, database, username):
        return ["-h", "127.0.0.1", "-p", "55439", "-U", username, "-d", database]

    def capture(self, identifier):
        archive, manifest_path = self._paths(identifier)
        if archive.exists() or manifest_path.exists():
            raise DomainError("backup_file_exists", "Существующая копия не перезаписывается.", 409)
        occupied = sum(
            path.stat().st_size for path in self.folder.iterdir() if path.is_file() and not path.is_symlink()
        )
        if occupied >= MAX_STORAGE_BYTES or shutil.disk_usage(self.folder).free < MIN_FREE_BYTES:
            raise DomainError("backup_disk_limit", "Недостаточно места для безопасной проверки копии.", 503)
        with self.db.engine.connect() as connection:
            connection = connection.execution_options(isolation_level="REPEATABLE READ")
            with connection.begin():
                connection.execute(text("SET TRANSACTION READ ONLY"))
                connection.execute(text("SET LOCAL TIME ZONE 'UTC'"))
                connection.execute(text("SET LOCAL statement_timeout='120s'"))
                if connection.execute(text("SELECT current_database(),inet_server_port()")).one() != (
                    self.db.engine.url.database,
                    55439,
                ):
                    raise DomainError("backup_scope_denied", "Экземпляр БД не подтверждён.", 403)
                schema = connection.scalar(text("SELECT current_schema()"))
                if not schema or not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema):
                    raise DomainError("backup_schema_denied", "Схема копии не разрешена.", 403)
                executable = connection.scalar(
                    text("""
                    SELECT EXISTS(SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname=:schema)
                    OR EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=:schema AND (c.relkind IN ('v','m','f') OR c.relrowsecurity))
                    OR EXISTS(SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=:schema AND NOT t.tgisinternal)
                """),
                    {"schema": schema},
                )
                if executable:
                    raise DomainError(
                        "backup_schema_denied", "Неаудированные исполняемые объекты схемы запрещены.", 403
                    )
                size = connection.scalar(
                    text(
                        "SELECT coalesce(sum(pg_total_relation_size(c.oid)),0) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=:schema AND c.relkind IN ('r','p','m')"
                    ),
                    {"schema": schema},
                )
                if size > 128 * 1024 * 1024:
                    raise DomainError("backup_disk_limit", "Схема превышает лимит локальной проверки.", 503)
                snapshot = connection.scalar(text("SELECT pg_export_snapshot()"))
                manifest = snapshot_manifest(connection, schema)
                url = self.db.engine.url
                self._run(
                    "pg_dump",
                    [
                        *self._connection_args(url.database, url.username),
                        "--format=custom",
                        "--no-owner",
                        "--no-privileges",
                        "--schema=" + schema,
                        "--snapshot=" + snapshot,
                        "-f",
                        str(archive),
                    ],
                    username=url.username,
                    password=url.password or "",
                )
        if archive.stat().st_size > MAX_ARCHIVE_BYTES:
            raise DomainError("backup_too_large", "Копия превышает лимит проверки и Telegram.", 409)
        manifest.update(
            identifier=identifier,
            database=self.db.engine.url.database,
            archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        )
        self._archive_objects(archive, manifest)
        signed = {"manifest": manifest, "signature": self._sign(manifest)}
        with manifest_path.open("x", encoding="utf-8") as output:
            json.dump(signed, output, ensure_ascii=False)
        return archive

    def _archive_objects(self, archive, manifest):
        args = dict(username="sunrise_local", password=self.resources["password"])
        listing = self._run("pg_restore", ["--list", str(archive)], **args)
        entries = validate_toc(listing, manifest["schema"])
        sequence_list = archive.with_suffix(".sequence-list")
        sequence_sql = archive.with_suffix(".sequence.sql")
        with sequence_list.open("x", encoding="utf-8") as output:
            output.write("\n".join(line for kind, line in entries if kind == "SEQUENCE SET"))
        try:
            self._run(
                "pg_restore",
                ["--use-list=" + str(sequence_list), "--file=" + str(sequence_sql), str(archive)],
                **args,
            )
            values = {}
            for line in sequence_sql.read_text(encoding="utf-8").splitlines():
                if line.startswith("SELECT pg_catalog.setval("):
                    match = re.fullmatch(
                        r"SELECT pg_catalog\.setval\('([a-z][a-z0-9_]*)\.([a-z][a-z0-9_]*)', (-?\d+), (true|false)\);",
                        line,
                    )
                    if not match or match[1] != manifest["schema"]:
                        raise ValueError("backup_sequences_denied")
                    values[match[2]] = [int(match[3]), match[4] == "true"]
            if set(values) != set(manifest["sequences"]):
                raise BackupIncomplete()
            manifest["sequence_values"] = values
        finally:
            sequence_list.unlink(missing_ok=True)
            sequence_sql.unlink(missing_ok=True)

    def verify(self, archive, identifier):
        expected, manifest_path = self._paths(identifier)
        archive = Path(archive)
        if archive != expected or archive.is_symlink() or not archive.is_file() or manifest_path.is_symlink():
            raise DomainError("backup_provenance_denied", "Происхождение копии не подтверждено.", 403)
        signed = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest = signed["manifest"]
        if (
            not hmac.compare_digest(signed["signature"], self._sign(manifest))
            or manifest["identifier"] != identifier
            or manifest["database"] != self.db.engine.url.database
        ):
            raise DomainError("backup_provenance_denied", "Происхождение копии не подтверждено.", 403)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != manifest["archive_sha256"]:
            raise BackupIncomplete()
        target, role = "sunrise_backup_check_" + uuid4().hex, "sunrise_backup_role_" + uuid4().hex
        password = secrets.token_urlsafe(32)
        target_identity, role_oid = None, None
        engine = None
        journal = self.folder / (identifier + ".restore-" + uuid4().hex + ".json")
        record = {"target": target, "role": role, "state": "preparing"}
        try:
            with psycopg.connect(**self.admin_kwargs, dbname="postgres", autocommit=True) as admin:
                if admin.execute("SELECT current_user,inet_server_port()").fetchone() != (
                    "sunrise_local",
                    55439,
                ):
                    raise DomainError("backup_scope_denied", "Экземпляр проверки не подтверждён.", 403)
                if (
                    admin.execute("SELECT 1 FROM pg_database WHERE datname=%s", (target,)).fetchone()
                    or admin.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone()
                ):
                    raise DomainError("backup_target_exists", "Проверочная БД уже существует.", 409)
                admin.execute(
                    sql.SQL(
                        "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {}"
                    ).format(sql.Identifier(role), sql.Literal(password))
                )
                role_oid = admin.execute("SELECT oid FROM pg_roles WHERE rolname=%s", (role,)).fetchone()[0]
                record.update(role_oid=role_oid, state="role_created")
                with journal.open("x", encoding="utf-8") as output:
                    json.dump({**record, "signature": self._sign(record)}, output)
                admin.execute(
                    sql.SQL("CREATE DATABASE {} OWNER {} TEMPLATE template0").format(
                        sql.Identifier(target), sql.Identifier(role)
                    )
                )
                target_identity = admin.execute(
                    "SELECT oid,datdba FROM pg_database WHERE datname=%s", (target,)
                ).fetchone()
                record.update(
                    database_oid=target_identity[0], database_owner=target_identity[1], state="target_created"
                )
                journal.write_text(json.dumps({**record, "signature": self._sign(record)}), encoding="utf-8")
                admin.execute(
                    sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC, sunrise_app").format(
                        sql.Identifier(target)
                    )
                )
                if admin.execute(
                    "SELECT has_database_privilege('sunrise_app',%s,'CONNECT')", (target,)
                ).fetchone()[0]:
                    raise DomainError(
                        "backup_runtime_denied", "Проверочная БД должна быть закрыта от runtime.", 403
                    )
                template_oid = admin.execute(
                    "SELECT oid FROM pg_database WHERE datname='template0'"
                ).fetchone()[0]
                template_references = self._foreign_references(admin, template_oid, role_oid)
                initial_references = self._foreign_references(admin, target_identity[0], role_oid)
                if not initial_references.issubset(template_references):
                    raise DomainError(
                        "backup_cleanup_denied",
                        "Начальные объекты проверочной БД не совпадают с template0.",
                        503,
                    )
                self._initial_references[target_identity[0]] = initial_references
                record["initial_foreign_references"] = sorted(initial_references)
                initial_catalog_references = self._catalog_references(target, target_identity[0], role_oid)
                self._initial_catalog_references[target_identity[0]] = initial_catalog_references
                record["initial_catalog_references"] = sorted(initial_catalog_references)
                journal.write_text(json.dumps({**record, "signature": self._sign(record)}), encoding="utf-8")
            self._run(
                "pg_restore",
                [
                    *self._connection_args(target, role),
                    "--single-transaction",
                    "--exit-on-error",
                    "--no-owner",
                    "--no-privileges",
                    str(archive),
                ],
                username=role,
                password=password,
            )
            engine = create_engine(
                URL.create(
                    "postgresql+psycopg",
                    username=role,
                    password=password,
                    host="127.0.0.1",
                    port=55439,
                    database=target,
                ),
                hide_parameters=True,
            )
            with engine.connect() as connection:
                with connection.begin():
                    connection.execute(text("SET TRANSACTION READ ONLY"))
                    connection.execute(text("SET LOCAL TIME ZONE 'UTC'"))
                    connection.execute(text("SET LOCAL statement_timeout='120s'"))
                    restored = snapshot_manifest(connection, manifest["schema"])
                    original = {key: manifest[key] for key in ("schema", "tables", "relations", "sequences")}
                    if restored != original:
                        raise BackupIncomplete()
                    quote = connection.dialect.identifier_preparer.quote
                    for name, value in manifest["sequence_values"].items():
                        if (
                            list(
                                connection.execute(
                                    text(
                                        f"SELECT last_value,is_called FROM {quote(manifest['schema'])}.{quote(name)}"  # noqa: S608 -- signed inspector identifiers, dialect-quoted
                                    )
                                ).one()
                            )
                            != value
                        ):  # noqa: S608 -- signed inspector-derived identifiers quoted
                            raise BackupIncomplete()
        finally:
            if engine is not None:
                engine.dispose()
            try:
                self._cleanup(target, role, target_identity, role_oid)
            except Exception:  # noqa: BLE001 -- retain exact owned resource identities, no automatic prefix cleanup
                if journal.exists():
                    record["state"] = "cleanup_requires_review"
                    journal.write_text(
                        json.dumps({**record, "signature": self._sign(record)}), encoding="utf-8"
                    )
                raise
            if journal.exists():
                record["state"] = "cleaned"
                journal.write_text(json.dumps({**record, "signature": self._sign(record)}), encoding="utf-8")
        return {
            "state": "verified",
            "table_count": len(manifest["tables"]),
            "sequence_count": len(manifest["sequences"]),
            "checked_at": datetime.now(UTC).isoformat(),
            "snapshot_equal": True,
            "relations_equal": True,
            "runtime_connect_denied": True,
            "target_removed": True,
        }

    def _foreign_references(self, admin, database_oid, role_oid):
        return set(
            admin.execute(
                "SELECT classid,objid,objsubid,refobjid,deptype FROM pg_shdepend WHERE dbid=%s AND refclassid='pg_authid'::regclass AND refobjid<>%s",
                (database_oid, role_oid),
            ).fetchall()
        )

    def _catalog_references(self, target, database_oid, role_oid):
        # Pinned bootstrap roles have no pg_shdepend entries. Inspect every catalogue
        # role reference, including functions/types/collations, from PostgreSQL's own FK metadata.
        references = set()
        with psycopg.connect(**self.admin_kwargs, dbname=target, autocommit=True) as connection:
            connection.execute("SET statement_timeout='120s'")
            catalogues = connection.execute(
                "SELECT f.fktable::oid,c.relname,c.relisshared,f.fkcols,f.is_array FROM pg_get_catalog_foreign_keys() f JOIN pg_class c ON c.oid=f.fktable WHERE f.pktable='pg_authid'::regclass"
            ).fetchall()
            for catalogue_oid, name, shared, columns, is_array in catalogues:
                if len(columns) != 1:
                    raise DomainError("backup_cleanup_denied", "Неизвестная ссылка системного каталога.", 503)
                column = columns[0]
                if shared and name not in {"pg_subscription", "pg_db_role_setting"}:
                    # Cluster-wide roles/databases/tablespaces are checked on the admin connection.
                    if name not in {"pg_database", "pg_tablespace", "pg_auth_members"}:
                        raise DomainError("backup_cleanup_denied", "Неизвестный общий каталог.", 503)
                    continue
                object_column = "setdatabase" if name == "pg_db_role_setting" else "oid"
                value = (
                    sql.SQL("unnest({})").format(sql.Identifier(column))
                    if is_array
                    else sql.Identifier(column)
                )
                query = sql.SQL("SELECT {},{} FROM pg_catalog.{}").format(
                    sql.Identifier(object_column), value, sql.Identifier(name)
                )
                parameters = ()
                if shared:
                    scope_column = "subdbid" if name == "pg_subscription" else "setdatabase"
                    query += sql.SQL(" WHERE {}=%s").format(sql.Identifier(scope_column))
                    parameters = (database_oid,)
                for object_oid, foreign_role in connection.execute(query, parameters):
                    if foreign_role not in {0, role_oid}:
                        references.add((catalogue_oid, object_oid, column, foreign_role))
        return references

    def _cleanup(self, target, role, identity, role_oid):
        if not re.fullmatch(r"sunrise_backup_check_[a-f0-9]{32}", target) or not re.fullmatch(
            r"sunrise_backup_role_[a-f0-9]{32}", role
        ):
            raise DomainError("backup_cleanup_denied", "Имя проверочного ресурса не подтверждено.", 403)
        if role_oid is None:
            return
        with psycopg.connect(**self.admin_kwargs, dbname="postgres", autocommit=True) as admin:
            observed = admin.execute(
                "SELECT oid,datdba FROM pg_database WHERE datname=%s", (target,)
            ).fetchone()
            observed_role = admin.execute("SELECT oid FROM pg_roles WHERE rolname=%s", (role,)).fetchone()
            if observed_role != (role_oid,) or observed is not None and observed != identity:
                raise DomainError("backup_cleanup_denied", "Очистка требует проверки владельца БД.", 503)
            foreign = admin.execute(
                "SELECT EXISTS(SELECT 1 FROM pg_shdepend WHERE refclassid='pg_authid'::regclass AND refobjid=%s AND ((dbid<>0 AND dbid<>%s) OR (dbid=0 AND NOT (classid='pg_database'::regclass AND objid=%s)))) OR EXISTS(SELECT 1 FROM pg_auth_members WHERE roleid=%s OR member=%s OR grantor=%s)",
                (
                    role_oid,
                    identity[0] if identity else 0,
                    identity[0] if identity else 0,
                    role_oid,
                    role_oid,
                    role_oid,
                ),
            ).fetchone()[0]
            if foreign:
                raise DomainError(
                    "backup_cleanup_denied", "Роль связана с чужими ресурсами; очистка остановлена.", 503
                )
            if observed is not None:
                if (
                    identity[0] not in self._initial_references
                    or self._foreign_references(admin, identity[0], role_oid)
                    - self._initial_references[identity[0]]
                    or identity[0] not in self._initial_catalog_references
                    or self._catalog_references(target, identity[0], role_oid)
                    - self._initial_catalog_references[identity[0]]
                ):
                    raise DomainError(
                        "backup_cleanup_denied",
                        "В проверочной БД обнаружены чужие объекты или отсутствует baseline; очистка остановлена.",
                        503,
                    )
                deadline = time.monotonic() + 1
                while admin.execute(
                    "SELECT count(*) FROM pg_stat_activity WHERE datid=%s", (identity[0],)
                ).fetchone()[0]:
                    if time.monotonic() >= deadline:
                        raise DomainError("backup_cleanup_denied", "Проверочная БД ещё используется.", 503)
                    time.sleep(0.05)
                admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(target)))
            admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
