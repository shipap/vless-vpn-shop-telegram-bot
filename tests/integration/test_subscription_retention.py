"""Durable deletion of owned synthetic users preserves financial and identity history."""

from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select
from sunrise_infrastructure.db import Agreement, Job, Segment, Setting, Subscription, now

from tests.integration.test_panel_28_workflows import panel28_workflow as panel28_workflow
from tests.integration.test_panel_entitlement_freshness import entitlement as entitlement


@pytest.fixture
def due_retention(entitlement):
    db, panel, commerce, _, state, sid, *_ = entitlement
    moment = now()
    state["user"]["expireAt"] = (moment - timedelta(days=8)).isoformat()
    with db.transaction() as session:
        session.add(Setting(key="subscription_retention", value={"enabled": True, "days_after_expiry": 7}))
    commerce.dashboard(301, refresh_panel=True)
    original_transport = panel.client._transport

    def handler(request):
        if request.method == "DELETE":
            state["calls"].append((request.method, request.url.path))
            assert request.url.path == "/api/users/" + state["user"]["uuid"]
            state["user"] = None
            if state.get("delete_timeout"):
                raise httpx.ReadTimeout("synthetic lost delete response")
            return httpx.Response(200, json={"response": {"isDeleted": True}})
        return original_transport.handle_request(request)

    panel.client._transport = httpx.MockTransport(handler)
    yield entitlement, moment


def test_due_cleanup_is_unique_and_preserves_subscription_and_segments(due_retention):
    context, moment = due_retention
    db, _, commerce, _, _, sid, *_ = context
    with db.transaction() as session:
        sub = session.get(Subscription, sid)
        original = (sub.account_id, sub.panel_uuid, sub.short_uuid, sub.connection_url, sub.expires_at)
        history = [(s.id, s.starts_at, s.ends_at, s.paid_value) for s in session.scalars(select(Segment))]
    assert commerce.retention.schedule_due(moment) == 1
    assert commerce.retention.schedule_due(moment) == 0
    with db.transaction() as session:
        assert len(list(session.scalars(select(Job).where(Job.kind == "subscription_cleanup")))) == 1
        sub = session.get(Subscription, sid)
        assert (
            sub.account_id,
            sub.panel_uuid,
            sub.short_uuid,
            sub.connection_url,
            sub.expires_at,
        ) == original
        assert [
            (s.id, s.starts_at, s.ends_at, s.paid_value) for s in session.scalars(select(Segment))
        ] == history


@pytest.mark.parametrize("offset,expected", [(timedelta(microseconds=-1), 0), (timedelta(0), 1)])
def test_cleanup_seven_day_boundary(due_retention, offset, expected):
    context, moment = due_retention
    commerce, state = context[2], context[4]
    state["user"]["expireAt"] = (moment - timedelta(days=7)).isoformat()
    commerce.dashboard(301, refresh_panel=True)
    assert commerce.retention.schedule_due(moment + offset) == expected


@pytest.mark.parametrize("status", ["suspended", "disputed", "revoked", "provisioning_pending"])
def test_strict_status_never_becomes_cleanup_authority(due_retention, status):
    context, moment = due_retention
    db, _, commerce, _, state, sid, *_ = context
    with db.transaction() as session:
        session.get(Subscription, sid).status = status
    assert commerce.retention.schedule_due(moment) == 0
    assert not any(method == "DELETE" for method, _ in state["calls"])


@pytest.mark.parametrize("agreement_state", ["active", "stop_pending", "consent_pending", "unknown"])
def test_any_unstopped_agreement_holds_cleanup(due_retention, agreement_state):
    context, moment = due_retention
    db, _, commerce, _, state, sid, *_ = context
    with db.transaction() as session:
        session.add(
            Agreement(
                account_id=301,
                subscription_id=sid,
                state=agreement_state,
                consent_amount="37.00",
                currency="RUB",
            )
        )
    assert commerce.retention.schedule_due(moment) == 0
    assert not any(method == "DELETE" for method, _ in state["calls"])


def test_delete_archives_projection_but_retains_original_binding(due_retention):
    context, moment = due_retention
    db, _, commerce, _, state, sid, *_ = context
    assert commerce.retention.schedule_due(moment) == 1
    with db.transaction() as session:
        sub = session.get(Subscription, sid)
        uuid, link = sub.panel_uuid, sub.connection_url
    assert commerce.retention.run_jobs() == 1
    assert state["user"] is None
    with db.transaction() as session:
        sub = session.get(Subscription, sid)
        assert sub.account_id == 301 and sub.panel_uuid == uuid and sub.connection_url == link
    projection = commerce.dashboard(301)
    assert all(item["id"] != sid for item in projection["subscriptions"])
    assert projection["subscription_deletion_notices"][0]["subscription_id"] == sid
    assert commerce.retention.run_jobs() == 0


def test_lost_delete_response_reconciles_without_second_delete(due_retention, monkeypatch):
    context, moment = due_retention
    commerce, state = context[2], context[4]
    monkeypatch.setattr("sunrise_infrastructure.retention.now", lambda: moment)
    state["delete_timeout"] = True
    assert commerce.retention.schedule_due(moment) == 1
    assert commerce.retention.run_jobs() == 0
    assert state["user"] is None
    assert commerce.retention.run_jobs() == 0
    monkeypatch.setattr("sunrise_infrastructure.retention.now", lambda: moment + timedelta(seconds=31))
    assert commerce.retention.run_jobs() == 1
    assert len([method for method, _ in state["calls"] if method == "DELETE"]) == 1


def test_due_scan_does_not_starve_eligible_subscription_after_held_rows(due_retention):
    context, moment = due_retention
    db, _, commerce, _, _, _, *_ = context
    with db.transaction() as session:
        for identifier in ["!held", "#held", "$held"]:
            session.add(
                Subscription(
                    id=identifier,
                    account_id=301,
                    devices_limit=4,
                    status="suspended",
                    expires_at=moment - timedelta(days=10),
                )
            )
    assert commerce.retention.schedule_due(moment, limit=1) == 1


def test_pending_cleanup_blocks_new_renew_quote(due_retention):
    from sunrise_core.errors import DomainError

    context, moment = due_retention
    commerce, sid, pid = context[2], context[5], context[6]
    assert commerce.retention.schedule_due(moment) == 1
    with pytest.raises(DomainError) as denied:
        commerce.quote(301, pid, 30, "renew", sid)
    assert denied.value.code == "subscription_cleanup_pending"


def test_initial_missing_remote_user_enters_review_without_delete(due_retention):
    context, moment = due_retention
    db, _, commerce, _, state, sid, *_ = context
    assert commerce.retention.schedule_due(moment) == 1
    state["user"] = None
    assert commerce.retention.run_jobs() == 0
    with db.transaction() as session:
        assert session.get(Setting, "retention:" + sid).value["phase"] == "review"
    assert not any(method == "DELETE" for method, _ in state["calls"])


@pytest.mark.parametrize("disabled", [False, True])
def test_unknown_delete_is_bounded_and_disabled_policy_only_reconciles(due_retention, monkeypatch, disabled):
    context, moment = due_retention
    db, panel, commerce, _, state, sid, *_ = context
    clock = {"now": moment}
    monkeypatch.setattr("sunrise_infrastructure.retention.now", lambda: clock["now"])
    original_transport = panel.client._transport

    def handler(request):
        if request.method == "DELETE":
            state["calls"].append((request.method, request.url.path))
            raise httpx.ReadTimeout("synthetic uncertain deletion")
        return original_transport.handle_request(request)

    panel.client._transport = httpx.MockTransport(handler)
    assert commerce.retention.schedule_due(moment) == 1
    assert commerce.retention.run_jobs() == 0
    assert commerce.retention.run_jobs() == 0
    assert len([m for m, _ in state["calls"] if m == "DELETE"]) == 1
    if disabled:
        with db.transaction() as session:
            session.get(Setting, "subscription_retention").value = {"enabled": False, "days_after_expiry": 7}
    for _attempt in range(5):
        clock["now"] += timedelta(seconds=31)
        assert commerce.retention.run_jobs() == 0
    assert len([m for m, _ in state["calls"] if m == "DELETE"]) == (1 if disabled else 3)
    with db.transaction() as session:
        assert session.get(Setting, "retention:" + sid).value["phase"] == "unknown"
    state["user"] = None
    clock["now"] += timedelta(seconds=31)
    assert commerce.retention.run_jobs() == 1
    assert len([m for m, _ in state["calls"] if m == "DELETE"]) == (1 if disabled else 3)


def test_remote_expired_status_receives_seven_day_cleanup(due_retention):
    context, moment = due_retention
    commerce, state = context[2], context[4]
    state["user"]["status"] = "EXPIRED"
    commerce.dashboard(301, refresh_panel=True)
    assert commerce.retention.schedule_due(moment) == 1
    assert commerce.retention.run_jobs() == 1


def test_late_accepted_payment_preserves_money_and_reconciles_recorded_deletion(due_retention, monkeypatch):
    from decimal import Decimal

    from sunrise_infrastructure.billing import Billing, VerifiedPayment
    from sunrise_infrastructure.db import Ledger, Order, Payment, Settlement

    context, moment = due_retention
    db, _, commerce, _, state, sid, pid, *_ = context
    quote = commerce.quote(301, pid, 30, "renew", sid)
    order = commerce.create_order(301, quote["id"], "late-cleanup-payment")
    with db.transaction() as session:
        original_segments = [
            (x.id, x.starts_at, x.ends_at, x.paid_value) for x in session.scalars(select(Segment))
        ]
        original_jobs = list(session.scalars(select(Job.id).where(Job.kind == "provision")))
        original_ledger = list(session.scalars(select(Ledger.id)))
        row = session.get(Order, order["id"])
        environment, amount = row.environment, row.amount
        row.status = "expired"
    state["delete_timeout"] = True
    assert commerce.retention.schedule_due(moment) == 1
    assert commerce.retention.run_jobs() == 0
    fact = VerifiedPayment(
        "synthetic",
        "cleanup-late",
        "synthetic-terminal",
        environment,
        order["id"],
        amount,
        "RUB",
        moment,
        fee=Decimal("1.00"),
        fee_currency="RUB",
    )
    billing = Billing(commerce, "synthetic-terminal", environment)
    assert billing.accept(fact)["status"] == "reconciliation_required"
    assert billing.accept(fact)["status"] == "reconciliation_required"
    with db.transaction() as session:
        retry_at = session.scalar(select(Job.available_at).where(Job.kind == "subscription_cleanup"))
    assert retry_at is not None
    monkeypatch.setattr("sunrise_infrastructure.retention.now", lambda: retry_at - timedelta(microseconds=1))
    assert commerce.retention.run_jobs() == 0
    monkeypatch.setattr("sunrise_infrastructure.retention.now", lambda: retry_at)
    assert commerce.retention.run_jobs() == 1
    with db.transaction() as session:
        payments = list(session.scalars(select(Payment).where(Payment.order_id == order["id"])))
        assert len(payments) == 1
        assert (
            len(list(session.scalars(select(Settlement).where(Settlement.payment_id == payments[0].id)))) == 1
        )
        assert session.get(Order, order["id"]).fulfilled_at is None
        assert [
            (x.id, x.starts_at, x.ends_at, x.paid_value) for x in session.scalars(select(Segment))
        ] == original_segments
        assert list(session.scalars(select(Job.id).where(Job.kind == "provision"))) == original_jobs
        assert list(session.scalars(select(Ledger.id))) == original_ledger
        assert session.get(Setting, "retention:" + sid).value["phase"] == "archived"
    assert len([m for m, _ in state["calls"] if m == "DELETE"]) == 1


def test_scheduler_without_verified_expiry_hint_never_polls_user(entitlement):
    from sunrise_infrastructure.db import PanelObservation

    db, _, commerce, _, state, sid, *_ = entitlement
    moment = now()
    state["user"]["expireAt"] = (moment - timedelta(days=8)).isoformat()
    state["user"]["status"] = "EXPIRED"
    with db.transaction() as session:
        session.add(Setting(key="subscription_retention", value={"enabled": True, "days_after_expiry": 7}))
        for row in session.scalars(select(PanelObservation)):
            row.observed_at = moment - timedelta(hours=1)
    state["calls"].clear()
    assert commerce.retention.schedule_due(moment) == 0
    assert state["calls"] == []
    commerce.dashboard(301, refresh_panel=True)
    assert commerce.retention.schedule_due(moment) == 1


def test_active_agreement_does_not_project_a_guaranteed_cleanup_date(due_retention):
    context, _ = due_retention
    db, _, commerce, _, _, sid, *_ = context
    with db.transaction() as session:
        session.add(
            Agreement(
                account_id=301, subscription_id=sid, state="active", consent_amount="37.00", currency="RUB"
            )
        )
    assert commerce.dashboard(301)["subscriptions"][0]["cleanup_at"] is None


@pytest.mark.parametrize("changed", ["expiry", "status"])
def test_changed_user_after_unknown_attempt_is_get_only_until_absence(due_retention, monkeypatch, changed):
    context, moment = due_retention
    db, panel, commerce, _, state, sid, *_ = context
    original = panel.client._transport

    def handler(request):
        if request.method == "DELETE":
            state["calls"].append((request.method, request.url.path))
            raise httpx.ReadTimeout("synthetic delayed operation")
        return original.handle_request(request)

    panel.client._transport = httpx.MockTransport(handler)
    assert commerce.retention.schedule_due(moment) == 1
    assert commerce.retention.run_jobs() == 0
    if changed == "expiry":
        state["user"]["expireAt"] = (moment + timedelta(days=30)).isoformat()
    else:
        state["user"]["status"] = "DISABLED"
    monkeypatch.setattr("sunrise_infrastructure.retention.now", lambda: moment + timedelta(seconds=31))
    assert commerce.retention.run_jobs() == 0
    with db.transaction() as session:
        assert session.get(Setting, "retention:" + sid).value["phase"] == "unknown"
    state["user"] = None
    monkeypatch.setattr("sunrise_infrastructure.retention.now", lambda: moment + timedelta(seconds=62))
    assert commerce.retention.run_jobs() == 1
    assert len([m for m, _ in state["calls"] if m == "DELETE"]) == 1


@pytest.mark.parametrize("archived", [False, True])
def test_identity_merge_preserves_archive_or_holds_live_fence(due_retention, archived):
    from sunrise_core.errors import DomainError
    from sunrise_infrastructure.db import Account, Alias

    from tests.legal_fixture import accept_test_legal

    context, moment = due_retention
    db, _, commerce, _, _, sid, *_ = context
    assert commerce.retention.schedule_due(moment) == 1
    if archived:
        assert commerce.retention.run_jobs() == 1
    with db.transaction() as session:
        source = Account(email="archive-merge@example.test", email_verified=True)
        session.add(source)
        session.flush()
        source_id = source.id
        tg = session.get(Account, 301).telegram_id
    accept_test_legal(commerce, source_id)
    proof = commerce.accounts.request_link(source_id, str(tg), now())["proof"]
    if not archived:
        with pytest.raises(DomainError) as denied:
            commerce.accounts.confirm_telegram_link(301, proof)
        assert denied.value.code == "subscription_cleanup_pending"
        with db.transaction() as session:
            assert session.get(Subscription, sid).account_id == 301
        return
    assert commerce.accounts.confirm_telegram_link(301, proof)["id"] == str(source_id)
    with db.transaction() as session:
        assert session.get(Alias, 301).canonical_id == source_id
        sub = session.get(Subscription, sid)
        marker = session.get(Setting, "retention:" + sid).value
        assert sub.account_id == source_id and marker["owner_id"] == str(source_id)
        assert marker["phase"] == "archived"
        assert session.get(Job, marker["job_id"]).payload == marker
        assert session.get(Setting, "panel_test:" + sid).value["account_id"] == "301"
    projection = commerce.dashboard(source_id)
    assert all(item["id"] != sid for item in projection["subscriptions"])
    assert projection["subscription_deletion_notices"][0]["subscription_id"] == sid


def test_overlapping_workers_and_checkout_observe_persisted_fence(due_retention):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from sunrise_core.errors import DomainError

    context, moment = due_retention
    db, panel, commerce, _, _, sid, pid, *_ = context
    assert commerce.retention.schedule_due(moment) == 1
    entered, release = Event(), Event()
    original = panel.client._transport

    def handler(request):
        if request.method == "GET" and not entered.is_set():
            entered.set()
            assert release.wait(10)
        if request.method == "DELETE":
            with db.transaction() as session:
                marker = session.get(Setting, "retention:" + sid).value
                assert marker["attempted_at"] is not None
                assert session.get(Job, marker["job_id"]).attempts == 1
            with pytest.raises(DomainError) as denied:
                commerce.quote(301, pid, 30, "renew", sid)
            assert denied.value.code == "subscription_cleanup_pending"
        return original.handle_request(request)

    panel.client._transport = httpx.MockTransport(handler)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(commerce.retention.run_jobs)
        try:
            assert entered.wait(10)
            assert commerce.retention.run_jobs() == 0
        finally:
            release.set()
        assert first.result(timeout=10) == 1


def test_earlier_unknown_delete_finishing_before_retry_404_is_reconciled(due_retention, monkeypatch):
    context, moment = due_retention
    _, panel, commerce, _, state, *_ = context
    original = panel.client._transport
    calls = {"delete": 0}

    def handler(request):
        if request.method == "DELETE":
            calls["delete"] += 1
            if calls["delete"] == 1:
                raise httpx.ReadTimeout("synthetic delayed deletion")
            state["user"] = None
            return httpx.Response(404)
        return original.handle_request(request)

    panel.client._transport = httpx.MockTransport(handler)
    assert commerce.retention.schedule_due(moment) == 1
    assert commerce.retention.run_jobs() == 0
    monkeypatch.setattr("sunrise_infrastructure.retention.now", lambda: moment + timedelta(seconds=31))
    assert commerce.retention.run_jobs() == 1
    assert calls["delete"] == 2


@pytest.mark.parametrize(
    "hold",
    [
        "future_paid",
        "pending_payment",
        "provider_unknown",
        "inbox_unknown",
        "pending_job",
        "private_legacy",
        "static_binding",
        "disabled",
    ],
)
def test_unresolved_or_unproved_targets_never_schedule_delete(due_retention, hold):
    from decimal import Decimal

    from sunrise_infrastructure.db import Inbox, LegacyIssue, Order, ProviderOperation

    context, moment = due_retention
    db, _, commerce, _, state, sid, pid, *_ = context
    if hold in {"pending_payment", "provider_unknown"}:
        quote = commerce.quote(301, pid, 30, "renew", sid)
        order = commerce.create_order(301, quote["id"], "retention-hold")
        if hold == "provider_unknown":
            with db.transaction() as session:
                session.get(Order, order["id"]).status = "expired"
                session.add(
                    ProviderOperation(
                        order_id=order["id"], terminal="synthetic", state="outcome_unknown", request={}
                    )
                )
    with db.transaction() as session:
        if hold == "future_paid":
            session.add(
                Segment(
                    subscription_id=sid,
                    product_id=pid,
                    starts_at=moment + timedelta(days=2),
                    ends_at=moment + timedelta(days=32),
                    devices=4,
                    paid_value=Decimal("37.00"),
                    price_basis={},
                )
            )
        elif hold == "inbox_unknown":
            session.add(Inbox(event_key="synthetic-unknown", payload={}, state="review"))
        elif hold == "pending_job":
            session.add(
                Job(
                    key="synthetic-pending",
                    kind="provision",
                    account_id=301,
                    payload={"subscription_id": sid},
                )
            )
        elif hold == "private_legacy":
            session.add(
                LegacyIssue(source_table="subscriptions", source_id=sid, reason="synthetic-private-copy")
            )
        elif hold == "static_binding":
            session.get(Setting, "panel_test:" + sid).value = {"origin": "static_binding"}
        elif hold == "disabled":
            session.get(Setting, "subscription_retention").value = {"enabled": False, "days_after_expiry": 7}
    assert commerce.retention.schedule_due(moment) == 0
    assert not any(m == "DELETE" for m, _ in state["calls"])
    assert commerce.dashboard(301)["subscriptions"][0]["cleanup_at"] is None


def test_worker_tick_runs_scheduling_and_confirmed_cleanup(due_retention):
    from types import SimpleNamespace

    import fakeredis
    from sunrise_infrastructure.scheduler import Scheduler
    from sunrise_infrastructure.subscription_engagement import SubscriptionEngagement
    from sunrise_worker.main import Worker

    context, moment = due_retention
    _, _, commerce, _, state, *_ = context
    from sunrise_infrastructure.settings import RuntimePolicy

    worker = Worker.__new__(Worker)
    worker.runtime_policy = RuntimePolicy(SimpleNamespace(runtime_mode="isolated"))
    worker.commerce, worker.redis, worker.provider = commerce, fakeredis.FakeRedis(), None
    worker.scheduler = Scheduler(commerce, commerce.public_settings)
    worker.scheduler.deliver_backups = lambda: 0
    worker.scheduler.schedule = lambda: 0
    worker.scheduler.notify_ready = lambda: None
    worker.broadcasts = SimpleNamespace(expand_pending=lambda: 0, report_completed=lambda: 0)
    worker.notifications = SimpleNamespace(deliver_pending=lambda: 0)
    worker.engagement = SubscriptionEngagement(commerce)
    worker.panel_events = SimpleNamespace(reconcile_pending=lambda: 0, nodes_snapshot=lambda: None)
    worker.operational_monitor = SimpleNamespace(notify_owner=lambda: None)
    worker._next_reminders = worker._next_panel_reconcile = moment
    worker.config = SimpleNamespace(panel_reconcile_interval_seconds=60)
    worker.tick()
    assert state["user"] is None


def test_expiry_reminder_shares_truthful_warning_and_deduplication(entitlement):
    from sunrise_infrastructure.db import Notification
    from sunrise_infrastructure.scheduler import Scheduler

    db, _, commerce, _, state, *_ = entitlement
    moment = now()
    state["user"]["expireAt"] = (moment + timedelta(hours=1)).isoformat()
    with db.transaction() as session:
        session.add(Setting(key="subscription_retention", value={"enabled": True, "days_after_expiry": 7}))
    commerce.dashboard(301, refresh_panel=True)
    scheduler = Scheduler(commerce, commerce.public_settings)
    scheduler.remind(moment)
    scheduler.remind(moment)
    with db.transaction() as session:
        notes = list(session.scalars(select(Notification).where(Notification.kind == "subscription_expiry")))
        assert len(notes) == 1
        assert "Через 7 дней" in notes[0].body and "история платежей сохранятся" in notes[0].body


def test_fenced_target_cannot_prepare_a_new_provider_checkout(due_retention):
    from types import SimpleNamespace

    from sunrise_core.errors import DomainError
    from sunrise_infrastructure.billing import Billing
    from sunrise_infrastructure.db import Order, ProviderOperation

    context, moment = due_retention
    db, _, commerce, _, _, sid, pid, *_ = context
    quote = commerce.quote(301, pid, 30, "renew", sid)
    order = commerce.create_order(301, quote["id"], "checkout-after-cleanup")
    with db.transaction() as session:
        row = session.get(Order, order["id"])
        row.status, row.environment = "expired", "sandbox"
    assert commerce.retention.schedule_due(moment) == 1
    calls = []
    provider = SimpleNamespace(terminal="synthetic", create_payment=lambda **kwargs: calls.append(kwargs))
    with pytest.raises(DomainError) as denied:
        Billing(commerce, "synthetic", "sandbox").prepare_payment(order["id"], provider)
    assert denied.value.code == "subscription_cleanup_pending"
    assert calls == []
    with db.transaction() as session:
        assert (
            session.scalar(select(ProviderOperation.id).where(ProviderOperation.order_id == order["id"]))
            is None
        )


def test_old_renewal_quote_cannot_checkout_after_cleanup_claim(due_retention):
    from sunrise_core.errors import DomainError

    context, moment = due_retention
    commerce, sid, pid = context[2], context[5], context[6]
    quote = commerce.quote(301, pid, 30, "renew", sid)
    assert commerce.retention.schedule_due(moment) == 1
    with pytest.raises(DomainError) as denied:
        commerce.create_order(301, quote["id"], "old-quote-cleanup")
    assert denied.value.code == "subscription_cleanup_pending"


@pytest.mark.parametrize("kind", ["provision", "credential_rotate", "device_revoke"])
def test_existing_panel_jobs_cannot_recreate_or_modify_archived_target(due_retention, kind):
    context, moment = due_retention
    db, _, commerce, _, state, sid, *_ = context
    assert commerce.retention.schedule_due(moment) == 1
    assert commerce.retention.run_jobs() == 1
    before = list(state["calls"])
    with db.transaction() as session:
        session.add(
            Job(
                key="after-archive:" + kind,
                kind=kind,
                account_id=301,
                payload={
                    "subscription_id": sid,
                    "environment": "local_test",
                    "version": session.get(Subscription, sid).version,
                },
            )
        )
    commerce.run_jobs()
    assert state["calls"] == before
    with db.transaction() as session:
        job = session.scalar(select(Job).where(Job.key == "after-archive:" + kind))
        assert job.state == "review" and job.last_error == "subscription_cleanup_pending"


def test_refund_financial_fact_survives_cleanup_fence_without_mutating_paid_history(due_retention):
    from sunrise_core.refunds import VerifiedEntitlementUsage
    from sunrise_infrastructure.billing import Billing
    from sunrise_infrastructure.db import Payment, Refund, RefundReview

    context, moment = due_retention
    db, panel, commerce, _, state, sid, pid, *_ = context
    quote = commerce.quote(301, pid, 30, "purchase")
    order = commerce.create_order(301, quote["id"], "refund-cleanup-fence")
    commerce.test_pay(301, order["id"])
    original_transport = panel.client._transport

    def new_user_transport(request):
        if (
            request.method == "GET"
            and "/users/by-username/" in request.url.path
            and not request.url.path.endswith(state["user"]["username"])
        ):
            return httpx.Response(404)
        return original_transport.handle_request(request)

    panel.client._transport = httpx.MockTransport(new_user_transport)
    commerce.run_jobs()
    with db.transaction() as session:
        from sunrise_infrastructure.db import Order

        sid = session.get(Order, order["id"]).subscription_id
        jobs = list(
            session.scalars(
                select(Job).where(Job.kind == "provision", Job.payload["subscription_id"].as_string() == sid)
            )
        )
        assert all(job.state == "done" and job.last_error is None for job in jobs), [
            (job.state, job.last_error) for job in jobs
        ]
    state["user"]["expireAt"] = (moment - timedelta(days=8)).isoformat()
    commerce.dashboard(301, refresh_panel=True)
    with db.transaction() as session:
        decision = commerce.retention.decision_in(session, session.get(Subscription, sid), now())
        assert decision.eligible, decision.reason
    assert commerce.order(301, order["id"])["provisioning_status"] == "ready"
    assert commerce.retention.schedule_due(now()) >= 1
    assert commerce.order(301, order["id"])["provisioning_status"] == "review"
    with db.transaction() as session:
        payment = session.scalar(select(Payment).where(Payment.order_id == order["id"]))
        segment = session.scalar(select(Segment).where(Segment.order_id == order["id"]))
        original = (segment.starts_at, segment.ends_at, segment.paid_value, segment.kind)
        terminal, environment, provider_id, amount = (
            payment.terminal,
            payment.environment,
            payment.provider_id,
            payment.amount,
        )
        segment_id, started_at = segment.id, segment.starts_at
        version = session.get(Subscription, sid).version
    usage = VerifiedEntitlementUsage(
        order["id"], segment_id, started_at, now(), 0, "verified_panel_attestation"
    )
    billing = Billing(commerce, terminal, environment)
    billing.refund(provider_id, "cleanup-fence", amount, now(), usage=usage)
    with db.transaction() as session:
        segment = session.get(Segment, segment_id)
        assert (segment.starts_at, segment.ends_at, segment.paid_value, segment.kind) == original
        assert session.get(Subscription, sid).version == version
        refund = session.scalar(select(Refund).where(Refund.payment_id == payment.id))
        review = session.scalar(select(RefundReview).where(RefundReview.refund_id == refund.id))
        assert review.evidence["reason"] == "retention_target_fenced"


def test_attempt_marker_without_durable_attempt_count_cannot_claim_initial_absence(due_retention):
    context, moment = due_retention
    db, _, commerce, _, state, sid, *_ = context
    assert commerce.retention.schedule_due(moment) == 1
    with db.transaction() as session:
        row = session.get(Setting, "retention:" + sid)
        row.value = row.value | {"phase": "unknown", "attempted_at": moment.isoformat()}
        session.get(Job, row.value["job_id"]).payload = row.value
    state["user"] = None
    assert commerce.retention.run_jobs() == 0
    assert commerce.dashboard(301)["subscriptions"][0]["retention_state"] == "review"


@pytest.mark.parametrize("fault", ["owner", "version", "contract", "job", "bool_version"])
def test_malformed_tombstone_never_deletes_or_exposes_connection(due_retention, fault):
    context, moment = due_retention
    db, _, commerce, _, state, sid, *_ = context
    assert commerce.retention.schedule_due(moment) == 1
    with db.transaction() as session:
        row = session.get(Setting, "retention:" + sid)
        patch = {
            "owner": {"owner_id": "999"},
            "version": {"target_version": 999},
            "contract": {"contract": "3.4_id"},
            "job": {"job_id": "missing"},
            "bool_version": {"target_version": True},
        }[fault]
        row.value = row.value | patch
    assert commerce.retention.run_jobs() == 0
    projection = commerce.dashboard(301)["subscriptions"][0]
    assert projection["retention_state"] == "review" and projection["connection_url"] is None
    assert not any(method == "DELETE" for method, _ in state["calls"])


def test_worker_stale_owner_candidate_preserves_new_owner_state(due_retention, monkeypatch):
    from sunrise_infrastructure.db import Account

    context, moment = due_retention
    db, _, commerce, _, state, sid, *_ = context
    assert commerce.retention.schedule_due(moment) == 1
    service = commerce.retention
    original_locked = service._locked
    switched = False

    def change_owner_before_lock(session, owner, target, job_id):
        nonlocal switched
        if not switched:
            switched = True
            with db.transaction() as tx:
                tx.add(Account(id=302, email="synthetic-owner-race@example.test"))
                sub = tx.get(Subscription, sid)
                sub.account_id = 302
                marker = tx.get(Setting, "retention:" + sid)
                marker.value = marker.value | {"owner_id": "302"}
                job = tx.get(Job, job_id)
                job.account_id, job.payload = 302, marker.value
        return original_locked(session, owner, target, job_id)

    monkeypatch.setattr(service, "_locked", change_owner_before_lock)
    assert service.run_jobs() == 0
    with db.transaction() as tx:
        sub = tx.get(Subscription, sid)
        marker = tx.get(Setting, "retention:" + sid).value
        job = tx.get(Job, marker["job_id"])
        assert marker["phase"] == "pending" and job.state == "pending"
        assert sub.version == marker["target_version"] and job.attempts == 0
    assert not any(method == "DELETE" for method, _ in state["calls"])


def test_scheduler_stale_owner_candidate_is_skipped_without_claim(due_retention):
    from sqlalchemy import event
    from sunrise_infrastructure.db import Account

    context, moment = due_retention
    db, _, commerce, _, state, sid, *_ = context
    candidates_captured, switched = False, False
    with db.transaction() as tx:
        original_version = tx.get(Subscription, sid).version
        tx.add(Account(id=302, email="synthetic-scheduler-race@example.test"))

    def switch_after_candidates(connection, cursor, statement, parameters, execution_context, executemany):
        nonlocal candidates_captured, switched
        if "SELECT v5_subscriptions.id, v5_subscriptions.account_id" in statement:
            candidates_captured = True
        if (
            candidates_captured
            and not switched
            and "FROM v5_accounts" in statement
            and "FOR UPDATE" in statement
        ):
            switched = True
            with db.transaction() as tx:
                tx.get(Subscription, sid).account_id = 302

    event.listen(db.engine, "after_cursor_execute", switch_after_candidates)
    try:
        assert commerce.retention.schedule_due(moment) == 0
    finally:
        event.remove(db.engine, "after_cursor_execute", switch_after_candidates)
    assert switched
    with db.transaction() as tx:
        assert tx.get(Subscription, sid).version == original_version
        assert tx.get(Setting, "retention:" + sid) is None
        assert not list(tx.scalars(select(Job).where(Job.kind == "subscription_cleanup")))
    assert not any(method == "DELETE" for method, _ in state["calls"])


def test_disabled_unattempted_cleanup_history_remains_nonfencing_after_paid_renewal(due_retention):
    from sunrise_infrastructure.retention import fenced_in, state_in

    context, moment = due_retention
    db, _, commerce, _, state, sid, pid, *_ = context
    assert commerce.retention.schedule_due(moment) == 1
    with db.transaction() as tx:
        tx.get(Setting, "subscription_retention").value = {"enabled": False, "days_after_expiry": 7}
    assert commerce.retention.run_jobs() == 0
    with db.transaction() as tx:
        history = dict(tx.get(Setting, "retention:" + sid).value)
        assert history["phase"] == "canceled" and history["attempted_at"] is None
    quote = commerce.quote(301, pid, 30, "renew", sid)
    order = commerce.create_order(301, quote["id"], "renew-after-cancel")
    commerce.test_pay(301, order["id"])
    with db.transaction() as tx:
        sub = tx.get(Subscription, sid)
        assert sub.version > history["target_version"]
        assert not fenced_in(tx, sub)
        assert state_in(tx, sub).phase == "canceled"
        assert tx.get(Setting, "retention:" + sid).value == history
        assert tx.get(Job, history["job_id"]).payload == history
        assert tx.get(Job, history["job_id"]).state == "done"
        assert tx.scalar(select(Segment).where(Segment.order_id == order["id"])) is not None
    assert not any(method == "DELETE" for method, _ in state["calls"])


@pytest.mark.parametrize("fault", ["attempt", "count", "pending_job", "lease", "archive", "future_version"])
def test_canceled_marker_without_terminal_unattempted_proof_stays_fenced(due_retention, fault):
    from sunrise_infrastructure.retention import fenced_in

    context, moment = due_retention
    db, _, commerce, _, state, sid, *_ = context
    assert commerce.retention.schedule_due(moment) == 1
    with db.transaction() as tx:
        marker = tx.get(Setting, "retention:" + sid)
        job = tx.get(Job, marker.value["job_id"])
        job.state = "done"
        marker.value = marker.value | {"phase": "canceled"}
        if fault == "attempt":
            marker.value = marker.value | {"attempted_at": moment.isoformat()}
            job.attempts = 1
        elif fault == "count":
            job.attempts = 1
        elif fault == "pending_job":
            job.state = "pending"
        elif fault == "lease":
            marker.value = marker.value | {"lease": "synthetic-lease"}
        elif fault == "archive":
            marker.value = marker.value | {"archived_at": moment.isoformat()}
        else:
            tx.get(Subscription, sid).version -= 1
        job.payload = marker.value
        assert fenced_in(tx, tx.get(Subscription, sid))
    assert not any(method == "DELETE" for method, _ in state["calls"])
