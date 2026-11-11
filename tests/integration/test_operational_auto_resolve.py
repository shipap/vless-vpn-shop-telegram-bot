"""Сигналы, которые закрываются сами: устаревшие отказы копий и старые проверки доставки."""

from datetime import timedelta

from sqlalchemy import func, select
from sunrise_infrastructure.db import Audit, Job, NotificationDelivery, Setting, now
from sunrise_infrastructure.notifications import NotificationService
from sunrise_infrastructure.operational_monitor import OperationalMonitor

from tests.integration.test_v11_notifications import notification_context  # noqa: F401


def backup(session, owner, key, state, at, delivery=None, error=None):
    payload = {"backup_scope": "production_server_sql", "source": "daily"}
    if delivery is not None:
        payload["document_delivery"] = {"state": delivery, "storage": "memory"}
    session.add(
        Job(
            key=key,
            kind="daily_backup",
            account_id=owner,
            state=state,
            attempts=3 if state == "review" else 1,
            last_error=error,
            available_at=at,
            payload=payload,
        )
    )


def job_signals(db, config):
    return OperationalMonitor(db, config).snapshot()["counts"]["job"]


def test_failed_backups_stop_counting_after_newer_delivered_copy(notification_context):  # noqa: F811
    db, config, (owner, _, _) = notification_context
    old = now() - timedelta(days=3)
    with db.transaction() as session:
        for day in range(3):
            backup(session, owner, f"failed-{day}", "review", old + timedelta(days=day), error="verification")
    assert job_signals(db, config) == 3
    with db.transaction() as session:
        # A newer copy whose delivery is not confirmed does not hide the failures.
        backup(session, owner, "undelivered", "done", now() - timedelta(hours=2), delivery="review")
    assert job_signals(db, config) == 3
    with db.transaction() as session:
        backup(session, owner, "delivered", "done", now() - timedelta(hours=1), delivery="delivered")
    assert job_signals(db, config) == 0
    with db.transaction() as session:
        # A failure after the last good copy is still a signal; history is untouched.
        backup(session, owner, "failed-after", "review", now(), error="verification")
        states = dict(session.execute(select(Job.key, Job.state).where(Job.kind == "daily_backup")).all())
    assert job_signals(db, config) == 1
    assert [states[f"failed-{day}"] for day in range(3)] == ["review"] * 3


def test_non_production_done_backup_without_document_supersedes(notification_context):  # noqa: F811
    db, config, (owner, other, _) = notification_context
    with db.transaction() as session:
        backup(session, owner, "failed", "review", now() - timedelta(days=1), error="x")
        backup(session, other, "other-done", "done", now())
    assert job_signals(db, config) == 1  # another account's copy never hides it
    with db.transaction() as session:
        backup(session, owner, "done", "done", now())
    assert job_signals(db, config) == 0


def add_delivery(db, config, owner, identifier, age, state="review"):
    with db.transaction() as session:
        notice = NotificationService(db, config).emit_in(
            session, owner, "auto-" + identifier, "security", "Событие", "Текст", channel="inbox"
        )
        session.add(
            NotificationDelivery(
                id=identifier,
                notification_id=notice.id,
                channel="telegram",
                recipient="900000001",
                state=state,
                attempts=1,
                available_at=now() - age,
                last_error="telegram_rejected",
            )
        )


def delivery_values(db, identifier):
    with db.transaction() as session:
        row = session.get(NotificationDelivery, identifier)
        return {column.name: getattr(row, column.name) for column in row.__table__.columns}


def test_stale_delivery_reviews_are_acknowledged_without_resend(notification_context):  # noqa: F811
    db, config, (owner, _, _) = notification_context
    add_delivery(db, config, owner, "stale", timedelta(days=2))
    add_delivery(db, config, owner, "fresh", timedelta(hours=2))
    before = delivery_values(db, "stale")
    monitor = OperationalMonitor(db, config)
    assert monitor.snapshot()["counts"]["delivery"] == 2
    assert monitor.auto_acknowledge_deliveries() == 1
    assert monitor.auto_acknowledge_deliveries() == 0
    snapshot = monitor.snapshot()
    assert snapshot["counts"]["delivery"] == 1
    assert [item["reference"] for item in snapshot["items"]] == ["fresh"]
    assert delivery_values(db, "stale") == before
    with db.transaction() as session:
        marker = session.get(Setting, "delivery-review:stale")
        assert marker.value["actor_id"] == "system" and marker.value["automatic"] is True
        assert "900000001" not in str(marker.value)
        audits = session.scalars(select(Audit).where(Audit.target == "stale")).all()
        assert len(audits) == 1 and audits[0].actor_id is None
        assert session.scalar(select(func.count()).select_from(Audit).where(Audit.target == "fresh")) == 0


def test_changed_delivery_after_auto_acknowledge_is_a_signal_again(notification_context):  # noqa: F811
    db, config, (owner, _, _) = notification_context
    add_delivery(db, config, owner, "changed", timedelta(days=2))
    monitor = OperationalMonitor(db, config)
    assert monitor.auto_acknowledge_deliveries() == 1
    with db.transaction() as session:
        session.get(NotificationDelivery, "changed").attempts = 2
    assert monitor.snapshot()["counts"]["delivery"] == 1
    assert monitor.auto_acknowledge_deliveries() == 1
    assert monitor.snapshot()["counts"]["delivery"] == 0


def test_pending_deliveries_are_never_auto_acknowledged(notification_context):  # noqa: F811
    db, config, (owner, _, _) = notification_context
    add_delivery(db, config, owner, "pending", timedelta(days=2), state="pending")
    monitor = OperationalMonitor(db, config)
    assert monitor.auto_acknowledge_deliveries() == 0
    assert monitor.snapshot()["counts"]["delivery"] == 1
