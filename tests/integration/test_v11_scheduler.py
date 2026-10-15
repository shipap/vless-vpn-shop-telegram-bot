import importlib.util
from datetime import UTC, datetime

from sqlalchemy import select
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import Job
from test_v11_notifications import notification_context as notification_context


def test_backup_schedule_moscow_owner_only_idempotent(notification_context, tmp_path):
    db, settings, _ = notification_context
    assert importlib.util.find_spec("sunrise_infrastructure.scheduler") is not None, (
        "Daily backup scheduler missing"
    )
    from sunrise_infrastructure.instance_config import InstanceConfig
    from sunrise_infrastructure.scheduler import Scheduler

    settings.instance = InstanceConfig(app_name="Другой магазин")

    calls = []

    def dump(identifier):
        result = tmp_path / (identifier + ".sql")
        result.write_text("-- Synthetic full database backup", encoding="utf-8")
        return result

    scheduler = Scheduler(
        Commerce(db, local_test=True, public_settings=settings),
        settings,
        dump_database=dump,
        verify_database=lambda *args: {
            "state": "verified",
            "table_count": 59,
            "snapshot_equal": True,
            "relations_equal": True,
        },
        send_document=lambda recipient, path, caption: calls.append((recipient, path.name, caption)),
    )
    assert scheduler.schedule(datetime(2026, 10, 8, 12, 59, tzinfo=UTC)) == 0
    assert scheduler.schedule(datetime(2026, 10, 8, 13, 0, tzinfo=UTC)) == 1
    assert scheduler.schedule(datetime(2026, 10, 8, 14, 0, tzinfo=UTC)) == 0
    scheduler.deliver_backups()
    scheduler.deliver_backups()
    assert calls == [
        (
            "202",
            calls[0][1],
            "Резервная копия всей тестовой базы Другой магазин. Храните файл конфиденциально.",
        )
    ]
    with db.transaction() as session:
        assert list(session.scalars(select(Job.state).where(Job.kind == "daily_backup"))) == ["done"]
