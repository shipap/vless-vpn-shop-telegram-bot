"""Explicit offline first-owner setup uses only new synthetic schemas."""

import importlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sunrise_core.errors import DomainError
from sunrise_infrastructure.db import Account, Alias, Audit, Setting, StaffRole
from sunrise_infrastructure.staff_permissions import StaffPermissions

from tests.isolated_database import isolated_database


def implementation():
    try:
        return importlib.import_module("scripts.bootstrap_local_owner")
    except ModuleNotFoundError:
        pytest.fail("Explicit offline first-owner setup is missing")


@pytest.fixture
def owner_database():
    with isolated_database("demo_instance") as db:
        schema = db.engine.url.query["options"].removeprefix("-csearch_path=")
        with db.transaction() as session:
            session.add(Setting(key="demo_instance", value={"schema": schema, "prepared": True}))
            session.add_all(
                [
                    Account(
                        id=9007199254740997, telegram_id=101, email="owner@example.test", email_verified=True
                    ),
                    Account(
                        id=9007199254740999, telegram_id=102, email="second@example.test", email_verified=True
                    ),
                ]
            )
        config = SimpleNamespace(
            local_test=True,
            database_url=db.engine.url.render_as_string(hide_password=False),
            admin_email_allowlist=["owner@example.test", "second@example.test"],
            admin_telegram_allowlist=["101", "102"],
            owner_id=None,
        )
        yield db, config


def grant(module, db, config, aid="9007199254740997", email="owner@example.test", **kwargs):
    return module.grant_first_owner(
        db,
        config,
        account_id=aid,
        email=email,
        confirm_email=email,
        confirmed=True,
        reason="Явная первоначальная настройка синтетического владельца",
        **kwargs,
    )


def test_offline_first_owner_enables_owner_capabilities_and_writes_one_atomic_audit(owner_database):
    db, config = owner_database
    module = implementation()
    permissions = StaffPermissions(SimpleNamespace(db=db, settings=config))
    assert "catalog_write" not in permissions.describe("9007199254740997")["capabilities"]
    assert grant(module, db, config) == {"status": "granted", "account_id": "9007199254740997"}
    assert "catalog_write" in permissions.describe("9007199254740997")["capabilities"]
    assert "catalog_write" not in permissions.describe("9007199254740999")["capabilities"]
    assert grant(module, db, config)["status"] == "already_owner"
    with db.transaction() as session:
        audits = list(session.scalars(select(Audit).where(Audit.action == "local_owner_bootstrap")))
        assert len(audits) == 1
        assert audits[0].actor_id == 9007199254740997 and audits[0].details["before"] is None
        assert audits[0].details["after"] == {"role": "owner", "account_id": "9007199254740997"}
        assert session.get(Account, 9007199254740997).telegram_id == 101


@pytest.mark.parametrize(
    "mode",
    [
        "unverified",
        "email_only",
        "not_allowlisted",
        "wrong_email",
        "alias",
        "other_owner",
        "existing_admin",
        "configured_other_owner",
        "no_confirmation",
        "missing_marker",
    ],
)
def test_offline_first_owner_refuses_invalid_identity_or_existing_grants_without_writes(owner_database, mode):
    db, config = owner_database
    aid, email = "9007199254740997", "owner@example.test"
    with db.transaction() as session:
        if mode == "unverified":
            session.get(Account, int(aid)).email_verified = False
        if mode == "email_only":
            session.get(Account, int(aid)).telegram_id = None
        if mode == "alias":
            session.add(Alias(old_id=51, canonical_id=int(aid)))
            aid = "51"
        if mode == "other_owner":
            session.add(StaffRole(account_id=9007199254740999, role="owner"))
        if mode == "existing_admin":
            session.add(StaffRole(account_id=int(aid), role="admin"))
        if mode == "missing_marker":
            session.delete(session.get(Setting, "demo_instance"))
    if mode == "not_allowlisted":
        config.admin_telegram_allowlist = []
    if mode == "wrong_email":
        email = "second@example.test"
    if mode == "configured_other_owner":
        config.owner_id = "103"
    module = implementation()
    with pytest.raises(DomainError):
        module.grant_first_owner(
            db,
            config,
            account_id=aid,
            email=email,
            confirm_email=email,
            confirmed=mode != "no_confirmation",
            reason="Синтетический отказ первоначального назначения",
        )
    with db.transaction() as session:
        assert not list(session.scalars(select(Audit).where(Audit.action == "local_owner_bootstrap")))
        row = session.get(StaffRole, 9007199254740997)
        assert row is None or mode == "existing_admin" and row.role == "admin"


def test_concurrent_different_first_owners_cannot_both_receive_the_role(owner_database):
    db, config = owner_database
    module, barrier = implementation(), Barrier(2)

    def attempt(aid, email):
        barrier.wait(timeout=5)
        try:
            return grant(module, db, config, aid, email)["status"]
        except DomainError:
            return "denied"

    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(attempt, "9007199254740997", "owner@example.test")
        two = pool.submit(attempt, "9007199254740999", "second@example.test")
        assert sorted([one.result(timeout=10), two.result(timeout=10)]) == ["denied", "granted"]
    with db.transaction() as session:
        assert len(list(session.scalars(select(StaffRole).where(StaffRole.role == "owner")))) == 1
        assert len(list(session.scalars(select(Audit).where(Audit.action == "local_owner_bootstrap")))) == 1


def test_matching_configured_telegram_owner_preserves_existing_identity(owner_database):
    db, config = owner_database
    with db.transaction() as session:
        session.get(Account, 9007199254740997).telegram_id = 101
    config.owner_id = "101"
    assert grant(implementation(), db, config)["status"] == "granted"
    with db.transaction() as session:
        assert session.get(Account, 9007199254740997).telegram_id == 101


def test_owner_role_and_audit_roll_back_together_after_sql_flush(owner_database):
    from sqlalchemy import event

    db, config = owner_database

    def deny_after_flush(session, context):
        raise RuntimeError("synthetic audit persistence failure")

    event.listen(db.sessions, "after_flush", deny_after_flush)
    try:
        with pytest.raises(RuntimeError, match="synthetic audit persistence failure"):
            grant(implementation(), db, config)
    finally:
        event.remove(db.sessions, "after_flush", deny_after_flush)
    with db.transaction() as session:
        assert not session.get(StaffRole, 9007199254740997)
        assert not list(session.scalars(select(Audit).where(Audit.action == "local_owner_bootstrap")))


def test_actual_cli_success_uses_confirmed_canonical_id_and_keeps_other_admin_ordinary(
    owner_database, monkeypatch, capsys
):
    db, config = owner_database
    module = implementation()
    monkeypatch.setattr(module, "load_settings", lambda profile: config)
    assert (
        module.main(
            [
                "--profile",
                "demo",
                "--account-id",
                "9007199254740997",
                "--email",
                "owner@example.test",
                "--confirm-email",
                "owner@example.test",
                "--confirm-first-owner",
                "--reason",
                "Явная локальная настройка владельца",
            ]
        )
        == 0
    )
    assert "назначен" in capsys.readouterr().out
    with db.transaction() as session:
        assert session.get(StaffRole, 9007199254740997).role == "owner"
        assert session.get(StaffRole, 9007199254740999) is None


@pytest.mark.parametrize("mode", ["allowed", "revoked", "email_only", "unverified"])
def test_local_bootstrap_uses_current_telegram_policy_without_native_io(mode):
    module = implementation()
    account = Account(id=9007199254740997, telegram_id=101, email="owner@example.test", email_verified=True)
    if mode == "email_only":
        account.telegram_id = None
    if mode == "unverified":
        account.email_verified = False
    original_telegram_id = account.telegram_id
    added = []

    class BoundarySession:
        def execute(self, statement):
            value = "public" if str(statement) == "SELECT current_schema()" else "sunrise_test"
            return SimpleNamespace(scalar=lambda: value)

        def scalar(self, statement):
            return account if statement.column_descriptions[0]["entity"] is Account else None

        def scalars(self, statement):
            return []

        def get(self, model, identifier):
            return None

        def add(self, value):
            added.append(value)

    @contextmanager
    def transaction():
        yield BoundarySession()

    target = "postgresql+psycopg://synthetic@127.0.0.1:55439/sunrise_test"
    db = SimpleNamespace(engine=SimpleNamespace(url=make_url(target)), transaction=transaction)
    config = SimpleNamespace(
        local_test=True,
        database_url=target,
        owner_id=None,
        admin_telegram_allowlist=[] if mode == "revoked" else ["101"],
    )
    if mode == "allowed":
        assert grant(module, db, config)["status"] == "granted"
        assert len([row for row in added if isinstance(row, StaffRole)]) == 1
        assert len([row for row in added if isinstance(row, Audit)]) == 1
    else:
        with pytest.raises(DomainError) as denied:
            grant(module, db, config)
        assert denied.value.code == "owner_identity_denied"
        assert added == []
    assert account.telegram_id == original_telegram_id
