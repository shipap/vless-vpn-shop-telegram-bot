"""Verified own absence hides operations while money, paid periods and identity survive."""

import pytest
from sqlalchemy import event, func, select
from sunrise_infrastructure.db import Job, Notification, Payment, Segment, Setting, Subscription, now
from sunrise_infrastructure.notifications import NotificationService
from sunrise_infrastructure.panel_events import PanelEvents

from tests.integration.test_panel_28_workflows import panel28_workflow as panel28_workflow
from tests.integration.test_panel_entitlement_freshness import entitlement as entitlement
from tests.integration.test_subscription_engagement import paid_creation as paid_creation
from tests.integration.test_subscription_retention import due_retention as due_retention
from tests.integration.test_v11_panel_events import TG, signed_event
from tests.integration.test_v11_panel_events import owned_panel as owned_panel


@pytest.fixture
def owner_first_database(monkeypatch):
    from contextlib import contextmanager

    from sunrise_infrastructure.db import Account, StaffRole

    from tests.isolated_database import isolated_database

    prepared = {}

    @contextmanager
    def seed_owner_first(prefix):
        with isolated_database(prefix) as db:
            with db.transaction() as session:
                owner = Account(email="merge-owner@example.test", email_verified=True)
                session.add(owner)
                session.flush()
                session.add(StaffRole(account_id=owner.id, role="owner"))
                prepared["id"] = owner.id
            yield db

    monkeypatch.setattr("tests.integration.test_v11_panel_events.isolated_database", seed_owner_first)
    return prepared


@pytest.fixture
def owner_first_paid_creation(owner_first_database, paid_creation):
    from tests.legal_fixture import accept_test_legal

    _, _, commerce, config, _, _, customer_id, *_ = paid_creation
    owner_id = owner_first_database["id"]
    assert owner_id < customer_id
    config.owner_id = TG
    config.admin_telegram_allowlist = ["70000000000000001"]
    config.admin_email_allowlist = ["merge-owner@example.test"]
    config.email_allowlist = ["merge-owner@example.test"]
    accept_test_legal(commerce, owner_id)
    return paid_creation, owner_id


def test_manual_archive_owner_lock_never_deadlocks_actual_canonical_merge(owner_first_paid_creation):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, current_thread
    from time import monotonic

    from sqlalchemy import text
    from sunrise_infrastructure.db import Account, Alias, NotificationDelivery, PanelEvent

    from tests.integration.test_v11_panel_events import TG

    context, owner_id = owner_first_paid_creation
    db, _, commerce, config, state, _, customer_id, sid, _ = context
    commerce.provisioning.run_jobs()
    commerce.dashboard(customer_id, refresh_panel=True)
    proof = commerce.accounts.request_link(owner_id, TG, now())["proof"]
    events = PanelEvents(db, commerce.panel_provider, config)
    assert events.accept(*signed_event("user.deleted"))["status"] == "accepted"
    state["user"] = None
    with db.transaction() as session:
        periods = [
            (item.id, item.starts_at, item.ends_at, item.paid_value)
            for item in session.scalars(select(Segment))
        ]
        payments = list(session.scalars(select(Payment.id)))
        identity = session.get(Subscription, sid).panel_user_id
    customer_locked, owner_locked, archive_completed = Event(), Event(), Event()
    race_identity = {}

    def archive_gate(connection, cursor, statement, parameters, context, executemany):
        if (
            current_thread().name.startswith("archive-merge-race")
            and "FROM v5_accounts" in statement
            and "FOR UPDATE" in statement
            and not customer_locked.is_set()
        ):
            connection.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
            race_identity["archive_pid"] = connection.scalar(select(func.pg_backend_pid()))
            customer_locked.set()
            assert owner_locked.wait(5), "Actual merge did not acquire its first owner account lock"

    def merge_gate(connection, cursor, statement, parameters, context, executemany):
        if (
            current_thread().name.startswith("canonical-merge-race")
            and "FROM v5_accounts" in statement
            and "ORDER BY" in statement
            and "FOR UPDATE" in statement
            and not owner_locked.is_set()
        ):
            connection.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
            connection.execute(select(Account.id).where(Account.id == owner_id).with_for_update())
            owner_locked.set()
            deadline = monotonic() + 5
            while not archive_completed.is_set():
                connection.execute(text("SELECT pg_stat_clear_snapshot()"))
                waiting = connection.scalar(
                    text("SELECT wait_event_type FROM pg_stat_activity WHERE pid=:pid"),
                    {"pid": race_identity["archive_pid"]},
                )
                if waiting == "Lock":
                    break
                assert monotonic() < deadline, "Archive did not wait for the owner or return a bounded retry"
                archive_completed.wait(0.01)

    def archive():
        try:
            return events.reconcile_pending()
        except Exception as error:  # noqa: BLE001 - capture bounded race evidence without private exception text
            return error
        finally:
            archive_completed.set()

    def merge():
        if not customer_locked.wait(5):
            return "archive_gate_missing"
        try:
            return commerce.accounts.confirm_telegram_link(customer_id, proof)
        except Exception as error:  # noqa: BLE001 - preserve the actual operation outcome for assertion
            return error

    event.listen(db.engine, "after_cursor_execute", archive_gate)
    event.listen(db.engine, "before_cursor_execute", merge_gate)
    try:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="archive-merge-race") as first:
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="canonical-merge-race") as second:
                archive_future = first.submit(archive)
                merge_future = second.submit(merge)
                archive_result = archive_future.result(timeout=12)
                merge_result = merge_future.result(timeout=12)
    finally:
        event.remove(db.engine, "after_cursor_execute", archive_gate)
        event.remove(db.engine, "before_cursor_execute", merge_gate)
    assert not isinstance(archive_result, Exception), (
        type(archive_result).__name__,
        getattr(getattr(archive_result, "orig", None), "sqlstate", None),
    )
    assert merge_result != "archive_gate_missing", "Archive did not acquire the customer account lock"
    assert not isinstance(merge_result, Exception), (
        type(merge_result).__name__,
        getattr(getattr(merge_result, "orig", None), "sqlstate", None),
    )
    assert archive_result == 0 and merge_result["id"] == str(owner_id)
    with db.transaction() as session:
        assert session.get(Setting, "archive:" + sid) is None
        assert session.get(Account, customer_id) is None
        assert session.get(Alias, customer_id).canonical_id == owner_id
        assert session.get(Subscription, sid).account_id == owner_id
        assert session.get(Subscription, sid).panel_user_id == identity
        assert [
            (item.id, item.starts_at, item.ends_at, item.paid_value)
            for item in session.scalars(select(Segment))
        ] == periods
        assert list(session.scalars(select(Payment.id))) == payments

        assert (
            session.scalar(
                select(Notification.id).where(
                    Notification.kind.in_(["subscription_deleted", "subscription_deleted_owner"])
                )
            )
            is None
        )
        pending = session.scalar(select(PanelEvent).where(PanelEvent.kind == "user.deleted"))
        assert pending.state == "pending" and pending.available_at > now()
        pending.available_at = now()
    assert events.reconcile_pending() == 1
    assert events.reconcile_pending() == 0
    with db.transaction() as session:
        notices = list(
            session.scalars(
                select(Notification).where(
                    Notification.kind.in_(["subscription_deleted", "subscription_deleted_owner"])
                )
            )
        )
        assert sorted((item.kind, item.account_id) for item in notices) == [
            ("subscription_deleted", owner_id),
            ("subscription_deleted_owner", owner_id),
        ]
        notice = next(item for item in notices if item.kind == "subscription_deleted_owner")
        delivery = session.scalar(
            select(NotificationDelivery).where(NotificationDelivery.notification_id == notice.id)
        )
        assert delivery.channel == "telegram" and delivery.recipient == TG
        assert session.get(Setting, "archive:" + sid).value["owner_id"] == str(owner_id)
        assert session.get(Setting, "archive:" + sid).value["owner_intent_id"] == notice.id
        assert [
            (item.id, item.starts_at, item.ends_at, item.paid_value)
            for item in session.scalars(select(Segment))
        ] == periods
        assert list(session.scalars(select(Payment.id))) == payments


def test_retention_busy_owner_rolls_back_current_claim_and_retries_without_delete(due_retention):
    from concurrent.futures import ThreadPoolExecutor
    from threading import current_thread

    from sunrise_core.errors import DomainError
    from sunrise_infrastructure.db import Account, NotificationDelivery, StaffRole

    from tests.legal_fixture import accept_test_legal

    context, moment = due_retention
    db, _, commerce, _, state, sid, *_ = context
    config = commerce.public_settings
    merge_id, owner_id = 42, 43
    owner_telegram = "70000000000000003"
    config.owner_id = owner_telegram
    config.admin_telegram_allowlist = [owner_telegram]
    config.test_telegram_recipients = owner_telegram
    config.admin_email_allowlist = ["retention-owner@example.test"]
    config.email_allowlist = ["retention-owner@example.test"]
    with db.transaction() as session:
        session.add(Account(id=merge_id, email="retention-owner@example.test", email_verified=True))
        session.add(Account(id=owner_id, telegram_id=int(owner_telegram)))
        session.flush()
        session.add(StaffRole(account_id=owner_id, role="owner"))
        telegram_id = str(session.get(Account, 301).telegram_id)
        periods = [
            (item.id, item.starts_at, item.ends_at, item.paid_value)
            for item in session.scalars(select(Segment))
        ]
    accept_test_legal(commerce, merge_id)
    proof = commerce.accounts.request_link(merge_id, telegram_id, now())["proof"]
    assert commerce.retention.schedule_due(moment) == 1

    def bound_wait(connection):
        if current_thread().name.startswith("retention-owner-busy"):
            connection.exec_driver_sql("SET LOCAL lock_timeout = '500ms'")

    def cleanup():
        try:
            return commerce.retention.run_jobs()
        except Exception as error:  # noqa: BLE001 - verify actual worker retry rather than suppress SQL failures
            return error

    event.listen(db.engine, "begin", bound_wait)
    try:
        with db.transaction() as holding:
            holding.scalar(select(Account).where(Account.id == owner_id).with_for_update())
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="retention-owner-busy") as executor:
                result = executor.submit(cleanup).result(timeout=5)
    finally:
        event.remove(db.engine, "begin", bound_wait)
    assert not isinstance(result, Exception), (
        type(result).__name__,
        getattr(getattr(result, "orig", None), "sqlstate", None),
    )
    assert result == 0 and sum(method == "DELETE" for method, _ in state["calls"]) == 1
    with db.transaction() as session:
        job = session.scalar(select(Job).where(Job.kind == "subscription_cleanup"))
        assert job.state == "pending" and job.attempts == 1
        assert job.payload["phase"] == "unknown" and job.payload["lease"] is None
        assert job.payload["attempted_at"] is not None and job.available_at > now()
        assert session.get(Setting, "archive:" + sid) is None
        assert (
            session.scalar(
                select(Notification.id).where(
                    Notification.kind.in_(["subscription_deleted", "subscription_deleted_owner"])
                )
            )
            is None
        )
        assert [
            (item.id, item.starts_at, item.ends_at, item.paid_value)
            for item in session.scalars(select(Segment))
        ] == periods
    with pytest.raises(DomainError) as denied:
        commerce.accounts.confirm_telegram_link(301, proof)
    assert denied.value.code == "subscription_cleanup_pending"
    with db.transaction() as session:
        session.scalar(select(Job).where(Job.kind == "subscription_cleanup")).available_at = now()
    assert commerce.retention.run_jobs() == 1
    assert commerce.retention.run_jobs() == 0
    assert sum(method == "DELETE" for method, _ in state["calls"]) == 1
    with db.transaction() as session:
        notices = list(
            session.scalars(
                select(Notification).where(
                    Notification.kind.in_(["subscription_deleted", "subscription_deleted_owner"])
                )
            )
        )
        assert sorted(item.kind for item in notices) == ["subscription_deleted", "subscription_deleted_owner"]
        owner_notice = next(item for item in notices if item.kind == "subscription_deleted_owner")
        delivery = session.scalar(
            select(NotificationDelivery).where(NotificationDelivery.notification_id == owner_notice.id)
        )
        assert owner_notice.account_id == owner_id and delivery.channel == "telegram"
        assert delivery.recipient == owner_telegram
        assert session.get(Setting, "archive:" + sid).value["owner_intent_id"] == owner_notice.id
        assert [
            (item.id, item.starts_at, item.ends_at, item.paid_value)
            for item in session.scalars(select(Segment))
        ] == periods
    assert commerce.accounts.confirm_telegram_link(301, proof)["id"] == str(merge_id)


def test_signed_owned_deleted_and_exact_absence_archives_once_without_deleting_history(paid_creation):
    db, _, commerce, config, state, _, aid, sid, _ = paid_creation
    assert commerce.provisioning.run_jobs() == 1
    commerce.dashboard(aid, refresh_panel=True)
    with db.transaction() as session:
        periods = [
            (row.id, row.starts_at, row.ends_at, row.paid_value) for row in session.scalars(select(Segment))
        ]
        payment_count = session.scalar(select(func.count()).select_from(Payment))
        identity = session.get(Subscription, sid).panel_user_id
    events = PanelEvents(db, commerce.panel_provider, config)
    raw, headers = signed_event("user.deleted")
    assert events.accept(raw, headers)["status"] == "accepted"
    state["user"] = None
    assert events.reconcile_pending() == 1
    assert events.accept(raw, headers)["status"] == "duplicate"
    result = commerce.dashboard(aid)
    assert all(item["id"] != sid for item in result["subscriptions"])
    assert result["subscription_deletion_notices"] == [
        {
            "subscription_id": sid,
            "name": "Моя подписка",
            "deleted_at": result["subscription_deletion_notices"][0]["deleted_at"],
            "reason": "manual_panel",
        }
    ]
    with db.transaction() as session:
        assert session.get(Subscription, sid).panel_user_id == identity
        assert [
            (row.id, row.starts_at, row.ends_at, row.paid_value) for row in session.scalars(select(Segment))
        ] == periods
        assert session.scalar(select(func.count()).select_from(Payment)) == payment_count
        snapshot = session.get(Setting, "archive:" + sid).value
        assert snapshot["review_required"] is True
        assert (
            len(
                list(session.scalars(select(Notification).where(Notification.kind == "subscription_deleted")))
            )
            == 1
        )
        assert all(row.state == "done" for row in session.scalars(select(Job).where(Job.kind == "provision")))


@pytest.mark.parametrize("case", ["bare_absence", "foreign_event", "present_user"])
def test_unproved_deletion_never_archives(paid_creation, case):
    db, _, commerce, config, state, _, aid, sid, _ = paid_creation
    commerce.provisioning.run_jobs()
    events = PanelEvents(db, commerce.panel_provider, config)
    if case != "bare_absence":
        raw, headers = signed_event("user.deleted", user_id=999 if case == "foreign_event" else 123)
        events.accept(raw, headers)
    if case != "present_user":
        state["user"] = None
    if case == "bare_absence":
        commerce.dashboard(aid, refresh_panel=True)
    else:
        events.reconcile_pending()
    with db.transaction() as session:
        assert session.get(Setting, "archive:" + sid) is None
        assert (
            session.scalar(select(Notification.id).where(Notification.kind == "subscription_deleted")) is None
        )


def test_day7_archive_hides_active_card_and_emits_support_notice(due_retention):
    context, moment = due_retention
    db, _, commerce, _, _, sid, *_ = context
    assert commerce.retention.schedule_due(moment) == 1
    assert commerce.retention.run_jobs() == 1
    result = commerce.dashboard(301)
    assert result["subscriptions"] == []
    assert result["subscription_deletion_notices"][0]["reason"] == "expiry_cleanup"
    with db.transaction() as session:
        assert session.get(Subscription, sid) is not None
        assert (
            session.scalar(select(Notification).where(Notification.kind == "subscription_deleted")).body
            == "Ваша подписка удалена, поскольку после окончания её срока действия прошло 7 дней."
        )


def test_verified_merge_preserves_archive_and_single_deletion_intent(paid_creation):
    from sunrise_infrastructure.db import Account

    from tests.integration.test_v11_panel_events import TG
    from tests.legal_fixture import accept_test_legal

    db, _, commerce, config, state, _, aid, sid, _ = paid_creation
    commerce.provisioning.run_jobs()
    events = PanelEvents(db, commerce.panel_provider, config)
    raw, headers = signed_event("user.deleted")
    events.accept(raw, headers)
    state["user"] = None
    assert events.reconcile_pending() == 1
    with db.transaction() as session:
        source = Account(email="archive-own-merge@example.test", email_verified=True)
        session.add(source)
        session.flush()
        canonical = source.id
    accept_test_legal(commerce, canonical)
    proof = commerce.accounts.request_link(canonical, TG, now())["proof"]
    commerce.accounts.confirm_telegram_link(aid, proof)
    result = commerce.dashboard(canonical)
    assert all(item["id"] != sid for item in result["subscriptions"])
    assert len(result["subscription_deletion_notices"]) == 1
    with db.transaction() as session:
        assert session.get(Setting, "archive:" + sid).value["owner_id"] == str(canonical)
        assert (
            len(
                list(session.scalars(select(Notification).where(Notification.kind == "subscription_deleted")))
            )
            == 1
        )


def test_verified_owner_recovery_notice_has_no_binding_or_connection_secret(paid_creation):
    from sunrise_infrastructure.db import Account, StaffRole

    from tests.integration.test_v11_panel_events import TG

    db, _, commerce, config, state, _, aid, sid, _ = paid_creation
    config.owner_id = TG
    config.admin_email_allowlist = ["archive-owner@example.test"]
    config.email_allowlist = ["archive-owner@example.test"]
    with db.transaction() as session:
        owner = session.get(Account, aid)
        owner.email, owner.email_verified = "archive-owner@example.test", True
        session.add(StaffRole(account_id=aid, role="owner"))
    commerce.provisioning.run_jobs()
    commerce.dashboard(aid, refresh_panel=True)
    binding = state["user"]["shortUuid"]
    events = PanelEvents(db, commerce.panel_provider, config)
    raw, headers = signed_event("user.deleted")
    events.accept(raw, headers)
    state["user"] = None
    assert events.reconcile_pending() == 1
    with db.transaction() as session:
        notice = session.scalar(select(Notification).where(Notification.kind == "subscription_deleted_owner"))
        assert notice is not None and notice.audience == "owner"
        assert "Тариф:" in notice.body and "Устройства:" in notice.body and "Срок UTC:" in notice.body
        assert binding not in notice.body and "https://" not in notice.body


@pytest.mark.parametrize("mode", ["web", "linked", "configured_other"])
def test_archive_notifies_separate_canonical_owner_with_allowlisted_telegram_identity(paid_creation, mode):
    from sunrise_infrastructure.db import Account, NotificationDelivery

    from tests.integration.test_node_news_and_crm import canonical_owner
    from tests.integration.test_v11_panel_events import TG

    db, _, commerce, config, state, _, aid, sid, _ = paid_creation
    owner_id = canonical_owner(paid_creation, mode=mode)
    owner_telegram = str(int(TG) + 1)
    config.owner_id = None
    config.admin_telegram_allowlist = [owner_telegram]
    with db.transaction() as session:
        session.get(Account, owner_id).telegram_id = int(owner_telegram)
    commerce.provisioning.run_jobs()
    commerce.dashboard(aid, refresh_panel=True)
    connection_secret = state["user"]["shortUuid"]
    with db.transaction() as session:
        identity = session.get(Subscription, sid).panel_user_id
        periods = [
            (item.id, item.starts_at, item.ends_at, item.paid_value)
            for item in session.scalars(select(Segment))
        ]
        payments = list(session.scalars(select(Payment.id)))
    events = PanelEvents(db, commerce.panel_provider, config)
    raw, headers = signed_event("user.deleted")
    assert events.accept(raw, headers)["status"] == "accepted"
    state["user"] = None
    assert events.reconcile_pending() == 1
    assert events.accept(raw, headers)["status"] == "duplicate"
    notifier = NotificationService(db, config)
    assert [item["kind"] for item in notifier.list(owner_id)["items"]] == ["subscription_deleted_owner"]
    assert not any(item["kind"] == "subscription_deleted_owner" for item in notifier.list(aid)["items"])
    with db.transaction() as session:
        notices = list(
            session.scalars(select(Notification).where(Notification.kind == "subscription_deleted_owner"))
        )
        assert len(notices) == 1
        notice = notices[0]
        assert notice.account_id == owner_id and notice.audience == "owner"
        assert connection_secret not in notice.body and "https://" not in notice.body
        assert (
            "Оплаченные права до UTC:" in notice.body
            and "Требуется проверка оплаченных прав: да" in notice.body
        )
        deliveries = list(
            session.scalars(
                select(NotificationDelivery).where(NotificationDelivery.notification_id == notice.id)
            )
        )
        assert [(item.channel, item.recipient) for item in deliveries] == [("telegram", owner_telegram)]
        assert session.get(Setting, "archive:" + sid).value["owner_intent_id"] == notice.id
        assert session.get(Subscription, sid).panel_user_id == identity
        assert [
            (item.id, item.starts_at, item.ends_at, item.paid_value)
            for item in session.scalars(select(Segment))
        ] == periods
        assert list(session.scalars(select(Payment.id))) == payments


@pytest.mark.parametrize(
    "invalid", ["admin", "missing_grant", "unbound_telegram", "unallowlisted", "alias", "ambiguous"]
)
def test_archive_owner_notice_fails_closed_without_changing_user_archive(paid_creation, invalid):
    from tests.integration.test_node_news_and_crm import canonical_owner

    db, _, commerce, config, state, _, aid, sid, _ = paid_creation
    canonical_owner(paid_creation, invalid=invalid)
    commerce.provisioning.run_jobs()
    events = PanelEvents(db, commerce.panel_provider, config)
    assert events.accept(*signed_event("user.deleted"))["status"] == "accepted"
    state["user"] = None
    assert events.reconcile_pending() == 1
    with db.transaction() as session:
        archive = session.get(Setting, "archive:" + sid).value
        assert archive["owner_intent_id"] is None
        assert (
            session.scalar(select(Notification.id).where(Notification.kind == "subscription_deleted_owner"))
            is None
        )
        user = session.scalar(select(Notification).where(Notification.kind == "subscription_deleted"))
        assert user.account_id == aid and user.audience == "account"
        assert user.body == "Подписка была удалена. Если это ошибка, обратитесь в поддержку"
        assert session.get(Subscription, sid) is not None
        assert session.scalar(select(Payment.id)) is not None


def test_archived_existing_agreement_can_be_cancelled_with_enrollment_off(paid_creation):
    from decimal import Decimal

    from sunrise_infrastructure.db import Agreement

    db, _, commerce, config, state, _, aid, sid, _ = paid_creation
    commerce.provisioning.run_jobs()
    with db.transaction() as session:
        session.add(
            Agreement(
                account_id=aid,
                subscription_id=sid,
                provider_id="synthetic-archived-agreement",
                state="active",
                consent_amount=Decimal("100.00"),
                currency="RUB",
            )
        )
        session.get(Setting, "recurring").value = {"enabled": False}
    events = PanelEvents(db, commerce.panel_provider, config)
    assert events.accept(*signed_event("user.deleted"))["status"] == "accepted"
    state["user"] = None
    assert events.reconcile_pending() == 1
    dashboard = commerce.dashboard(aid)
    assert all(item["id"] != sid for item in dashboard["subscriptions"])
    assert dashboard["archived_subscription_agreements"][0]["subscription_id"] == sid
    cancelled = commerce.cancel_agreement(aid, sid)
    assert cancelled["state"] == "stop_pending"
    with db.transaction() as session:
        assert (
            session.scalar(select(Agreement).where(Agreement.subscription_id == sid)).state == "stop_pending"
        )
        assert session.get(Subscription, sid) is not None


def test_manual_deletion_preserves_genuine_future_only_paid_segment(paid_creation, monkeypatch):
    from datetime import timedelta

    from sunrise_infrastructure.db import Account, StaffRole

    from tests.integration.test_node_news_and_crm import signed
    from tests.integration.test_v11_panel_events import TG

    db, _, commerce, config, state, _, aid, sid, _ = paid_creation
    config.owner_id, config.admin_email_allowlist = TG, ["archive-owner@example.test"]
    with db.transaction() as session:
        account = session.get(Account, aid)
        account.email, account.email_verified = "archive-owner@example.test", True
        session.add(StaffRole(account_id=aid, role="owner"))
    commerce.provisioning.run_jobs()
    with db.transaction() as session:
        old_end = session.get(Subscription, sid).expires_at
    # A verified longer remote horizon makes the actual paid renewal start later.
    state["user"]["expireAt"] = (old_end + timedelta(days=5)).isoformat()
    commerce.dashboard(aid, refresh_panel=True)
    quote = commerce.quote(aid, "standard", 30, "renew", sid)
    order = commerce.create_order(aid, quote["id"], "actual-future-only-archive")
    commerce.test_pay(aid, order["id"])
    commerce.provisioning.run_jobs()
    moment = old_end + timedelta(days=1)
    for module in ("panel_events", "subscription_archive", "panel_entitlements"):
        monkeypatch.setattr("sunrise_infrastructure." + module + ".now", lambda: moment)
    monkeypatch.setattr("tests.integration.test_node_news_and_crm.now", lambda: moment)
    with db.transaction() as session:
        periods = [
            (item.id, item.starts_at, item.ends_at, item.paid_value)
            for item in session.scalars(select(Segment).where(Segment.subscription_id == sid))
        ]
        assert any(
            item.starts_at > moment and item.kind == "paid"
            for item in session.scalars(select(Segment).where(Segment.subscription_id == sid))
        )
        assert not any(
            item.starts_at <= moment < item.ends_at
            for item in session.scalars(select(Segment).where(Segment.subscription_id == sid))
        )
    events = PanelEvents(db, commerce.panel_provider, config)
    assert events.accept(*signed("user.deleted", {"id": 123}))["status"] == "accepted"
    state["user"] = None
    assert events.reconcile_pending() == 1
    with db.transaction() as session:
        assert [
            (item.id, item.starts_at, item.ends_at, item.paid_value)
            for item in session.scalars(select(Segment).where(Segment.subscription_id == sid))
        ] == periods
        snapshot = session.get(Setting, "archive:" + sid).value
        assert snapshot["review_required"] is True
        owner_notice = session.scalar(
            select(Notification).where(Notification.kind == "subscription_deleted_owner")
        )
        expected_horizon = max(item[2] for item in periods).isoformat()
        assert snapshot["paid_horizon"] == expected_horizon
        assert "Оплаченные права до UTC: " + expected_horizon in owner_notice.body
        assert "Требуется проверка оплаченных прав: да" in owner_notice.body
        assert owner_notice.audience == "owner"
        assert "https://" not in owner_notice.body and "panel_user_id" not in owner_notice.body
