"""Only the approved paid-notice stages run in the scoped worker."""

from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace

from sunrise_infrastructure.db import now
from sunrise_worker import main as worker_module


def test_scoped_tick_runs_ready_and_paid_notice_delivery_after_provision(monkeypatch):
    calls = []

    class Billing:
        def __init__(self, commerce, terminal, environment):
            assert environment == "scoped_live"

        def poll_known_payments(self, **kwargs):
            calls.append("poll")
            return 0

        def reconcile_inbox(self, *args, **kwargs):
            calls.append("reconcile")
            return 0

        def refresh_missing_settlements(self, *args, **kwargs):
            calls.append("settlements")
            return 0

    monkeypatch.setattr(worker_module, "Billing", Billing)

    @contextmanager
    def transaction():
        yield SimpleNamespace(scalars=lambda *_: [])

    lock = SimpleNamespace(acquire=lambda **_: True, release=lambda: None)
    worker = worker_module.Worker.__new__(worker_module.Worker)
    worker.redis = SimpleNamespace(lock=lambda *args, **kwargs: lock)
    worker.db = SimpleNamespace(transaction=transaction)
    worker.provider = SimpleNamespace(terminal="synthetic-terminal")
    worker.config = SimpleNamespace(payment_settlement_interval_seconds=60, payment_settlement_batch_size=10)
    worker.commerce = SimpleNamespace(
        panel_provider=object(),
        provisioning=SimpleNamespace(run_jobs=lambda **_: calls.append("provision") or 1),
    )
    worker.panel_contacts = SimpleNamespace(run_pending=lambda **_: calls.append("contacts") or 0)
    worker.panel_events = SimpleNamespace(reconcile_pending=lambda **_: calls.append("panel_events") or 1)
    worker.engagement = SimpleNamespace(run_due=lambda **_: calls.append("engagement") or 1)
    worker.scheduler = SimpleNamespace(
        notify_ready=lambda **kwargs: calls.append(("ready", kwargs)),
        sync_panel_routes=lambda: calls.append("routes") or 0,
    )
    worker.notifications = SimpleNamespace(
        deliver_pending=lambda **kwargs: calls.append(("deliver", kwargs)) or 1
    )
    worker._next_route_sync = now() - timedelta(minutes=1)
    worker._news_cleanup_cursor = None

    assert worker._scoped_tick() == 4
    assert calls == [
        "poll",
        "reconcile",
        "settlements",
        "provision",
        "contacts",
        "panel_events",
        "engagement",
        ("ready", {"scoped_only": True}),
        ("deliver", {"scoped_only": True}),
        "routes",
    ]
