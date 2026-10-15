import pytest
from sqlalchemy import select
from sunrise_infrastructure.broadcasts import BroadcastService
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import Account, Job, Notification, NotificationDelivery
from test_v11_notifications import notification_context as notification_context


def service_for(context):
    db, settings, (owner, email, _) = context
    commerce = Commerce(db, local_test=True, public_settings=settings)
    documents = [
        {"document_type": kind, "version": "report", "url": f"https://example.test/{kind}"}
        for kind in ("offer", "privacy")
    ]
    commerce.legal.configure(None, documents, "Синтетическое согласие отчёта")
    with db.transaction() as session:
        for identifier in (owner, email):
            commerce.legal.accept_in(
                session, session.get(Account, identifier), documents, "website", "registration"
            )
    return BroadcastService(commerce, settings), owner


@pytest.mark.parametrize(
    "state,error,bucket",
    [
        ("delivered", None, "delivered_count"),
        ("blocked", "legal_consent_required", "blocked_count"),
        ("blocked", "telegram_forbidden", "provider_rejected_count"),
        ("review", "mail_rejected", "provider_rejected_count"),
        ("review", "delivery_outcome_unknown", "unknown_count"),
        ("pending", "telegram_retry_after", "waiting_count"),
        ("in_flight", None, "waiting_count"),
        ("cancelled", "broadcast_stopped", "cancelled_count"),
    ],
)
def test_report_distinguishes_primary_delivery_outcomes(notification_context, state, error, bucket):
    service, owner = service_for(notification_context)
    job = service.create(owner, [{"type": "text", "text": "Сообщение"}], "Синтетический отчёт", draft=False)
    service.expand_pending()
    with service.db.transaction() as session:
        rows = list(
            session.scalars(
                select(NotificationDelivery)
                .join(Notification)
                .where(Notification.event_key == "broadcast:" + job["id"])
            )
        )
        rows[0].state, rows[0].last_error = state, error
        rows[1].state = "pending"
    result = service.detail(owner, job["id"])
    expected = {
        key: 0
        for key in (
            "delivered_count",
            "blocked_count",
            "provider_rejected_count",
            "unknown_count",
            "waiting_count",
            "cancelled_count",
        )
    }
    expected["waiting_count"] = 1
    expected[bucket] += 1
    assert {key: result[key] for key in expected} == expected
    assert sum(result[key] for key in expected) == result["recipient_count"] == 2


def test_revoked_before_expansion_is_reported_blocked_not_lost(notification_context):
    service, owner = service_for(notification_context)
    job = service.create(owner, [{"type": "text", "text": "Сообщение"}], "Синтетический отчёт", draft=False)
    service.settings.email_allowlist = []
    service.expand_pending()
    result = service.detail(owner, job["id"])
    assert (result["blocked_count"], result["waiting_count"], result["recipient_count"]) == (1, 1, 2)


def test_automatic_report_waits_for_settled_delivery_and_emits_once(notification_context):
    service, owner = service_for(notification_context)
    job = service.create(owner, [{"type": "text", "text": "Сообщение"}], "Синтетический отчёт", draft=False)
    assert service.report_completed() == 0
    service.expand_pending()
    assert service.report_completed() == 0
    with service.db.transaction() as session:
        rows = list(
            session.scalars(
                select(NotificationDelivery)
                .join(Notification)
                .where(Notification.event_key == "broadcast:" + job["id"])
            )
        )
        rows[0].state = "delivered"
        rows[1].state, rows[1].last_error = "review", "delivery_outcome_unknown"
    assert service.report_completed() == 1
    assert service.report_completed() == 0
    with service.db.transaction() as session:
        report = session.scalar(select(Notification).where(Notification.kind == "broadcast_report"))
        assert report.account_id == owner and report.audience == "owner"
        assert "Доставлено: 1" in report.body and "Неизвестный результат: 1" in report.body
        assert session.get(Job, job["id"]).payload["report_state"] == "queued"


def test_automatic_report_denies_owner_with_revoked_telegram_authority(notification_context):
    service, owner = service_for(notification_context)
    job = service.create(owner, [{"type": "text", "text": "Сообщение"}], "Синтетический отчёт", draft=False)
    service.stop(owner, job["id"], "Синтетическая остановка")
    service.settings.owner_id = None
    service.settings.admin_telegram_allowlist = []
    assert service.report_completed() == 0
    with service.db.transaction() as session:
        assert not session.scalar(select(Notification.id).where(Notification.kind == "broadcast_report"))
        assert session.get(Job, job["id"]).payload["report_state"] == "owner_unavailable"


def test_waiting_first_broadcast_does_not_starve_completed_report(notification_context):
    service, owner = service_for(notification_context)
    service.create(owner, [{"type": "text", "text": "Первое"}], "Синтетический отчёт", draft=False)
    second = service.create(owner, [{"type": "text", "text": "Второе"}], "Синтетический отчёт", draft=False)
    service.expand_pending()
    with service.db.transaction() as session:
        for row in session.scalars(
            select(NotificationDelivery)
            .join(Notification)
            .where(Notification.event_key == "broadcast:" + second["id"])
        ):
            row.state = "delivered"
    assert service.report_completed(limit=1) == 1


def test_report_and_admin_detail_use_consistent_owner_job_lock_order(notification_context, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    service, owner = service_for(notification_context)
    job = service.create(owner, [{"type": "text", "text": "Сообщение"}], "Синтетический отчёт", draft=False)
    service.expand_pending()
    with service.db.transaction() as session:
        for row in session.scalars(
            select(NotificationDelivery)
            .join(Notification)
            .where(Notification.event_key == "broadcast:" + job["id"])
        ):
            row.state = "delivered"
    emit_started, actor_locked = Event(), Event()
    original_emit, original_require = service.notifications.emit_in, service.staff.require_in

    def emit(*args, **kwargs):
        emit_started.set()
        actor_locked.wait(1)
        return original_emit(*args, **kwargs)

    def require(*args, **kwargs):
        result = original_require(*args, **kwargs)
        actor_locked.set()
        return result

    monkeypatch.setattr(service.notifications, "emit_in", emit)
    monkeypatch.setattr(service.staff, "require_in", require)

    def detail():
        assert emit_started.wait(5)
        return service.detail(owner, job["id"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        report, read = pool.submit(service.report_completed), pool.submit(detail)
        assert report.result(timeout=15) == 1
        assert read.result(timeout=15)["report_state"] == "queued"
