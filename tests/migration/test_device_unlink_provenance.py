import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sunrise_infrastructure.db import Account, Device, DeviceUnlink, Job, Subscription

from tests.isolated_database import isolated_database


def test_expand_unlink_provenance_preserves_unknown_and_original_event_identity():
    with isolated_database("unlink_migration") as db:
        with db.transaction() as session:
            account = Account()
            session.add(account)
            session.flush()
            sub = Subscription(account_id=account.id, devices_limit=1)
            session.add(sub)
            session.flush()
            event_ids = []
            for index in range(3):
                device = Device(subscription_id=sub.id, name="Текущее имя не доказывает историческое")
                session.add(device)
                session.flush()
                event = DeviceUnlink(
                    account_id=account.id,
                    original_account_id=account.id,
                    subscription_id=sub.id,
                    device_id=device.id,
                )
                session.add(event)
                session.flush()
                event_ids.append(event.id)
                if index != 1:
                    session.add(
                        Job(
                            key=f"device-revoke:{device.id}:1",
                            kind="device_revoke",
                            account_id=account.id,
                            payload={
                                "subscription_id": sub.id if index == 0 else "wrong-subscription",
                                "device_id": device.id,
                                "generation": 1,
                                "unlink_mode": "bulk",
                                "unlink_operation": "proven-operation",
                            },
                        )
                    )
        with db.engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE v9_device_unlinks DROP CONSTRAINT ck_device_unlink_provenance")
            )
            for column in ("unlink_mode", "unlink_operation", "device_name"):
                connection.execute(text("ALTER TABLE v9_device_unlinks DROP COLUMN " + column))
            path = Path(__file__).resolve().parents[2] / "migrations/versions/0019_device_unlink_operation.py"
            spec = importlib.util.spec_from_file_location("unlink_migration", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            with Operations.context(MigrationContext.configure(connection)):
                module.upgrade()
            rows = {
                row.id: row
                for row in connection.execute(
                    text("SELECT id,unlink_mode,unlink_operation,device_name FROM v9_device_unlinks")
                )
            }
            assert set(rows) == set(event_ids)
            assert rows[event_ids[0]].unlink_mode == "bulk"
            assert rows[event_ids[0]].unlink_operation == "proven-operation"
            assert all(row.device_name is None for row in rows.values())
            assert all(
                rows[event_id].unlink_mode is None and rows[event_id].unlink_operation is None
                for event_id in event_ids[1:]
            )
