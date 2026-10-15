import pytest
from sqlalchemy import select
from sunrise_core.errors import DomainError
from sunrise_infrastructure.db import Notification, NotificationDelivery
from sunrise_infrastructure.notifications import NotificationService

from tests.integration.test_v11_notifications import notification_context as notification_context


@pytest.mark.parametrize("owner_only", [False, True])
def test_revoked_staff_cannot_read_or_receive_sensitive_notifications(notification_context, owner_only):
    db, config, (owner, _, _) = notification_context
    sent = []
    service = NotificationService(db, config, telegram_transport=lambda *args: sent.append(args))
    with db.transaction() as session:
        item = service.emit_in(
            session,
            owner,
            "staff-private",
            "identity_review" if owner_only else "sale",
            "Служебное событие",
            "Закрытая информация",
            owner_only=owner_only,
        )
        identifier = item.id
        ordinary = service.emit_in(
            session, owner, "ordinary", "device_added", "Устройство", "Телефон", channel="inbox"
        )
        ordinary_id = ordinary.id
    config.owner_id = None
    config.admin_telegram_allowlist = []
    assert [item["id"] for item in service.list(owner)["items"]] == [ordinary_id]
    assert service.list(owner)["unread_count"] == 1
    with pytest.raises(DomainError) as failure:
        service.mark_read(owner, identifier)
    assert failure.value.status == 404
    service.mark_all_read(owner)
    service.deliver_pending()
    assert not sent
    with db.transaction() as session:
        assert session.get(Notification, identifier).read_at is None
        assert session.scalar(select(NotificationDelivery.state)) == "blocked"


def test_owner_only_scope_survives_owner_change(notification_context):
    db, config, (owner, _, _) = notification_context
    service = NotificationService(db, config)
    with db.transaction() as session:
        service.emit_in(
            session,
            owner,
            "owner-event",
            "custom_owner_event",
            "Владелец",
            "Личное событие",
            owner_only=True,
            channel="inbox",
        )
    config.owner_id = "201"
    assert service.list(owner) == {
        "items": [],
        "unread_count": 0,
        "total_count": 0,
        "next_offset": None,
        "previous_offset": None,
    }


def test_hidden_staff_items_do_not_consume_page_limit(notification_context):
    db, config, (owner, _, _) = notification_context
    service = NotificationService(db, config)
    with db.transaction() as session:
        ordinary = service.emit_in(
            session, owner, "ordinary-first", "device_added", "Устройство", "Телефон", channel="inbox"
        )
        identifier = ordinary.id
    for i in range(3):
        with db.transaction() as session:
            service.emit_in(session, owner, f"sale:{i}", "sale", "Оплата", "Тариф", channel="inbox")
    config.owner_id = None
    config.admin_telegram_allowlist = []
    assert [item["id"] for item in service.list(owner, limit=1)["items"]] == [identifier]


def test_audience_migration_preserves_existing_fields_and_classifies_legacy_items(notification_context):
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text

    db, _, (owner, _, _) = notification_context
    with db.engine.begin() as connection:
        connection.execute(text("ALTER TABLE v11_notifications DROP COLUMN audience"))
        for kind, target in [
            ("sale", None),
            ("identity_review", None),
            ("device_added", None),
            ("custom", "/admin#users"),
        ]:
            connection.execute(
                text(
                    "INSERT INTO v11_notifications (id,account_id,event_key,kind,title,body,severity,target,nodes,actions,created_at) VALUES (:kind,:account,:kind,:kind,'Title','Body','info',:target,'[]','[]',now())"
                ),
                {"kind": kind, "account": owner, "target": target},
            )
        before = connection.execute(
            text(
                "SELECT id,account_id,event_key,kind,title,body,severity,target,nodes::text,actions::text,created_at,read_at FROM v11_notifications ORDER BY id"
            )
        ).all()
        path = Path(__file__).resolve().parents[2] / "migrations/versions/0017_notification_audience.py"
        spec = importlib.util.spec_from_file_location("audience_migration", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()
        after = connection.execute(
            text(
                "SELECT id,account_id,event_key,kind,title,body,severity,target,nodes::text,actions::text,created_at,read_at FROM v11_notifications ORDER BY id"
            )
        ).all()
        assert before == after
        assert dict(connection.execute(text("SELECT kind,audience FROM v11_notifications")).all()) == {
            "sale": "staff",
            "identity_review": "owner",
            "device_added": "account",
            "custom": "staff",
        }
