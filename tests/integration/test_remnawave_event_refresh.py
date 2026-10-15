"""Panel reads are driven by explicit entries or bounded affected events."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sunrise_core.errors import DomainError
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import Account, Job, PanelObservation, Setting, Subscription, now
from sunrise_infrastructure.panel import PanelUnavailable

from tests.integration.test_panel_28_workflows import panel28_workflow as panel28_workflow
from tests.integration.test_panel_entitlement_freshness import entitlement as entitlement
from tests.integration.test_subscription_retention import due_retention as due_retention
from tests.integration.test_v11_panel_events import (
    owned_panel as owned_panel,
)
from tests.integration.test_v11_panel_events import service, settings, signed_event


def test_cached_dashboard_never_reads_panel_and_explicit_entry_does(owned_panel):
    db, panel, worker, state, aid, sid, _ = owned_panel
    worker.run_jobs()
    commerce = Commerce(
        db, local_test=True, panel_provider=panel, public_settings=settings(), connection_provider=object()
    )
    state["calls"].clear()
    commerce.dashboard(aid)
    assert state["calls"] == []
    commerce.dashboard(aid, refresh_panel=True, refresh_subscription_id=sid)
    assert sum(path == "/api/users/123" for _, path in state["calls"]) == 1


def test_no_pending_event_causes_no_background_stale_user_get(owned_panel):
    db, panel, worker, state, _, _, _ = owned_panel
    worker.run_jobs()
    state["calls"].clear()
    assert service(db, panel).reconcile_pending() == 0
    assert state["calls"] == []


def test_events_for_one_target_coalesce_to_one_get(owned_panel):
    db, panel, worker, state, _, _, _ = owned_panel
    worker.run_jobs()
    events = service(db, panel)
    for kind in ("user.modified", "user.traffic_reset"):
        assert events.accept(*signed_event(kind))["status"] == "accepted"
    state["calls"].clear()
    assert events.reconcile_pending() == 2
    assert sum(path == "/api/users/123" for _, path in state["calls"]) == 1


def test_new_event_cannot_bypass_target_retry_backoff(owned_panel, monkeypatch):
    db, panel, worker, state, _, _, _ = owned_panel
    worker.run_jobs()
    events = service(db, panel)
    events.accept(*signed_event())
    original = panel.snapshot

    def unavailable(binding):
        raise PanelUnavailable("synthetic_unavailable")

    monkeypatch.setattr(panel, "snapshot", unavailable)
    assert events.reconcile_pending() == 0
    monkeypatch.setattr(panel, "snapshot", original)
    events.accept(*signed_event("user.traffic_reset"))
    state["calls"].clear()
    assert events.reconcile_pending() == 0
    assert state["calls"] == []


def test_due_scheduler_uses_stale_hint_without_http(due_retention):
    context, moment = due_retention
    db, _, commerce, _, state, _, *_ = context
    with db.transaction() as session:
        session.scalar(select(PanelObservation)).observed_at = moment - timedelta(days=1)
    state["calls"].clear()
    assert commerce.retention.schedule_due(moment) == 1
    assert state["calls"] == []


def test_stale_due_hint_fresh_renewal_cancels_without_delete(due_retention):
    context, moment = due_retention
    db, _, commerce, _, state, sid, *_ = context
    with db.transaction() as session:
        session.scalar(select(PanelObservation)).observed_at = moment - timedelta(days=1)
    assert commerce.retention.schedule_due(moment) == 1
    renewed = moment + timedelta(days=30)
    state["user"]["expireAt"] = renewed.isoformat()
    state["calls"].clear()
    assert commerce.retention.run_jobs() == 0
    assert not any(method == "DELETE" for method, _ in state["calls"])
    with db.transaction() as session:
        marker = session.get(Setting, "retention:" + sid).value
        job = session.get(Job, marker["job_id"])
        assert marker["phase"] == "canceled" and marker["attempted_at"] is None
        assert job.state == "done" and job.attempts == 0
        assert session.get(Subscription, sid).expires_at == renewed


@pytest.mark.parametrize("invalid", ["missing", "foreign", "error", "restricted", "static"])
def test_invalid_historical_hint_never_schedules_or_contacts_panel(due_retention, invalid):
    context, moment = due_retention
    db, _, commerce, _, state, sid, *_ = context
    with db.transaction() as session:
        observation = session.scalar(select(PanelObservation))
        observation.observed_at = moment - timedelta(days=1)
        if invalid == "missing":
            session.delete(observation)
        elif invalid == "foreign":
            observation.payload = observation.payload | {"account_id": "999999"}
        elif invalid == "error":
            observation.last_error = "panel_unavailable"
        elif invalid == "restricted":
            observation.status = "DISABLED"
        else:
            session.get(Setting, "panel_test:" + sid).value = {"origin": "static_binding"}
    state["calls"].clear()
    assert commerce.retention.schedule_due(moment) == 0
    assert state["calls"] == []


def test_explicit_foreign_subscription_entry_denies_before_http(owned_panel):
    db, panel, worker, state, _, sid, _ = owned_panel
    worker.run_jobs()
    with db.transaction() as session:
        outsider = Account()
        session.add(outsider)
        session.flush()
        outsider_id = outsider.id
    commerce = Commerce(db, local_test=True, panel_provider=panel)
    state["calls"].clear()
    with pytest.raises(DomainError) as denied:
        commerce.dashboard(outsider_id, refresh_panel=True, refresh_subscription_id=sid)
    assert denied.value.status == 404
    assert state["calls"] == []


def test_worker_tick_never_refreshes_node_fleet():
    from sunrise_worker.main import Worker

    calls = []

    def zero():
        return 0

    from sunrise_infrastructure.settings import RuntimePolicy

    worker = Worker.__new__(Worker)
    worker.runtime_policy = RuntimePolicy(SimpleNamespace(runtime_mode="isolated"))
    worker.engagement = SimpleNamespace(run_due=lambda: 0)
    worker.redis = SimpleNamespace(
        lock=lambda *a, **k: SimpleNamespace(acquire=lambda **k: True, release=lambda: None)
    )
    worker.commerce = SimpleNamespace(
        panel_provider=SimpleNamespace(contract="3.4_id"),
        run_jobs=zero,
        retention=SimpleNamespace(schedule_due=zero, run_jobs=zero),
    )
    worker.panel_events = SimpleNamespace(
        reconcile_pending=zero, nodes_snapshot=lambda: calls.append("node_fleet_get")
    )
    worker.scheduler = SimpleNamespace(notify_ready=zero, remind=zero, schedule=zero, deliver_backups=zero)
    worker.broadcasts = SimpleNamespace(expand_pending=zero, report_completed=zero)
    worker.notifications = SimpleNamespace(deliver_pending=zero)
    worker.operational_monitor = SimpleNamespace(notify_owner=zero)
    worker._next_reminders = worker._next_panel_reconcile = now()
    worker.config, worker.provider = SimpleNamespace(panel_reconcile_interval_seconds=60), None
    assert worker.tick() == 0
    assert calls == []
