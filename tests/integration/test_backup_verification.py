import importlib
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import Account, Job, Notification
from sunrise_infrastructure.scheduler import Scheduler
from test_v11_notifications import notification_context as notification_context


def verifier_module():
    try:
        return importlib.import_module("sunrise_infrastructure.backup_verification")
    except ModuleNotFoundError:
        pytest.fail("Closed snapshot backup verification is absent")


def test_real_backup_restores_exact_snapshot_and_cleans_only_owned_target(notification_context, tmp_path):
    module = verifier_module()
    db, settings, _ = notification_context
    verifier = module.BackupVerification(db, settings, storage_dir=tmp_path)
    identifier = str(uuid4())
    path = verifier.capture(identifier)
    original = path.read_bytes()
    # A changing live account balance must not invalidate the recorded snapshot.
    with db.transaction() as session:
        account = session.scalar(select(Account).limit(1))
        account.credit_balance += 7
    result = verifier.verify(path, identifier)
    assert result["state"] == "verified" and result["snapshot_equal"] is True
    assert result["relations_equal"] is True and result["runtime_connect_denied"] is True
    assert result["table_count"] >= 40 and result["target_removed"] is True
    assert path.read_bytes() == original
    import json

    journals = list(tmp_path.glob(identifier + ".restore-*.json"))
    assert len(journals) == 1
    journal = json.loads(journals[0].read_text(encoding="utf-8"))
    assert journal["state"] == "cleaned" and journal["database_oid"] > 0 and journal["role_oid"] > 0


def test_corrupted_owned_archive_is_rejected_before_restore(notification_context, tmp_path):
    module = verifier_module()
    db, settings, _ = notification_context
    verifier = module.BackupVerification(db, settings, storage_dir=tmp_path)
    identifier = str(uuid4())
    path = verifier.capture(identifier)
    path.write_bytes(path.read_bytes()[:-100])
    with pytest.raises(module.BackupIncomplete):
        verifier.verify(path, identifier)


def test_custom_archive_executable_objects_are_refused():
    module = verifier_module()
    with pytest.raises(ValueError, match="backup_objects_denied"):
        module.validate_toc("123; 1255 999 FUNCTION public evil() sunrise_local", "public")
    with pytest.raises(ValueError, match="backup_objects_denied"):
        module.validate_toc("123; 1259 999 TABLE other customer sunrise_local", "public")


def test_unreviewed_source_function_is_refused_before_backup_creation(notification_context, tmp_path):
    module = verifier_module()
    db, settings, _ = notification_context
    with db.transaction() as session:
        session.execute(
            text("CREATE FUNCTION unreviewed_backup_function() RETURNS int LANGUAGE sql AS 'SELECT 1'")
        )
    verifier = module.BackupVerification(db, settings, storage_dir=tmp_path)
    from sunrise_core.errors import DomainError

    with pytest.raises(DomainError) as caught:
        verifier.capture(str(uuid4()))
    assert caught.value.code == "backup_schema_denied"
    assert not list(tmp_path.glob("*.dump"))


@pytest.mark.parametrize("failures", [0, 2, 3])
def test_each_backup_verifies_and_replaces_only_confirmed_incomplete_before_single_send(
    notification_context, tmp_path, failures
):
    module = verifier_module()
    db, settings, (owner, _, _) = notification_context
    captures, verifications, sends = [], [], []

    def capture(identifier):
        captures.append(identifier)
        path = tmp_path / (identifier + ".dump")
        path.write_bytes(b"synthetic owned archive")
        return path

    def verify(path, identifier):
        verifications.append(path)
        if len(verifications) <= failures:
            raise module.BackupIncomplete()
        return {"state": "verified", "table_count": 59, "snapshot_equal": True, "relations_equal": True}

    with db.transaction() as session:
        job = Job(
            key="verify:" + str(uuid4()),
            kind="daily_backup",
            account_id=owner,
            payload={"database": db.engine.url.database, "recipient": settings.owner_id},
        )
        session.add(job)
        session.flush()
        job_id = job.id
    scheduler = Scheduler(
        Commerce(db, local_test=True, public_settings=settings),
        settings,
        dump_database=capture,
        verify_database=verify,
        send_document=lambda *args: sends.append(args),
    )
    scheduler.deliver_backups()
    scheduler.deliver_backups()
    assert len(captures) == len(verifications) == min(failures + 1, 3)
    assert len(set(captures)) == len(captures)
    assert len(sends) == (0 if failures == 3 else 1)
    with db.transaction() as session:
        current = session.get(Job, job_id)
        assert current.state == ("review" if failures == 3 else "done")
        assert current.payload["verification"]["state"] == ("incomplete" if failures == 3 else "verified")


def test_unknown_verification_does_not_recreate_or_send_backup(notification_context, tmp_path):
    verifier_module()
    db, settings, (owner, _, _) = notification_context
    calls = []
    path = tmp_path / "synthetic.dump"
    path.write_bytes(b"synthetic")

    def verify(*args):
        raise TimeoutError("unavailable local verifier")

    with db.transaction() as session:
        session.add(
            Job(
                key="verify-unknown",
                kind="daily_backup",
                account_id=owner,
                payload={"database": db.engine.url.database, "recipient": settings.owner_id},
            )
        )
    scheduler = Scheduler(
        Commerce(db, local_test=True, public_settings=settings),
        settings,
        dump_database=lambda identifier: calls.append(identifier) or path,
        verify_database=verify,
        send_document=lambda *args: pytest.fail("Unverified document sent"),
    )
    scheduler.deliver_backups()
    scheduler.deliver_backups()
    assert len(calls) == 1
    with db.transaction() as session:
        job = session.scalar(select(Job).where(Job.key == "verify-unknown"))
        assert job.state == "review" and job.payload["verification"]["state"] == "unknown"
        report = session.scalar(select(Notification).where(Notification.kind == "backup_report"))
        assert report.account_id == owner and report.audience == "owner"


def test_verified_metadata_is_durable_before_ambiguous_document_send(notification_context, tmp_path):
    verifier_module()
    db, settings, (owner, _, _) = notification_context
    path = tmp_path / "synthetic.dump"
    path.write_bytes(b"synthetic")
    with db.transaction() as session:
        session.add(
            Job(
                key="send-unknown",
                kind="daily_backup",
                account_id=owner,
                payload={"database": db.engine.url.database, "recipient": settings.owner_id},
            )
        )
    observed = []

    def send(*args):
        with db.transaction() as session:
            job = session.scalar(select(Job).where(Job.key == "send-unknown"))
            observed.append(job.payload.get("verification", {}).get("state"))
        raise TimeoutError("ambiguous document send")

    scheduler = Scheduler(
        Commerce(db, local_test=True, public_settings=settings),
        settings,
        dump_database=lambda identifier: path,
        verify_database=lambda *args: {
            "state": "verified",
            "table_count": 59,
            "snapshot_equal": True,
            "relations_equal": True,
        },
        send_document=send,
    )
    scheduler.deliver_backups()
    assert observed == ["verified"]
    with db.transaction() as session:
        job = session.scalar(select(Job).where(Job.key == "send-unknown"))
        assert job.state == "review" and job.payload["verification"]["state"] == "verified"


def test_backup_report_retries_selection_after_owner_role_is_restored(notification_context, tmp_path):
    verifier_module()
    db, settings, (owner, _, _) = notification_context
    configured_owner = settings.owner_id
    path = tmp_path / "synthetic.dump"
    path.write_bytes(b"synthetic")
    with db.transaction() as session:
        session.add(
            Job(
                key="owner-unavailable",
                kind="daily_backup",
                account_id=owner,
                payload={"database": db.engine.url.database, "recipient": settings.owner_id},
            )
        )
    scheduler = Scheduler(
        Commerce(db, local_test=True, public_settings=settings),
        settings,
        dump_database=lambda identifier: path,
        verify_database=lambda *args: {
            "state": "verified",
            "table_count": 59,
            "snapshot_equal": True,
            "relations_equal": True,
        },
        send_document=lambda *args: setattr(settings, "owner_id", None),
    )
    scheduler.deliver_backups()
    settings.owner_id = configured_owner
    scheduler.deliver_backups()
    scheduler.deliver_backups()
    with db.transaction() as session:
        assert (
            len(list(session.scalars(select(Notification).where(Notification.kind == "backup_report")))) == 1
        )


@pytest.mark.parametrize("revoke_during_verification", [False, True])
def test_backup_document_requires_current_verified_owner_before_claim_and_send(
    notification_context, tmp_path, revoke_during_verification
):
    db, settings, (owner, _, _) = notification_context
    captured, sent = [], []
    path = tmp_path / "synthetic.dump"
    path.write_bytes(b"synthetic owned archive")
    with db.transaction() as session:
        session.add(
            Job(
                key="revoked-backup",
                kind="daily_backup",
                account_id=owner,
                payload={"database": db.engine.url.database, "recipient": settings.owner_id},
            )
        )
        if not revoke_during_verification:
            session.get(Account, owner).telegram_id = None

    def verify(*args):
        with db.transaction() as session:
            session.get(Account, owner).telegram_id = None
        return {"state": "verified", "snapshot_equal": True, "relations_equal": True}

    scheduler = Scheduler(
        Commerce(db, local_test=True, public_settings=settings),
        settings,
        dump_database=lambda identifier: captured.append(identifier) or path,
        verify_database=verify,
        send_document=lambda *args: sent.append(args),
    )
    scheduler.deliver_backups()
    scheduler.deliver_backups()
    assert sent == []
    assert len(captured) == int(revoke_during_verification)
    with db.transaction() as session:
        job = session.scalar(select(Job).where(Job.key == "revoked-backup"))
        assert job.state == ("review" if revoke_during_verification else "blocked")


def test_role_with_foreign_database_objects_is_preserved_before_target_drop(
    notification_context, tmp_path, monkeypatch
):
    import psycopg
    from psycopg import sql
    from sunrise_core.errors import DomainError

    module = verifier_module()
    db, settings, _ = notification_context
    verifier = module.BackupVerification(db, settings, storage_dir=tmp_path)
    identifier = str(uuid4())
    path = verifier.capture(identifier)
    foreign = "backup_foreign_" + uuid4().hex
    original_cleanup, observed = verifier._cleanup, []

    def cleanup(target, role, identity, role_oid):
        observed.append((target, role, identity, role_oid))
        with psycopg.connect(
            **verifier.admin_kwargs, dbname=db.engine.url.database, autocommit=True
        ) as admin:
            admin.execute(
                sql.SQL("CREATE SCHEMA {} AUTHORIZATION {}").format(
                    sql.Identifier(foreign), sql.Identifier(role)
                )
            )
        original_cleanup(target, role, identity, role_oid)

    monkeypatch.setattr(verifier, "_cleanup", cleanup)
    try:
        with pytest.raises(DomainError) as caught:
            verifier.verify(path, identifier)
        assert caught.value.code == "backup_cleanup_denied"
        target, role, identity, role_oid = observed[0]
        with psycopg.connect(**verifier.admin_kwargs, dbname="postgres", autocommit=True) as admin:
            assert (
                admin.execute("SELECT oid,datdba FROM pg_database WHERE datname=%s", (target,)).fetchone()
                == identity
            )
    finally:
        if observed:
            target, role, identity, role_oid = observed[0]
            with psycopg.connect(
                **verifier.admin_kwargs, dbname=db.engine.url.database, autocommit=True
            ) as admin:
                assert admin.execute(
                    "SELECT nspowner FROM pg_namespace WHERE nspname=%s", (foreign,)
                ).fetchone() == (role_oid,)
                admin.execute(sql.SQL("DROP SCHEMA {}").format(sql.Identifier(foreign)))
            original_cleanup(target, role, identity, role_oid)


def test_confirmed_incomplete_capture_is_replaced_before_verification(notification_context, tmp_path):
    module = verifier_module()
    db, settings, (owner, _, _) = notification_context
    calls, sent = [], []

    def capture(identifier):
        calls.append(identifier)
        if len(calls) == 1:
            raise module.BackupIncomplete()
        path = tmp_path / (identifier + ".dump")
        path.write_bytes(b"synthetic replacement")
        return path

    with db.transaction() as session:
        session.add(
            Job(
                key="capture-incomplete",
                kind="daily_backup",
                account_id=owner,
                payload={"database": db.engine.url.database, "recipient": settings.owner_id},
            )
        )
    scheduler = Scheduler(
        Commerce(db, local_test=True, public_settings=settings),
        settings,
        dump_database=capture,
        verify_database=lambda *args: {
            "state": "verified",
            "table_count": 59,
            "snapshot_equal": True,
            "relations_equal": True,
        },
        send_document=lambda *args: sent.append(args),
    )
    scheduler.deliver_backups()
    scheduler.deliver_backups()
    assert len(calls) == 2 and len(set(calls)) == 2 and len(sent) == 1


@pytest.mark.parametrize(
    "create,drop",
    [
        ("CREATE TABLE public.foreign_owned_sentinel(value int)", "DROP TABLE public.foreign_owned_sentinel"),
        (
            "CREATE FUNCTION public.foreign_owned_sentinel() RETURNS int LANGUAGE sql AS 'SELECT 1'",
            "DROP FUNCTION public.foreign_owned_sentinel()",
        ),
        (
            "CREATE TYPE public.foreign_owned_sentinel AS ENUM ('sentinel')",
            "DROP TYPE public.foreign_owned_sentinel",
        ),
        (
            "CREATE COLLATION public.foreign_owned_sentinel (provider=libc, locale='C')",
            "DROP COLLATION public.foreign_owned_sentinel",
        ),
    ],
)
def test_cleanup_preserves_unexpected_object_with_foreign_owner(
    notification_context, tmp_path, monkeypatch, create, drop
):
    import psycopg
    from sunrise_core.errors import DomainError

    module = verifier_module()
    db, settings, _ = notification_context
    verifier = module.BackupVerification(db, settings, storage_dir=tmp_path)
    identifier = str(uuid4())
    path = verifier.capture(identifier)
    original_cleanup, observed = verifier._cleanup, []

    def cleanup(target, role, identity, role_oid):
        observed.append((target, role, identity, role_oid))
        with psycopg.connect(**verifier.admin_kwargs, dbname=target, autocommit=True) as admin:
            admin.execute(create)
        original_cleanup(target, role, identity, role_oid)

    monkeypatch.setattr(verifier, "_cleanup", cleanup)
    try:
        with pytest.raises(DomainError) as caught:
            verifier.verify(path, identifier)
        assert caught.value.code == "backup_cleanup_denied"
        target, role, identity, role_oid = observed[0]
        with psycopg.connect(**verifier.admin_kwargs, dbname="postgres", autocommit=True) as admin:
            assert (
                admin.execute("SELECT oid,datdba FROM pg_database WHERE datname=%s", (target,)).fetchone()
                == identity
            )
    finally:
        if observed:
            target, role, identity, role_oid = observed[0]
            with psycopg.connect(**verifier.admin_kwargs, dbname="postgres", autocommit=True) as admin:
                remains = admin.execute(
                    "SELECT oid,datdba FROM pg_database WHERE datname=%s", (target,)
                ).fetchone()
                if remains:
                    assert remains == identity
                    with psycopg.connect(
                        **verifier.admin_kwargs, dbname=target, autocommit=True
                    ) as target_connection:
                        target_connection.execute(drop)
                role_exists = admin.execute("SELECT oid FROM pg_roles WHERE rolname=%s", (role,)).fetchone()
            if role_exists:
                assert role_exists == (role_oid,)
                original_cleanup(target, role, identity, role_oid)


def test_role_owning_another_database_preserves_both_before_cleanup(
    notification_context, tmp_path, monkeypatch
):
    import psycopg
    from psycopg import sql
    from sunrise_core.errors import DomainError

    module = verifier_module()
    db, settings, _ = notification_context
    verifier = module.BackupVerification(db, settings, storage_dir=tmp_path)
    identifier = str(uuid4())
    path = verifier.capture(identifier)
    foreign = "backup_foreign_db_" + uuid4().hex
    original_cleanup, observed = verifier._cleanup, []

    def cleanup(target, role, identity, role_oid):
        with psycopg.connect(**verifier.admin_kwargs, dbname="postgres", autocommit=True) as admin:
            admin.execute(
                sql.SQL("CREATE DATABASE {} OWNER {} TEMPLATE template0").format(
                    sql.Identifier(foreign), sql.Identifier(role)
                )
            )
            foreign_identity = admin.execute(
                "SELECT oid,datdba FROM pg_database WHERE datname=%s", (foreign,)
            ).fetchone()
            admin.execute(
                sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC, sunrise_app").format(
                    sql.Identifier(foreign)
                )
            )
        observed.append((target, role, identity, role_oid, foreign_identity))
        original_cleanup(target, role, identity, role_oid)

    monkeypatch.setattr(verifier, "_cleanup", cleanup)
    try:
        with pytest.raises(DomainError) as caught:
            verifier.verify(path, identifier)
        assert caught.value.code == "backup_cleanup_denied"
        target, role, identity, role_oid, foreign_identity = observed[0]
        with psycopg.connect(**verifier.admin_kwargs, dbname="postgres", autocommit=True) as admin:
            assert (
                admin.execute("SELECT oid,datdba FROM pg_database WHERE datname=%s", (target,)).fetchone()
                == identity
            )
            assert (
                admin.execute("SELECT oid,datdba FROM pg_database WHERE datname=%s", (foreign,)).fetchone()
                == foreign_identity
            )
    finally:
        if observed:
            target, role, identity, role_oid, foreign_identity = observed[0]
            with psycopg.connect(**verifier.admin_kwargs, dbname="postgres", autocommit=True) as admin:
                assert (
                    admin.execute(
                        "SELECT oid,datdba FROM pg_database WHERE datname=%s", (foreign,)
                    ).fetchone()
                    == foreign_identity
                )
                admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(foreign)))
            original_cleanup(target, role, identity, role_oid)


def test_shared_tablespace_owner_preserves_resources_before_cleanup(
    notification_context, tmp_path, monkeypatch
):
    import psycopg
    from psycopg import sql
    from sunrise_core.errors import DomainError

    module = verifier_module()
    db, settings, _ = notification_context
    verifier = module.BackupVerification(db, settings, storage_dir=tmp_path)
    identifier = str(uuid4())
    path = verifier.capture(identifier)
    space_name = "backup_foreign_space_" + uuid4().hex
    location = tmp_path / "owned_tablespace_location"
    location.mkdir()
    original_cleanup, observed = verifier._cleanup, []

    def cleanup(target, role, identity, role_oid):
        with psycopg.connect(**verifier.admin_kwargs, dbname="postgres", autocommit=True) as admin:
            admin.execute(
                sql.SQL("CREATE TABLESPACE {} OWNER {} LOCATION {}").format(
                    sql.Identifier(space_name), sql.Identifier(role), sql.Literal(str(location))
                )
            )
            space_identity = admin.execute(
                "SELECT oid,spcowner FROM pg_tablespace WHERE spcname=%s", (space_name,)
            ).fetchone()
        observed.append((target, role, identity, role_oid, space_identity))
        original_cleanup(target, role, identity, role_oid)

    monkeypatch.setattr(verifier, "_cleanup", cleanup)
    try:
        with pytest.raises(DomainError) as caught:
            verifier.verify(path, identifier)
        assert caught.value.code == "backup_cleanup_denied"
        target, role, identity, role_oid, space_identity = observed[0]
        with psycopg.connect(**verifier.admin_kwargs, dbname="postgres", autocommit=True) as admin:
            assert (
                admin.execute("SELECT oid,datdba FROM pg_database WHERE datname=%s", (target,)).fetchone()
                == identity
            )
            assert (
                admin.execute(
                    "SELECT oid,spcowner FROM pg_tablespace WHERE spcname=%s", (space_name,)
                ).fetchone()
                == space_identity
            )
    finally:
        if observed:
            target, role, identity, role_oid, space_identity = observed[0]
            with psycopg.connect(**verifier.admin_kwargs, dbname="postgres", autocommit=True) as admin:
                assert (
                    admin.execute(
                        "SELECT oid,spcowner FROM pg_tablespace WHERE spcname=%s", (space_name,)
                    ).fetchone()
                    == space_identity
                )
                admin.execute(sql.SQL("DROP TABLESPACE {}").format(sql.Identifier(space_name)))
            original_cleanup(target, role, identity, role_oid)
