import importlib.util
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema, DropSchema
from sunrise_infrastructure.db import Account, Base, Database, StaffRole


@pytest.fixture
def notification_context():
    url = os.getenv("SUNRISE_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Synthetic PostgreSQL required")
    parsed = make_url(url)
    assert parsed.database == "sunrise_test_backend" and parsed.host in {"127.0.0.1", "localhost"}
    name = "v11_notify_" + uuid4().hex
    base = Database(url)
    with base.engine.begin() as connection:
        connection.execute(CreateSchema(name))
    db = Database(
        parsed.set(query={"options": "-csearch_path=" + name}).render_as_string(hide_password=False)
    )
    Base.metadata.create_all(db.engine)
    config = SimpleNamespace(
        local_test=True,
        secret_key="n" * 48,
        owner_id="202",
        admin_telegram_allowlist=[],
        admin_email_allowlist=["owner@example.test"],
        email_allowlist=["owner@example.test", "email@example.test"],
        test_telegram_recipients="202,201",
        bot_test_token=None,
        push_private_key=None,
        dashboard_url="http://127.0.0.1:3200",
    )
    with db.transaction() as session:
        accounts = [
            Account(email="owner@example.test", email_verified=True, telegram_id=202),
            Account(email="email@example.test", email_verified=True),
            Account(email="private@example.test", email_verified=True, telegram_id=201),
        ]
        session.add_all(accounts)
        session.flush()
        ids = [account.id for account in accounts]
        session.add(StaffRole(account_id=ids[0], role="owner", granted_by=ids[0]))
    try:
        yield db, config, ids
    finally:
        db.engine.dispose()
        with base.engine.begin() as connection:
            connection.execute(DropSchema(name, cascade=True))
        base.engine.dispose()


def notification_service(context):
    assert importlib.util.find_spec("sunrise_infrastructure.notifications") is not None, (
        "Durable notifications absent"
    )
    from sunrise_infrastructure.notifications import NotificationService

    return NotificationService(context[0], context[1])


def test_one_event_has_one_inbox_item_and_one_preferred_channel(notification_context):
    service = notification_service(notification_context)
    db, _, (owner, _, _) = notification_context
    with db.transaction() as session:
        service.emit_in(
            session, owner, "synthetic-device:1", "device_added", "Новое устройство", "Подключён телефон"
        )
        service.emit_in(
            session, owner, "synthetic-device:1", "device_added", "Новое устройство", "Подключён телефон"
        )
    result = service.list(owner)
    assert result["unread_count"] == 1 and len(result["items"]) == 1
    from sunrise_infrastructure.db import NotificationDelivery

    with db.transaction() as session:
        channels = list(session.scalars(select(NotificationDelivery.channel)))
    assert channels == ["telegram"]


def test_email_only_account_uses_email_and_foreign_read_never_changes_inbox(notification_context):
    service = notification_service(notification_context)
    db, _, (owner, email_only, _) = notification_context
    with db.transaction() as session:
        item = service.emit_in(
            session,
            email_only,
            "synthetic-payment:1",
            "payment_received",
            "Оплата получена",
            "Готовим доступ",
        )
        identifier = item.id
    from sunrise_core.errors import DomainError

    with pytest.raises(DomainError) as failure:
        service.mark_read(owner, identifier)
    assert failure.value.status == 404
    assert service.list(email_only)["unread_count"] == 1
    service.mark_read(email_only, identifier)
    service.mark_read(email_only, identifier)
    assert service.list(email_only)["unread_count"] == 0
    from sunrise_infrastructure.db import NotificationDelivery

    with db.transaction() as session:
        assert list(session.scalars(select(NotificationDelivery.channel))) == ["email"]


def test_unapproved_recipient_stays_in_inbox_but_delivery_is_denied(notification_context):
    service = notification_service(notification_context)
    db, config, (_, _, target) = notification_context
    config.test_telegram_recipients = "202"
    with db.transaction() as session:
        service.emit_in(
            session, target, "synthetic-alert:1", "security", "Новый вход", "Неизвестное устройство"
        )
    assert service.list(target)["unread_count"] == 1
    calls = []
    service.telegram_transport = lambda *args, **kwargs: calls.append((args, kwargs))
    service.deliver_pending()
    assert calls == []
    from sunrise_infrastructure.db import NotificationDelivery

    with db.transaction() as session:
        assert session.scalar(select(NotificationDelivery.state)) == "blocked"
