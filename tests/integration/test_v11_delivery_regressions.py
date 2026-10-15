from datetime import timedelta

import pytest
from sqlalchemy import select
from sunrise_core.errors import DomainError
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import (
    Account,
    Job,
    Notification,
    NotificationDelivery,
    NotificationPreference,
    PushSubscription,
    StaffRole,
    now,
)
from sunrise_infrastructure.notifications import NotificationService
from test_v11_notifications import notification_context as notification_context

from tests.integration.test_subscription_engagement import paid_creation as paid_creation
from tests.integration.test_v11_panel_events import owned_panel as owned_panel


def test_queued_broadcast_new_legal_revision_blocks_transport(notification_context):
    from sunrise_infrastructure.broadcasts import BroadcastService

    db, settings, (owner, _, _) = notification_context
    commerce = Commerce(db, local_test=True, public_settings=settings)
    documents = [
        {"document_type": key, "version": "v1", "url": f"https://example.test/{key}"}
        for key in ("offer", "privacy")
    ]
    commerce.legal.configure(None, documents, "Синтетические документы")
    with db.transaction() as session:
        commerce.legal.accept_in(session, session.get(Account, owner), documents, "website", "registration")
    broadcasts = BroadcastService(commerce, settings)
    broadcasts.create(owner, [{"type": "text", "text": "Сообщение"}], "Синтетическая рассылка", draft=False)
    broadcasts.expand_pending()
    commerce.legal.configure(None, [{**d, "version": "v2"} for d in documents], "Смена документов")
    sent = []
    NotificationService(db, settings, telegram_transport=lambda *args: sent.append(args)).deliver_pending()
    with db.transaction() as session:
        delivery = session.scalar(select(NotificationDelivery))
        assert (delivery.state, delivery.last_error) == ("blocked", "legal_consent_required")
    assert sent == []


@pytest.mark.parametrize("merge", [False, True])
def test_forbidden_old_telegram_does_not_block_changed_binding(notification_context, merge):
    db, settings, (owner, other, _) = notification_context
    service = NotificationService(db, settings)
    with db.transaction() as session:
        item = service.emit_in(session, owner, "forbidden-race", "security", "Вход", "Проверка")
        item_id = item.id

    def forbidden(*args):
        with db.transaction() as session:
            account = session.get(Account, owner)
            if merge:
                session.get(Notification, item_id).account_id = other
                session.flush()
                grant = session.get(StaffRole, owner)
                if grant is not None:
                    session.delete(grant)
                    session.flush()
                session.delete(account)
            else:
                account.telegram_id = 1070400000
        raise DomainError("telegram_forbidden", "Бот заблокирован", 503)

    service.telegram_transport = forbidden
    service.deliver_pending()
    with db.transaction() as session:
        account = session.get(Account, other if merge else owner)
        assert account.telegram_notifications_blocked is False
        assert session.scalar(select(NotificationDelivery.state)) == "blocked"


def test_expired_delivery_claim_is_reviewed_without_resending(notification_context):
    db, settings, (owner, _, _) = notification_context
    service = NotificationService(db, settings)
    with db.transaction() as session:
        item = service.emit_in(session, owner, "restart-expired", "security", "Вход", "Проверка")
        session.flush()
        delivery = session.scalar(
            select(NotificationDelivery).where(NotificationDelivery.notification_id == item.id)
        )
        delivery.state = "in_flight"
        delivery.available_at = now() - timedelta(seconds=1)
        delivery.attempts = 1
    sent = []
    service.telegram_transport = lambda *args: sent.append(args)
    service.deliver_pending()
    service.deliver_pending()
    with db.transaction() as session:
        delivery = session.scalar(select(NotificationDelivery))
        assert (delivery.state, delivery.last_error, delivery.attempts) == (
            "review",
            "delivery_outcome_unknown",
            1,
        )
    assert sent == []


def test_active_delivery_claim_is_not_recovered_early(notification_context):
    db, settings, (owner, _, _) = notification_context
    service = NotificationService(db, settings)
    with db.transaction() as session:
        service.emit_in(session, owner, "restart-active", "security", "Вход", "Проверка")
        session.flush()
        delivery = session.scalar(select(NotificationDelivery))
        delivery.state, delivery.available_at = "in_flight", now() + timedelta(minutes=5)
    service.deliver_pending()
    with db.transaction() as session:
        assert session.scalar(select(NotificationDelivery.state)) == "in_flight"


@pytest.mark.parametrize("deadline", [None, "expired", "invalid"])
def test_expired_backup_claim_is_reviewed_without_dump_or_send(notification_context, deadline):
    from sunrise_infrastructure.scheduler import Scheduler

    db, settings, (owner, _, _) = notification_context
    payload = {"recipient": settings.owner_id, "database": db.engine.url.database}
    if deadline:
        payload["claim_expires_at"] = (
            (now() - timedelta(seconds=1)).isoformat() if deadline == "expired" else "invalid"
        )
    with db.transaction() as session:
        session.add(
            Job(
                key="backup-restart",
                kind="daily_backup",
                account_id=owner,
                state="in_flight",
                attempts=1,
                payload=payload,
            )
        )
    calls = []
    scheduler = Scheduler(
        Commerce(db, local_test=True, public_settings=settings),
        settings,
        dump_database=lambda *args: calls.append("dump"),
        send_document=lambda *args: calls.append("send"),
    )
    scheduler.deliver_backups()
    scheduler.deliver_backups()
    with db.transaction() as session:
        job = session.scalar(select(Job).where(Job.kind == "daily_backup"))
        assert (job.state, job.last_error, job.attempts) == ("review", "backup_outcome_unknown", 1)
    assert calls == []


def test_disabling_browser_preference_cancels_pending_push_and_mount_keeps_opt_out(notification_context):
    import base64

    db, settings, (owner, _, _) = notification_context
    settings.push_private_key = "synthetic-not-used"
    settings.push_test_authorized = True
    service = NotificationService(db, settings)
    value = {
        "endpoint": "https://fcm.googleapis.com/synthetic-test-only",
        "keys": {
            "p256dh": base64.urlsafe_b64encode(b"\x04" + b"a" * 64).decode(),
            "auth": base64.urlsafe_b64encode(b"a" * 16).decode(),
        },
    }
    subscription = service.register_push(owner, value)
    with db.transaction() as session:
        service.emit_in(session, owner, "push-opt-out", "security", "Вход", "Проверка", channel="inbox")
    assert service.set_browser_preference(owner, False) == {"browser_enabled": False}
    service.register_push(owner, value)
    with db.transaction() as session:
        assert session.get(NotificationPreference, owner).browser_enabled is False
        push = session.scalar(select(NotificationDelivery).where(NotificationDelivery.channel == "push"))
        assert push.state == "cancelled"
        assert session.get(PushSubscription, subscription["id"]).disabled_at is None


def test_push_claim_requires_current_preference_and_same_account(notification_context):
    db, settings, (owner, other, _) = notification_context
    settings.push_test_authorized = True
    service = NotificationService(db, settings)
    with db.transaction() as session:
        session.add(NotificationPreference(account_id=owner, browser_enabled=True))
        row = PushSubscription(
            account_id=other, endpoint_digest="foreign-endpoint", subscription_ciphertext="not-used"
        )
        session.add(row)
        item = service.emit_in(
            session, owner, "foreign-push", "security", "Вход", "Проверка", channel="inbox"
        )
        session.flush()
        session.add(NotificationDelivery(notification_id=item.id, channel="push", recipient=row.id))
    sent = []
    service.push_transport = lambda *args: sent.append(args)
    service.deliver_pending()
    with db.transaction() as session:
        assert session.scalar(select(NotificationDelivery.state)) == "blocked"
    assert sent == []


@pytest.mark.parametrize("kind", ["broadcast", "support"])
def test_long_telegram_message_rejected_before_queue_creation(notification_context, kind):
    db, settings, (owner, other, _) = notification_context
    commerce = Commerce(db, local_test=True, public_settings=settings)
    nodes = [{"type": "text", "text": "x" * 3000}, {"type": "text", "text": "y" * 2000}]
    with pytest.raises(DomainError) as failure:
        if kind == "broadcast":
            from sunrise_infrastructure.broadcasts import BroadcastService

            BroadcastService(commerce, settings).create(owner, nodes, "Синтетическая рассылка", draft=False)
        else:
            from sunrise_infrastructure.admin_operations import AdminOperations

            AdminOperations(commerce, settings).message(
                owner, other, nodes, "long-message", "Синтетическое сообщение", confirmed=True
            )
    assert failure.value.code == "telegram_message_too_long"
    with db.transaction() as session:
        assert session.scalar(select(Notification.id)) is None


def test_telegram_budget_counts_text_after_entities_and_formatting():
    from sunrise_infrastructure.notifications import validate_telegram_content

    rendered = validate_telegram_content(
        "A", "&" * 4093, [{"type": "bold", "children": [{"type": "text", "text": "&" * 4093}]}]
    )
    assert rendered.startswith("A\n\n<b>&amp;")
    with pytest.raises(DomainError) as failure:
        validate_telegram_content("AB", "&" * 4093)
    assert failure.value.code == "telegram_message_too_long"


def test_renewed_subscription_cancels_queued_expiry_reminder(paid_creation):
    from sunrise_infrastructure.scheduler import Scheduler

    db, panel, commerce, settings, state, _, owner, sub_id, _ = paid_creation
    assert commerce.provisioning.run_jobs() == 1
    state["user"]["expireAt"] = (now() + timedelta(hours=12)).isoformat()
    commerce.dashboard(owner, refresh_panel=True)
    scheduler = Scheduler(commerce, settings)
    assert scheduler.remind() == 1
    with db.transaction() as session:
        obsolete = session.scalar(
            select(Notification).where(
                Notification.account_id == owner, Notification.kind == "subscription_expiry"
            )
        )
        assert obsolete is not None
        delivery_id = session.scalar(
            select(NotificationDelivery.id).where(NotificationDelivery.notification_id == obsolete.id)
        )
        obsolete_id = obsolete.id
        assert delivery_id is not None
    quote = commerce.quote(owner, "standard", 30, "renew", sub_id)
    order = commerce.create_order(owner, quote["id"], "queued-reminder-paid-renewal")
    commerce.test_pay(owner, order["id"])
    assert commerce.provisioning.run_jobs() == 1
    scheduler.notify_ready()
    with db.transaction() as session:
        confirmation_id = session.scalar(
            select(Notification.id).where(
                Notification.event_key == "access-ready:" + order["id"],
                Notification.kind == "subscription_renewed",
            )
        )
        assert confirmation_id is not None
    calls = []
    panel_calls = list(state["calls"])
    NotificationService(
        db,
        settings,
        telegram_transport=lambda recipient, payload, identifier: calls.append(payload["notification_id"]),
    ).deliver_pending()
    with db.transaction() as session:
        assert session.get(NotificationDelivery, delivery_id).state == "cancelled"
    assert obsolete_id not in calls and confirmation_id in calls
    assert state["calls"] == panel_calls


def test_only_definitive_telegram_429_schedules_bounded_retry(notification_context, monkeypatch):
    import httpx
    from sunrise_infrastructure import notifications

    db, settings, (owner, _, _) = notification_context
    settings.bot_test_token = "700002:synthetic"  # noqa: S105 -- fake token, HTTP MockTransport only
    settings.test_bot_ids = "700002"

    def response(request):
        if request.url.path.endswith("getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"id": 700002}})
        return httpx.Response(429, json={"ok": False, "error_code": 429, "parameters": {"retry_after": 10}})

    real_client = httpx.Client
    monkeypatch.setattr(
        notifications.httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(response), **kwargs),
    )
    service = NotificationService(db, settings)
    with db.transaction() as session:
        service.emit_in(session, owner, "telegram-rate-limit", "security", "Вход", "Проверка")
    before = now()
    service.deliver_pending(limit=1)
    with db.transaction() as session:
        row = session.scalar(select(NotificationDelivery))
        assert row.state == "pending" and row.last_error == "telegram_rate_limited"
        assert before + timedelta(seconds=10) <= row.available_at <= now() + timedelta(seconds=10)
        row.attempts = 3
        row.available_at = now() - timedelta(seconds=1)
    service.deliver_pending(limit=1)
    with db.transaction() as session:
        assert session.scalar(select(NotificationDelivery.state)) == "review"


def test_backup_retention_deletes_only_old_confirmed_own_dump(notification_context, tmp_path, monkeypatch):
    from uuid import uuid4

    from sunrise_infrastructure import scheduler as module
    from sunrise_infrastructure.scheduler import Scheduler

    db, settings, (owner, _, _) = notification_context
    root = tmp_path / "workspace"
    monkeypatch.setattr(
        module, "__file__", str(root / "packages/infrastructure/src/sunrise_infrastructure/scheduler.py")
    )
    folder = root / ".private/backups/owner-daily"
    folder.mkdir(parents=True)
    identifiers = [str(uuid4()) for _ in range(4)]
    files = [folder / (identifier + ".dump") for identifier in identifiers]
    outside = root / ".private/backups/manual.dump"
    outside.write_bytes(b"synthetic manual")
    unknown = folder / "untracked.dump"
    unknown.write_bytes(b"unknown")
    with db.transaction() as session:
        for index, identifier in enumerate(identifiers):
            files[index].write_bytes(b"synthetic dump")
            session.add(
                Job(
                    id=identifier,
                    key="retention:" + identifier,
                    kind="daily_backup",
                    account_id=owner,
                    state=("done", "review", "in_flight", "done")[index],
                    payload={"completed_at": (now() - timedelta(days=8 if index < 3 else 1)).isoformat()},
                )
            )
    result = Scheduler(Commerce(db, local_test=True, public_settings=settings), settings).cleanup_backups()
    assert result == 1 and not files[0].exists()
    assert all(path.exists() for path in files[1:]) and outside.exists() and unknown.exists()


def test_backup_retention_rejects_path_traversal_and_keeps_active_claim(
    notification_context, tmp_path, monkeypatch
):
    from sunrise_infrastructure import scheduler as module
    from sunrise_infrastructure.scheduler import Scheduler

    db, settings, (owner, _, _) = notification_context
    root = tmp_path / "workspace"
    monkeypatch.setattr(
        module, "__file__", str(root / "packages/infrastructure/src/sunrise_infrastructure/scheduler.py")
    )
    folder = root / ".private/backups/owner-daily"
    folder.mkdir(parents=True)
    outside = folder.parent / "outside.dump"
    outside.write_bytes(b"synthetic outside")
    with db.transaction() as session:
        session.add(
            Job(
                id="../outside",
                key="unsafe-dump",
                kind="daily_backup",
                account_id=owner,
                state="done",
                payload={"completed_at": (now() - timedelta(days=8)).isoformat()},
            )
        )
        session.add(
            Job(
                key="active-backup",
                kind="daily_backup",
                account_id=owner,
                state="in_flight",
                payload={"claim_expires_at": (now() + timedelta(minutes=5)).isoformat()},
            )
        )
    scheduler = Scheduler(Commerce(db, local_test=True, public_settings=settings), settings)
    assert scheduler.cleanup_backups() == 0 and outside.read_bytes() == b"synthetic outside"
    scheduler.deliver_backups()
    with db.transaction() as session:
        assert session.scalar(select(Job.state).where(Job.key == "active-backup")) == "in_flight"


def test_network_unknown_delivery_is_never_automatically_retried(notification_context):
    db, settings, (owner, _, _) = notification_context
    calls = []

    def unknown(*args):
        calls.append(args)
        raise TimeoutError("synthetic unknown outcome")

    service = NotificationService(db, settings, telegram_transport=unknown)
    with db.transaction() as session:
        service.emit_in(session, owner, "network-unknown", "security", "Вход", "Проверка")
    service.deliver_pending()
    service.deliver_pending()
    with db.transaction() as session:
        row = session.scalar(select(NotificationDelivery))
        assert (row.state, row.last_error, row.attempts) == ("review", "delivery_outcome_unknown", 1)
    assert len(calls) == 1


def test_current_telegram_forbidden_persists_contactability_block(notification_context):
    db, settings, (owner, _, _) = notification_context

    def forbidden(*args):
        raise DomainError("telegram_forbidden", "Бот заблокирован", 503)

    service = NotificationService(db, settings, telegram_transport=forbidden)
    with db.transaction() as session:
        service.emit_in(session, owner, "current-forbidden", "security", "Вход", "Проверка")
    service.deliver_pending()
    with db.transaction() as session:
        assert session.get(Account, owner).telegram_notifications_blocked is True
