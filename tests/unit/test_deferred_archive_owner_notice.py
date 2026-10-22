"""Отложенное уведомление по сохранённому архиву; pure-проверки не используют SQL/панель."""

import copy
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal
from threading import Barrier, Event
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.sql import operators
from sunrise_core.errors import DomainError
from sunrise_infrastructure import panel_entitlements, panel_events, subscription_archive
from sunrise_infrastructure.db import (
    Account,
    Alias,
    Job,
    Notification,
    NotificationDelivery,
    Product,
    Setting,
    StaffRole,
    Subscription,
    now,
)
from sunrise_infrastructure.notifications import NotificationService
from sunrise_infrastructure.panel_v3 import production_binding_key
from sunrise_worker.main import Worker

from tests.unit.test_production_panel_news import RowSession


class ArchiveRows(RowSession):
    def __init__(self):
        super().__init__()
        self.scans = []
        self.account_locks = []
        self.busy_accounts = set()

    def get(self, model, key, **kwargs):
        if model is Account and kwargs.get("with_for_update"):
            self.account_locks.append(key)
            if key in self.busy_accounts:
                cause = Exception("synthetic lock busy")
                cause.sqlstate = "55P03"
                raise OperationalError("synthetic", {}, cause)
        return super().get(model, key, **kwargs)

    def execute(self, statement, parameters=None):
        if not hasattr(statement, "column_descriptions"):
            return super().execute(statement, parameters)
        assert [item["name"] for item in statement.column_descriptions] == ["key", "id", "account_id"]
        cursor = next(
            (
                condition.right.value
                for condition in statement._where_criteria
                if getattr(condition, "operator", None) is operators.gt
            ),
            None,
        )
        limit = statement._limit_clause.value
        assert 1 <= limit <= 50
        rows = []
        for (kind, key), row in sorted(self.rows.items(), key=lambda item: str(item[0][1])):
            if kind is not Setting or not key.startswith("archive:") or cursor and key <= cursor:
                continue
            if isinstance(row.value, dict) and row.value.get("owner_intent_id") is not None:
                continue
            sub = self.get(Subscription, key.removeprefix("archive:"))
            if sub:
                rows.append(SimpleNamespace(key=key, id=sub.id, account_id=sub.account_id))
        self.scans.append((cursor, limit))
        return SimpleNamespace(all=lambda: rows[:limit])


class SyntheticDatabase:
    def __init__(self):
        self.session = ArchiveRows()

    @contextmanager
    def transaction(self):
        before = copy.deepcopy(self.session.rows)
        try:
            yield self.session
        except BaseException:
            self.session.rows = before
            raise


class CurrentPolicy:
    production = True

    def __init__(self):
        self.allowed = True
        self.scope = SimpleNamespace(
            scope_id="synthetic-project",
            panel=SimpleNamespace(origin="https://panel.example.test", contract="3.4_id"),
        )

    def require(self, capability=None):
        if not self.allowed:
            raise DomainError("production_stage_denied", "Этап не разрешён.", 409)

    def require_current(self, session, capability=None):
        self.require(capability)


def seed_archive_inputs(session, sid="owned-sub", *, owner_verified=False):
    customer = Account(
        id=301, telegram_id=930000000011, email_verified=False, telegram_notifications_blocked=False
    )
    owner = Account(
        id=901,
        email="owner@example.test",
        email_verified=owner_verified,
        telegram_id=None,
        telegram_notifications_blocked=False,
    )
    for row in (
        customer,
        owner,
        StaffRole(account_id=owner.id, role="owner"),
        Product(
            id="synthetic-plan",
            code="synthetic-plan",
            name="Сохранённый тариф",
            available=True,
            price=Decimal("1.00"),
            currency="RUB",
            tier_level=1,
            included_devices=4,
        ),
    ):
        if session.get(type(row), row.account_id if isinstance(row, StaffRole) else row.id) is None:
            session.add(row)
    sub = Subscription(
        id=sid,
        account_id=customer.id,
        product_id="synthetic-plan",
        name="Архивная подписка",
        environment="live",
        version=1,
        devices_limit=4,
        panel_user_id=42,
        panel_uuid="22222222-2222-4222-8222-222222222222",
        short_uuid="synthetic-short",
        connection_url="https://sub.example.test/synthetic-secret-token",
    )
    binding = {
        "scope_id": "synthetic-project",
        "generation": 1,
        "origin_nonce": "a" * 32,
        "user_id": "42",
        "uuid": sub.panel_uuid,
        "short_uuid": sub.short_uuid,
        "username": "synthetic-preserved-user",
    }
    proof = {
        "origin": "new_project_identity",
        "environment": "live",
        "scope_id": "synthetic-project",
        "generation": 1,
        "subscription_id": sid,
        "account_id": str(customer.id),
        "binding": binding,
        "contract": "3.4_id",
        "panel_host": "panel.example.test",
        "connection_url": sub.connection_url,
    }
    session.add(sub)
    session.add(Setting(key="production_admission", value={"scope_id": "synthetic-project"}))
    session.add(Setting(key=production_binding_key(sid), value=proof))
    session.add(
        Job(
            id="delete-" + sid,
            key="delete-" + sid,
            kind="subscription_cleanup",
            account_id=customer.id,
            state="done",
            attempts=1,
            payload={"phase": "archived", "attempted_at": now().isoformat()},
        )
    )
    return sub, binding, proof


@pytest.fixture
def archived(monkeypatch):
    db, policy = SyntheticDatabase(), CurrentPolicy()
    settings = SimpleNamespace(
        runtime_mode="production",
        local_test=False,
        owner_id=None,
        admin_telegram_allowlist=["930000000001"],
        admin_email_allowlist=["owner@example.test"],
        email_allowlist=[],
        _runtime_policy=policy,
    )
    sub, binding, proof = seed_archive_inputs(db.session)
    panel = SimpleNamespace(contract="3.4_id")
    commerce = SimpleNamespace(db=db, public_settings=settings, panel_provider=panel, runtime_policy=policy)
    # Initial archival ownership has separate acceptance tests. Deferred state_in is never mocked.
    monkeypatch.setattr(panel_events.PanelEvents, "_owned", lambda self, session, row: (binding, proof))
    monkeypatch.setattr(panel_entitlements, "paid_horizon", lambda *args: None)
    monkeypatch.setattr(panel_entitlements, "remaining_segments", lambda *args: [])
    with db.transaction() as session:
        state = subscription_archive.archive_absence_in(
            session, commerce, sub, binding, reason="expiry_cleanup", proof_id="delete-" + sub.id
        )
    assert state.owner_intent_id is None
    assert subscription_archive.state_in(db.session, sub).owner_intent_id is None
    notices = NotificationService(db, settings)
    return SimpleNamespace(
        db=db, policy=policy, settings=settings, commerce=commerce, notices=notices, sid=sub.id
    )


def rows(item, kind):
    return [row for (model, _), row in item.db.session.rows.items() if model is kind]


def verify_owner(item):
    item.db.session.get(Account, 901).telegram_id = 930000000001


def dispatcher(item):
    assert hasattr(subscription_archive, "DeferredArchiveOwnerNotices"), (
        "deferred archive dispatcher is missing"
    )
    return subscription_archive.DeferredArchiveOwnerNotices(item.commerce, item.notices)


def test_actual_worker_backfills_archived_notice_after_late_verified_owner(archived):
    item = archived
    verify_owner(item)
    worker = Worker.__new__(Worker)
    lock = SimpleNamespace(acquire=lambda **kwargs: True, release=lambda: None)
    worker.runtime_policy, worker.redis = item.policy, SimpleNamespace(lock=lambda *a, **kw: lock)
    worker.commerce = SimpleNamespace(
        **vars(item.commerce),
        run_jobs=lambda: 0,
        retention=SimpleNamespace(schedule_due=lambda: 0, run_jobs=lambda: 0),
    )
    worker.commerce.panel_provider = None  # This notice-only worker fixture has no panel transport.
    worker.broadcasts = SimpleNamespace(expand_pending=lambda: 0, report_completed=lambda: 0)
    worker.scheduler = SimpleNamespace(notify_ready=lambda: None, remind=lambda: None)
    worker.operational_monitor = SimpleNamespace(notify_owner=lambda: None)
    worker.panel_events = SimpleNamespace(reconcile_pending=lambda: 0)
    worker.engagement = SimpleNamespace(run_due=lambda: 0)
    worker.notifications = SimpleNamespace(deliver_pending=lambda: 0)
    worker.panel_contacts = SimpleNamespace(run_pending=lambda: 0)
    worker.provider, worker._next_reminders = None, now() - timedelta(seconds=1)
    cls = getattr(subscription_archive, "DeferredArchiveOwnerNotices", None)
    worker.archive_owner_notices = cls(item.commerce, item.notices) if cls else None
    worker.tick()
    state = subscription_archive.state_in(item.db.session, item.db.session.get(Subscription, item.sid))
    assert state.owner_intent_id is not None
    owner_notices = [row for row in rows(item, Notification) if row.kind == "subscription_deleted_owner"]
    assert len(owner_notices) == 1 and owner_notices[0].account_id == 901
    assert len([row for row in rows(item, Notification) if row.kind == "subscription_deleted"]) == 1


def test_deferred_notice_is_once_only_without_panel_or_extra_customer_alert(archived):
    item = archived
    before = [row.id for row in rows(item, Notification)]
    service = dispatcher(item)
    assert service.run_pending() == 0
    verify_owner(item)
    assert service.run_pending() == 1
    assert service.run_pending() == 0
    notices = [row for row in rows(item, Notification) if row.id not in before]
    assert len(notices) == 1 and notices[0].audience == "owner"
    assert "synthetic-secret-token" not in notices[0].body and "https://" not in notices[0].body
    assert "22222222-2222" not in notices[0].body
    deliveries = [row for row in rows(item, NotificationDelivery) if row.notification_id == notices[0].id]
    assert [(row.channel, row.recipient) for row in deliveries] == [("telegram", "930000000001")]
    assert item.db.session.account_locks[-2:] == [301, 901]


@pytest.mark.parametrize(
    "denial",
    [
        "gate",
        "scope",
        "generation",
        "panel_origin",
        "proof",
        "version",
        "unbound_telegram",
        "unallowlisted",
        "role",
        "alias",
        "ambiguous",
    ],
)
def test_deferred_notice_denies_stale_scope_proof_or_unverified_owner(archived, denial):
    item = archived
    verify_owner(item)
    if denial == "gate":
        item.policy.allowed = False
    elif denial == "scope":
        item.policy.scope.scope_id = "foreign-project"
    elif denial == "panel_origin":
        item.policy.scope.panel.origin = "https://foreign.example.test"
    elif denial == "generation":
        item.db.session.get(Setting, production_binding_key(item.sid)).value["generation"] = 2
    elif denial == "proof":
        item.db.session.get(Job, "delete-" + item.sid).state = "pending"
    elif denial == "version":
        item.db.session.get(Subscription, item.sid).version = 2
    elif denial == "unbound_telegram":
        item.db.session.get(Account, 901).telegram_id = None
    elif denial == "unallowlisted":
        item.settings.admin_telegram_allowlist = []
    elif denial == "role":
        item.db.session.get(StaffRole, 901).role = "admin"
    elif denial == "alias":
        item.db.session.add(Alias(old_id=901, canonical_id=999))
    else:
        item.db.session.add(
            Account(id=902, email="second@example.test", email_verified=True, telegram_id=930000000002)
        )
        item.db.session.add(StaffRole(account_id=902, role="owner"))
        item.settings.admin_telegram_allowlist.append("930000000002")
    assert dispatcher(item).run_pending() == 0
    assert item.db.session.get(Setting, "archive:" + item.sid).value["owner_intent_id"] is None
    assert not any(row.kind == "subscription_deleted_owner" for row in rows(item, Notification))


def test_malformed_front_page_cannot_starve_later_valid_archive(archived):
    item = archived
    verify_owner(item)
    current = item.db.session.get(Setting, "archive:" + item.sid).value
    for index in range(51):
        sid = "aaa-invalid-" + str(index).zfill(3)
        item.db.session.add(Subscription(id=sid, account_id=301, environment="live"))
        item.db.session.add(
            Setting(key="archive:" + sid, value=current | {"version": 9, "subscription_id": sid})
        )
    service = dispatcher(item)
    assert service.run_pending(limit=500) == 0
    assert service.run_pending(limit=500) == 1
    assert [limit for _, limit in item.db.session.scans] == [50, 50]


def test_busy_customer_lock_defers_and_cursor_wrap_retries(archived):
    item = archived
    verify_owner(item)
    item.db.session.busy_accounts.add(301)
    service = dispatcher(item)
    assert service.run_pending() == 0
    item.db.session.busy_accounts.clear()
    assert service.run_pending() == 0
    assert service.run_pending() == 1


def test_notice_creation_and_archive_id_roll_back_together_then_retry_once(archived, monkeypatch):
    item = archived
    verify_owner(item)
    emit = item.notices.emit_in

    def fail_after_emit(*args, **kwargs):
        result = emit(*args, **kwargs)
        if result.kind == "subscription_deleted_owner":
            raise DomainError("synthetic_rollback", "Синтетический откат.", 409)
        return result

    monkeypatch.setattr(item.notices, "emit_in", fail_after_emit)
    assert dispatcher(item).run_pending() == 0
    assert not any(row.kind == "subscription_deleted_owner" for row in rows(item, Notification))
    assert item.db.session.get(Setting, "archive:" + item.sid).value["owner_intent_id"] is None
    monkeypatch.setattr(item.notices, "emit_in", emit)
    assert dispatcher(item).run_pending() == 1
    assert dispatcher(item).run_pending() == 0


def test_user_supplied_archive_labels_cannot_leak_usable_links_in_owner_notice(archived):
    item = archived
    verify_owner(item)
    row = item.db.session.get(Setting, "archive:" + item.sid)
    row.value = row.value | {
        "subscription_name": "VPN https://sub.example.test/private-token",
        "tariff_name": "Тариф vless://synthetic-secret@node.example.test",
    }
    assert dispatcher(item).run_pending() == 1
    body = next(row.body for row in rows(item, Notification) if row.kind == "subscription_deleted_owner")
    assert "://" not in body and "private-token" not in body and "synthetic-secret" not in body


@pytest.mark.parametrize("evidence_present", [False, True])
def test_nullable_imported_capacity_requires_preserved_evidence_and_safe_copy(archived, evidence_present):
    item = archived
    verify_owner(item)
    item.db.session.get(Subscription, item.sid).devices_limit = None
    archive = item.db.session.get(Setting, "archive:" + item.sid)
    archive.value = archive.value | {"device_limit": None}
    registration = item.db.session.get(Setting, production_binding_key(item.sid))
    registration.value = registration.value | {"origin": "imported_project_identity"}
    if evidence_present:
        registration.value["capacity_evidence"] = {
            "kind": "explicit_null_preserved",
            "present": True,
            "source": "authenticated_panel_snapshot",
            "until": now().isoformat(),
        }
    assert dispatcher(item).run_pending() == (1 if evidence_present else 0)
    notices = [row for row in rows(item, Notification) if row.kind == "subscription_deleted_owner"]
    if evidence_present:
        assert "Лимит не задан в панели" in notices[0].body
    else:
        assert notices == []


def test_owner_hint_is_not_authority_after_current_telegram_revocation(archived, monkeypatch):
    item = archived
    verify_owner(item)
    original_lookup = subscription_archive.owner_account_in
    calls = 0

    def revoked_after_hint(session, settings):
        nonlocal calls
        calls += 1
        if calls == 2:
            session.get(Account, 901).telegram_id = None
        return original_lookup(session, settings)

    monkeypatch.setattr(subscription_archive, "owner_account_in", revoked_after_hint)
    assert dispatcher(item).run_pending() == 0
    assert not any(row.kind == "subscription_deleted_owner" for row in rows(item, Notification))


def test_unexpected_sql_failure_is_not_swallowed_as_nowait_busy(archived, monkeypatch):
    item = archived
    verify_owner(item)
    original_get = item.db.session.get

    def unavailable_connection(model, key, **kwargs):
        if model is Account and key == 301 and kwargs.get("with_for_update"):
            cause = Exception("synthetic unavailable SQL connection")
            cause.sqlstate = "08006"
            raise OperationalError("synthetic", {}, cause)
        return original_get(model, key, **kwargs)

    monkeypatch.setattr(item.db.session, "get", unavailable_connection)
    with pytest.raises(OperationalError):
        dispatcher(item).run_pending()


@pytest.fixture
def pg_archived(monkeypatch):
    from tests.isolated_database import isolated_database

    with isolated_database("deferred_owner") as original_db:

        class ScopedDatabase:
            @contextmanager
            def transaction(self):
                with original_db.transaction() as session:
                    session.execute(text("SET LOCAL statement_timeout = '5s'"))
                    yield session

        db, policy = ScopedDatabase(), CurrentPolicy()
        settings = SimpleNamespace(
            runtime_mode="production",
            local_test=False,
            owner_id=None,
            admin_telegram_allowlist=["930000000001"],
            admin_email_allowlist=["owner@example.test"],
            email_allowlist=[],
            _runtime_policy=policy,
        )
        commerce = SimpleNamespace(
            db=db,
            public_settings=settings,
            runtime_policy=policy,
            panel_provider=SimpleNamespace(contract="3.4_id"),
        )
        with db.transaction() as session:
            sub, binding, proof = seed_archive_inputs(session)
            session.flush()
            monkeypatch.setattr(panel_events.PanelEvents, "_owned", lambda *args: (binding, proof))
            state = subscription_archive.archive_absence_in(
                session, commerce, sub, binding, reason="expiry_cleanup", proof_id="delete-" + sub.id
            )
            assert state.owner_intent_id is None
        yield SimpleNamespace(
            db=db,
            commerce=commerce,
            settings=settings,
            notices=NotificationService(db, settings),
            sid="owned-sub",
        )


@pytest.mark.postgres
def test_pg_late_verified_owner_retries_after_atomic_notice_rollback(pg_archived, monkeypatch):
    item = pg_archived
    service = subscription_archive.DeferredArchiveOwnerNotices(item.commerce, item.notices)
    assert service.run_pending() == 0
    with item.db.transaction() as session:
        assert session.get(Account, 901).telegram_id is None
        session.get(Account, 901).telegram_id = 930000000001
    emit = item.notices.emit_in

    def fail_after_emit(*args, **kwargs):
        result = emit(*args, **kwargs)
        if result.kind == "subscription_deleted_owner":
            raise DomainError("synthetic_rollback", "Синтетический откат.", 409)
        return result

    monkeypatch.setattr(item.notices, "emit_in", fail_after_emit)
    assert service.run_pending() == 0
    with item.db.transaction() as session:
        assert (
            session.scalar(select(Notification.id).where(Notification.kind == "subscription_deleted_owner"))
            is None
        )
        assert session.get(Setting, "archive:" + item.sid).value["owner_intent_id"] is None
    monkeypatch.setattr(item.notices, "emit_in", emit)
    assert service.run_pending() == 0  # cursor wraps after the rolled-back last candidate
    assert service.run_pending() == 1
    assert service.run_pending() == 0
    with item.db.transaction() as session:
        notices = list(
            session.scalars(select(Notification).where(Notification.kind == "subscription_deleted_owner"))
        )
        assert len(notices) == 1 and notices[0].account_id == 901
        assert session.get(Setting, "archive:" + item.sid).value["owner_intent_id"] == notices[0].id
        assert (
            len(
                list(
                    session.scalars(
                        select(Notification.id).where(Notification.kind == "subscription_deleted")
                    )
                )
            )
            == 1
        )
        assert (
            session.get(Subscription, item.sid).connection_url
            == "https://sub.example.test/synthetic-secret-token"
        )


@pytest.mark.postgres
def test_pg_concurrent_dispatchers_create_exactly_one_owner_intent(pg_archived, monkeypatch):
    item = pg_archived
    with item.db.transaction() as session:
        session.get(Account, 901).telegram_id = 930000000001
    services = [
        subscription_archive.DeferredArchiveOwnerNotices(item.commerce, item.notices) for _ in range(2)
    ]
    ready, barrier = Event(), Barrier(2)
    for service in services:
        original_record = service._record

        def simultaneous_record(candidate, hint, original_record=original_record):
            ready.set()  # snapshot transaction has released its temporary owner lock
            barrier.wait(timeout=10)
            return original_record(candidate, hint)

        monkeypatch.setattr(service, "_record", simultaneous_record)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(services[0].run_pending)
        assert ready.wait(timeout=10)
        second = pool.submit(services[1].run_pending)
        assert sorted([first.result(timeout=15), second.result(timeout=15)]) == [0, 1]
    with item.db.transaction() as session:
        notices = list(
            session.scalars(select(Notification).where(Notification.kind == "subscription_deleted_owner"))
        )
        assert len(notices) == 1
        assert session.get(Setting, "archive:" + item.sid).value["owner_intent_id"] == notices[0].id
        deliveries = list(
            session.scalars(
                select(NotificationDelivery).where(NotificationDelivery.notification_id == notices[0].id)
            )
        )
        assert [(row.channel, row.recipient) for row in deliveries] == [("telegram", "930000000001")]


def test_expiry_archive_emits_explicit_seven_day_customer_notice(archived):
    notice = next(row for row in rows(archived, Notification) if row.account_id == 301)
    assert notice.body == "Ваша подписка удалена, поскольку после окончания её срока действия прошло 7 дней."
