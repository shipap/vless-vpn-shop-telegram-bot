import pytest
from sqlalchemy import select
from sunrise_infrastructure.db import Account, Device, DeviceUnlink, Job, Subscription
from test_v11_customer_operations import operations as operations
from test_v11_panel_events import owned_panel as owned_panel


@pytest.mark.parametrize("bulk", [False, True])
def test_owner_notice_has_confirmed_device_hwid_without_owner_approval(operations, bulk):
    ops, commerce, (db, _, _, _, aid, sid, _) = operations
    with db.transaction() as session:
        account = session.get(Account, aid)
        account.telegram_username = "synthetic_customer"
        expiry = session.get(Subscription, sid).expires_at
        devices = list(session.scalars(select(Device).order_by(Device.id)))
        identities = {device.id: (device.panel_hwid, device.generation) for device in devices}
    selected = list(identities) if bulk else list(identities)[:1]
    ops.revoke_devices(aid, sid, selected, "own-confirmed-unlink", confirmed=True)
    with db.transaction() as session:
        assert not list(
            session.scalars(
                select(Job).where(
                    Job.kind == "owner_device_unlink",
                    Job.account_id == aid,
                    Job.payload["device_id"].as_string().in_(selected),
                )
            )
        )
    assert commerce.provisioning.run_jobs() == len(selected)
    commerce.provisioning.run_jobs()
    with db.transaction() as session:
        notices = list(
            session.scalars(
                select(Job).where(
                    Job.kind == "owner_device_unlink",
                    Job.account_id == aid,
                    Job.payload["device_id"].as_string().in_(selected),
                )
            )
        )
        assert len(notices) == len(selected)
        assert len(
            list(
                session.scalars(
                    select(DeviceUnlink).where(
                        DeviceUnlink.account_id == aid,
                        DeviceUnlink.subscription_id == sid,
                        DeviceUnlink.device_id.in_(selected),
                    )
                )
            )
        ) == len(selected)
        for notice in notices:
            payload = notice.payload
            assert payload["device_id"] in selected
            assert (payload["hwid"], payload["generation"]) == identities[payload["device_id"]]
            assert payload["account_id"] == str(aid)
            assert payload["telegram_username"] == "synthetic_customer"
        assert session.get(Subscription, sid).expires_at == expiry


@pytest.mark.parametrize("selected_count,operation_mode", [(1, None), (2, None), (1, "bulk")])
def test_first_confirmed_unlink_preserves_operation_metadata_and_reused_jobs(
    operations, selected_count, operation_mode
):
    ops, commerce, (db, _, _, _, aid, sid, _) = operations
    selected = [item["id"] for item in ops.inventory(aid, sid)["devices"]][:selected_count]
    result = ops.revoke_devices(
        aid, sid, selected, "operation-origin", confirmed=True, operation_mode=operation_mode
    )
    expected = operation_mode or ("bulk" if selected_count > 1 else "single")
    with db.transaction() as session:
        first_payloads = {jid: dict(session.get(Job, jid).payload) for jid in result["job_ids"]}
        assert all(payload["unlink_mode"] == expected for payload in first_payloads.values())
        assert len({payload["unlink_operation"] for payload in first_payloads.values()}) == 1
    ops.revoke_devices(aid, sid, selected, "overlapping-operation", confirmed=True, operation_mode="bulk")
    with db.transaction() as session:
        assert {jid: session.get(Job, jid).payload for jid in result["job_ids"]} == first_payloads
    assert commerce.provisioning.run_jobs() == selected_count
    with db.transaction() as session:
        notices = list(
            session.scalars(
                select(Job).where(
                    Job.kind == "owner_device_unlink",
                    Job.account_id == aid,
                    Job.payload["device_id"].as_string().in_(selected),
                )
            )
        )
        assert len(notices) == selected_count
        assert {notice.payload["unlink_mode"] for notice in notices} == {expected}
        assert len({notice.payload["unlink_operation"] for notice in notices}) == 1
