import pytest
from sqlalchemy import select
from sunrise_infrastructure.db import Account, DeviceUnlink, Job
from sunrise_infrastructure.moderation import DeviceModeration

from tests.integration.test_v11_customer_operations import operations as operations
from tests.integration.test_v11_panel_events import owned_panel  # noqa: F401 -- fixture dependency


@pytest.mark.parametrize(
    "count,mode,want_single,want_bulk", [(1, None, 1, 0), (2, None, 0, 1), (1, "bulk", 0, 1)]
)
def test_successful_job_persists_original_unlink_group_and_counts_replay_once(
    operations, count, mode, want_single, want_bulk
):  # noqa: F811 -- pytest fixture
    ops, commerce, (db, _, _, _, aid, sid, _) = operations
    devices = ops.inventory(aid, sid)["devices"][:count]
    result = ops.revoke_devices(
        aid, sid, [item["id"] for item in devices], "confirmed-origin", confirmed=True, operation_mode=mode
    )
    with db.transaction() as session:
        assert session.scalar(select(DeviceUnlink.id)) is None
    assert commerce.provisioning.run_jobs() == count
    assert commerce.provisioning.run_jobs() == 0
    assert (
        ops.revoke_devices(
            aid,
            sid,
            [item["id"] for item in devices],
            "confirmed-origin",
            confirmed=True,
            operation_mode=mode,
        )
        == result
    )
    with db.transaction() as session:
        events = list(session.scalars(select(DeviceUnlink).where(DeviceUnlink.account_id == aid)))
        assert len(events) == count
        assert {event.unlink_mode for event in events} == {"bulk" if want_bulk else "single"}
        assert len({event.unlink_operation for event in events}) == 1
        assert all(event.device_name for event in events)
        state = DeviceModeration(commerce).state(session, session.get(Account, aid))
        assert state["unlink_individual_total"] == state["unlink_individual_month"] == want_single
        assert state["unlink_bulk_total"] == state["unlink_bulk_month"] == want_bulk
        assert state["unlink_unknown_total"] == 0
        assert all(session.get(Job, jid).state == "done" for jid in result["job_ids"])


def test_partial_bulk_with_one_confirmed_removal_counts_once(operations):  # noqa: F811 -- pytest fixture
    ops, commerce, (db, _, _, _, aid, sid, _) = operations
    devices = ops.inventory(aid, sid)["devices"]
    ops.revoke_devices(aid, sid, [item["id"] for item in devices], "partial-bulk", confirmed=True)
    assert commerce.provisioning.run_jobs(limit=1) == 1
    with db.transaction() as session:
        state = DeviceModeration(commerce).state(session, session.get(Account, aid))
        assert state["unlink_total"] == 1 and state["unlink_bulk_total"] == 1
    assert commerce.provisioning.run_jobs(limit=1) == 1
    with db.transaction() as session:
        state = DeviceModeration(commerce).state(session, session.get(Account, aid))
        assert state["unlink_total"] == 2 and state["unlink_bulk_total"] == 1
