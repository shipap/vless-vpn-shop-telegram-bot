from datetime import UTC, datetime
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sunrise_infrastructure.db import Account, DeviceUnlink
from sunrise_infrastructure.moderation import DeviceModeration


def test_completed_bulk_counts_once_at_first_success_and_preserves_unknown_history():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    DeviceUnlink.__table__.create(engine)
    with Session(engine) as session:
        for index, (stamp, mode, operation) in enumerate(
            [
                (datetime(2031, 1, 31, 20, 59, tzinfo=UTC), "bulk", "bulk-one"),
                (datetime(2031, 1, 31, 21, 0, tzinfo=UTC), "bulk", "bulk-one"),
                (datetime(2031, 2, 1, 1, 0, tzinfo=UTC), "single", "single-one"),
                (datetime(2031, 2, 1, 2, 0, tzinfo=UTC), None, None),
            ]
        ):
            session.add(
                DeviceUnlink(
                    account_id=102,
                    original_account_id=102,
                    subscription_id="sub",
                    device_id=str(index),
                    generation=1,
                    completed_at=stamp,
                    unlink_mode=mode,
                    unlink_operation=operation,
                )
            )
        session.flush()
        account = Account(id=102, unlink_warnings=0, unlink_warnings_total=2, unlink_blocked=True)
        result = DeviceModeration(SimpleNamespace(db=None)).state(
            session, account, datetime(2031, 2, 2, tzinfo=UTC)
        )
        assert result["unlink_total"] == 4 and result["unlink_month"] == 3
        assert result["unlink_individual_total"] == result["unlink_individual_month"] == 1
        assert result["unlink_bulk_total"] == 1 and result["unlink_bulk_month"] == 0
        assert result["unlink_unknown_total"] == result["unlink_unknown_month"] == 1
    engine.dispose()
