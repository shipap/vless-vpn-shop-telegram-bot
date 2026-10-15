from datetime import timedelta
from types import SimpleNamespace

from sunrise_infrastructure.db import now
from sunrise_worker.main import Worker


def test_live_worker_keeps_business_ticks_but_never_calls_synthetic_backup():
    calls = []

    def callback(name, value=0):
        def call(*args, **kwargs):
            calls.append(name)
            return value

        return call

    lock = SimpleNamespace(acquire=lambda **_: True, release=lambda: None)
    worker = Worker.__new__(Worker)
    worker.runtime_policy = SimpleNamespace(production=True, require=lambda *args: None)
    worker.redis = SimpleNamespace(lock=lambda *args, **kwargs: lock)
    worker.broadcasts = SimpleNamespace(
        expand_pending=callback("broadcast"), report_completed=callback("report")
    )
    worker.commerce = SimpleNamespace(
        panel_provider=None,
        run_jobs=callback("provision"),
        retention=SimpleNamespace(
            schedule_due=callback("retention_schedule"), run_jobs=callback("retention")
        ),
    )
    worker.scheduler = SimpleNamespace(
        notify_ready=callback("ready"),
        remind=callback("remind"),
        schedule=callback("synthetic_backup_schedule"),
        deliver_backups=callback("synthetic_backup_delivery"),
    )
    worker.engagement = SimpleNamespace(run_due=callback("engagement"))
    worker.operational_monitor = SimpleNamespace(notify_owner=callback("monitor"))
    worker.archive_owner_notices = SimpleNamespace(run_pending=callback("archive_owner"))
    worker.panel_contacts = SimpleNamespace(run_pending=callback("contacts"))
    worker.notifications = SimpleNamespace(deliver_pending=callback("notices"))
    worker._next_reminders = now() - timedelta(minutes=1)
    worker._stage = lambda *args: True
    worker.provider = None
    assert worker.tick() == 0
    assert "remind" in calls and "provision" in calls and "engagement" in calls
    assert calls.index("archive_owner") < calls.index("notices")
    assert calls.index("provision") < calls.index("contacts") < calls.index("notices")
    assert not any(name.startswith("synthetic_backup") for name in calls)
