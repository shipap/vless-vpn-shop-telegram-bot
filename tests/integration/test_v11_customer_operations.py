"""Customer commands never affect other subscriptions or paid rights."""

import importlib
from datetime import timedelta

import fakeredis
import pytest
from sqlalchemy import select
from sunrise_core.errors import DomainError
from sunrise_infrastructure.account_features import AccountsFeatures
from sunrise_infrastructure.accounts import Accounts
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import (
    Account,
    Audit,
    Device,
    DeviceUnlink,
    Job,
    LegacyIssue,
    PanelObservation,
    Session,
    Subscription,
    now,
)
from sunrise_infrastructure.security import RateGuard, digest

from tests.e2e.local_auth_server import SyntheticSettings
from tests.integration.test_v11_panel_events import (  # noqa: F401 - pytest fixture
    device,
    owned_panel,
    service,
)
from tests.legal_fixture import accept_test_legal


@pytest.fixture
def operations(owned_panel):  # noqa: F811 - imported pytest fixture
    db, panel, worker, state, aid, sid, jid = owned_panel
    assert worker.run_jobs() == 1
    commerce = Commerce(db, local_test=True, panel_provider=panel)
    accept_test_legal(commerce, aid)
    events = service(db, panel)
    state["devices"] = [device("SYNTHETIC-01"), device("SYNTHETIC-02")]
    events.reconcile_owned(sid, initial=True)
    cls = importlib.import_module("sunrise_infrastructure.customer_operations").CustomerOperations
    value = cls(commerce, panel_events=events)
    return value, commerce, owned_panel


def test_v11_inventory_has_owned_string_counts_and_nullable_observation(operations):
    operations, commerce, owned = operations
    db, panel, worker, state, aid, sid, jid = owned
    result = operations.inventory(aid, sid)
    assert result["traffic_bytes"] == "42" and result["paid_expires_at"]
    assert len(result["devices"]) == 2 and result["devices"][0]["generation"] == 1
    assert "hwid" not in result["devices"][0]
    with db.transaction() as s:
        observation = s.scalar(select(PanelObservation))
        observation.traffic_bytes = None
    assert operations.inventory(aid, sid)["traffic_bytes"] is None


def test_v11_customer_bulk_enqueues_each_generation_once_and_preserves_expiry(operations):
    operations, commerce, owned = operations
    db, panel, worker, state, aid, sid, jid = owned
    with db.transaction() as s:
        ids = list(s.scalars(select(Device.id).order_by(Device.id)))
        expiry = s.get(Subscription, sid).expires_at
    calls = len(state["calls"])
    result = operations.revoke_devices(aid, sid, ids, "synthetic-bulk-1", confirmed=True)
    assert result["status"] == "revoke_pending" and len(result["job_ids"]) == 2
    assert (
        operations.revoke_devices(aid, sid, list(reversed(ids)), "synthetic-bulk-1", confirmed=True) == result
    )
    assert len(state["calls"]) == calls
    with db.transaction() as s:
        jobs = list(s.scalars(select(Job).where(Job.kind == "device_revoke")))
        assert len(jobs) == 2 and all(job.payload["generation"] == 1 for job in jobs)
        assert all(not s.get(Device, did).revoked for did in ids)
        assert s.get(Subscription, sid).expires_at == expiry


@pytest.mark.parametrize("mode", ["missing", "extra", "bool", "zero", "negative", "float", "string", "list"])
def test_v11_expected_generations_exact_members_and_positive_integers_are_required(operations, mode):
    ops, commerce, owned = operations
    db, _, _, state, aid, sid, _ = owned
    devices = ops.inventory(aid, sid)["devices"]
    ids = [device["id"] for device in devices]
    expected = {device["id"]: device["generation"] for device in devices}
    if mode == "missing":
        expected.pop(ids[0])
    elif mode == "extra":
        expected["foreign-device"] = 1
    elif mode == "list":
        expected = [1, 1]
    else:
        expected[ids[0]] = {"bool": True, "zero": 0, "negative": -1, "float": 1.0, "string": "1"}[mode]
    before = len(state["calls"])
    with pytest.raises(DomainError) as failure:
        ops.revoke_devices(aid, sid, ids, "invalid-expected", confirmed=True, expected_generations=expected)
    assert failure.value.code == "device_selection_invalid" and failure.value.status == 400
    assert len(state["calls"]) == before
    with db.transaction() as session:
        assert not list(session.scalars(select(Job).where(Job.kind == "device_revoke")))


def test_v11_stale_generation_in_batch_rejects_every_device_before_any_job(operations):
    ops, commerce, owned = operations
    db, _, _, state, aid, sid, _ = owned
    snapshot = ops.inventory(aid, sid)
    ids = [device["id"] for device in snapshot["devices"]]
    expected = {device["id"]: device["generation"] for device in snapshot["devices"]}
    with db.transaction() as session:
        session.get(Device, ids[-1]).generation += 1
        expiry = session.get(Subscription, sid).expires_at
    before = len(state["calls"])
    with pytest.raises(DomainError) as failure:
        ops.revoke_devices(aid, sid, ids, "stale-batch", confirmed=True, expected_generations=expected)
    assert failure.value.code == "device_selection_changed" and failure.value.status == 409
    with db.transaction() as session:
        assert not list(session.scalars(select(Job).where(Job.kind == "device_revoke")))
        assert not list(session.scalars(select(DeviceUnlink)))
        assert session.get(Subscription, sid).expires_at == expiry
        assert all(not session.get(Device, did).revoked for did in ids)
    assert len(state["calls"]) == before


def test_v11_confirmed_generation_replay_keeps_original_result_after_completion(operations):
    ops, commerce, owned = operations
    db, _, _, state, aid, sid, _ = owned
    snapshot = ops.inventory(aid, sid)
    ids = [device["id"] for device in snapshot["devices"]]
    expected = {device["id"]: device["generation"] for device in snapshot["devices"]}
    result = ops.revoke_devices(
        aid, sid, ids, "confirmed-replay", confirmed=True, expected_generations=expected
    )
    assert commerce.provisioning.run_jobs() == 2
    with db.transaction() as session:
        session.get(Device, ids[0]).generation += 1
    before = len(state["calls"])
    assert (
        ops.revoke_devices(
            aid, sid, list(reversed(ids)), "confirmed-replay", confirmed=True, expected_generations=expected
        )
        == result
    )
    with db.transaction() as session:
        assert len(list(session.scalars(select(Job).where(Job.kind == "device_revoke")))) == 2
        assert len(list(session.scalars(select(DeviceUnlink)))) == 2
    assert len(state["calls"]) == before
    changed = expected | {ids[0]: expected[ids[0]] + 1}
    with pytest.raises(DomainError) as failure:
        ops.revoke_devices(aid, sid, ids, "confirmed-replay", confirmed=True, expected_generations=changed)
    assert failure.value.code == "idempotency_conflict"


@pytest.mark.parametrize("mode", ["confirmation", "foreign", "blocked", "duplicate", "too_many", "stale_key"])
def test_v11_bulk_denied_commands_never_enqueue_partial_jobs(operations, mode):
    operations, commerce, owned = operations
    db, panel, worker, state, aid, sid, jid = owned
    with db.transaction() as s:
        ids = list(s.scalars(select(Device.id)))
        if mode == "blocked":
            s.get(Account, aid).unlink_blocked = True
        if mode == "foreign":
            foreign = Account()
            s.add(foreign)
            s.flush()
            aid = foreign.id
    supplied = ids if mode != "duplicate" else [ids[0], ids[0]]
    if mode == "too_many":
        supplied = ["synthetic-" + str(i) for i in range(101)]
    if mode == "stale_key":
        operations.revoke_devices(aid, sid, [ids[0]], "synthetic-key", confirmed=True)
        supplied = [ids[1]]
    with pytest.raises(DomainError):
        operations.revoke_devices(aid, sid, supplied, "synthetic-key", confirmed=mode != "confirmation")
    with db.transaction() as s:
        jobs = list(s.scalars(select(Job).where(Job.kind == "device_revoke")))
        assert len(jobs) == (1 if mode == "stale_key" else 0)


def test_v11_credential_rotation_is_owned_confirmed_idempotent_queue_only(operations):
    operations, commerce, owned = operations
    db, panel, worker, state, aid, sid, jid = owned
    calls = len(state["calls"])
    result = operations.rotate_credentials(aid, sid, "synthetic-rotation", confirmed=True)
    assert result["status"] == "rotate_pending"
    assert operations.rotate_credentials(aid, sid, "synthetic-rotation", confirmed=True) == result
    assert len(state["calls"]) == calls
    with pytest.raises(DomainError) as failure:
        operations.rotate_credentials(aid, sid, "another-key", confirmed=True)
    assert failure.value.code == "credential_rotation_pending"
    with db.transaction() as s:
        job = s.get(Job, result["job_id"])
        assert job.payload["revoke_only_passwords"] is False
        assert job.payload["panel_binding"]["user_id"] == "123"


def test_blocked_rotation_visible_no_duplicate_and_exact_never_started_cancel(operations):
    ops, commerce, owned = operations
    db, _, _, state, aid, sid, _ = owned
    original = ops.rotate_credentials(aid, sid, "blocked-original", confirmed=True)
    with db.transaction() as tx:
        job = tx.get(Job, original["job_id"])
        job.state, job.last_error = "blocked", "panel_target_denied"
        expiry = tx.get(Subscription, sid).expires_at
    calls = len(state["calls"])
    profile = commerce.dashboard(aid)["subscriptions"][0]
    assert profile["credential_rotation_state"] == "blocked"
    assert profile["credential_rotation_job_id"] == original["job_id"]
    assert profile["credential_rotation_cancelable"] is True
    assert profile["panel_sync_state"] == "pending"
    with pytest.raises(DomainError) as blocked:
        ops.rotate_credentials(aid, sid, "blocked-duplicate", confirmed=True)
    assert blocked.value.code == "credential_rotation_pending"
    canceled = ops.cancel_rotation(aid, sid, original["job_id"], "cancel-never-started", confirmed=True)
    assert canceled["status"] == "rotate_canceled"
    assert ops.rotate_credentials(aid, sid, "blocked-original", confirmed=True) == canceled
    assert (
        ops.cancel_rotation(aid, sid, original["job_id"], "cancel-never-started", confirmed=True) == canceled
    )
    assert len(state["calls"]) == calls
    with db.transaction() as tx:
        assert tx.get(Subscription, sid).expires_at == expiry
        assert tx.scalar(select(Audit).where(Audit.action == "credential_rotation_canceled")) is not None
        assert len(list(tx.scalars(select(Job).where(Job.kind == "credential_rotate")))) == 1
    assert commerce.dashboard(aid)["subscriptions"][0]["credential_rotation_state"] is None


def test_v11_inventory_refresh_checks_owner_before_any_http(operations):
    operations, commerce, owned = operations
    db, panel, worker, state, aid, sid, jid = owned
    with db.transaction() as s:
        foreign = Account()
        s.add(foreign)
        s.flush()
        other = foreign.id
    before = len(state["calls"])
    with pytest.raises(DomainError) as failure:
        operations.inventory(other, sid, refresh=True)
    assert failure.value.status == 404 and len(state["calls"]) == before


def test_v11_bulk_counts_each_verified_unlink_and_reappearing_generation(operations):
    operations, commerce, owned = operations
    db, panel, worker, state, aid, sid, jid = owned
    ids = [item["id"] for item in operations.inventory(aid, sid)["devices"]]
    result = operations.revoke_devices(aid, sid, ids, "synthetic-bulk-accounting", confirmed=True)
    assert commerce.provisioning.run_jobs() == 2
    with db.transaction() as s:
        assert len(list(s.scalars(select(DeviceUnlink)))) == 2
        assert all(s.get(Job, jid).state == "done" for jid in result["job_ids"])
    state["devices"] = [device("SYNTHETIC-01")]
    operations.panel_events.reconcile_owned(sid)
    active = [item for item in operations.inventory(aid, sid)["devices"] if not item["revoked"]]
    assert len(active) == 1 and active[0]["generation"] == 2
    operations.revoke_devices(aid, sid, [active[0]["id"]], "synthetic-second-generation", confirmed=True)
    assert commerce.provisioning.run_jobs() == 1
    with db.transaction() as s:
        events = list(s.scalars(select(DeviceUnlink)))
        assert len(events) == 3 and sorted(item.generation for item in events) == [1, 1, 2]


def identity_service(db):
    config = SyntheticSettings(
        _env_file=None,
        database_url=db.engine.url.render_as_string(hide_password=False),
        local_test=True,
        secret_key="synthetic-v11-identity-" * 3,
        owner_id=None,
        owner_email=None,
        admin_email_allowlist=[],
        email_allowlist=["synthetic@identity.test"],
    )
    return Accounts(db, RateGuard(fakeredis.FakeRedis()), config)


def session_row(account_id):
    return Session(
        id=digest("synthetic-v11-session-" + str(account_id)),
        account_id=account_id,
        csrf="synthetic-csrf",
        expires_at=now() + timedelta(days=1),
        confirmed_at=now(),
        email_confirmed_at=now(),
        ip_address="127.0.0.1",
        auth_method="email_otp",
    )


def test_v11_verified_telegram_unlink_keeps_original_panel_identity_manageable(operations):
    ops, commerce, owned = operations
    db, panel, worker, state, aid, sid, jid = owned
    accounts = identity_service(db)
    with db.transaction() as s:
        account = s.get(Account, aid)
        account.email, account.email_verified = "synthetic@identity.test", True
        current = session_row(aid)
        s.add(current)
        session_id = current.id
    features = AccountsFeatures(accounts)
    request = features.request_unlink(aid, session_id, "127.0.0.1")
    features.confirm_unlink(int(panel.auto_test_telegram_ids.copy().pop()), request["proof"])
    assert ops.inventory(aid, sid, refresh=True)["error"] is None
    assert (
        ops.rotate_credentials(aid, sid, "after-verified-unlink", confirmed=True)["status"]
        == "rotate_pending"
    )
    assert commerce.provisioning.run_jobs() == 1
    with db.transaction() as s:
        sub = s.get(Subscription, sid)
        assert s.get(Account, aid).telegram_id is None and sub.panel_user_id == 123
        assert sub.short_uuid == "r" * 24 and sub.expires_at > now()


def test_v11_verified_account_merge_resolves_immutable_origin_to_current_owner(operations):
    ops, commerce, owned = operations
    db, panel, worker, state, aid, sid, jid = owned
    accounts = identity_service(db)
    with db.transaction() as s:
        source = Account(email="synthetic@identity.test", email_verified=True)
        s.add(source)
        s.flush()
        new_id = source.id
        current = session_row(new_id)
        s.add(current)
        session_id = current.id
    accept_test_legal(commerce, new_id)
    request = accounts.request_link(
        new_id, str(next(iter(panel.auto_test_telegram_ids))), now(), session_id=session_id, ip="127.0.0.1"
    )
    accounts.confirm_telegram_link(aid, request["proof"])
    assert ops.inventory(new_id, sid, refresh=True)["error"] is None
    ops.rotate_credentials(new_id, sid, "after-verified-merge", confirmed=True)
    assert commerce.provisioning.run_jobs() == 1
    with db.transaction() as s:
        assert s.get(Subscription, sid).account_id == new_id
        assert s.get(Subscription, sid).panel_user_id == 123


@pytest.mark.parametrize("mode", ["unverified_transfer", "import_issue"])
def test_v11_origin_ownership_or_import_violation_never_reaches_http(operations, mode):
    ops, commerce, owned = operations
    db, panel, worker, state, aid, sid, jid = owned
    with db.transaction() as s:
        if mode == "import_issue":
            s.add(LegacyIssue(source_table="subscriptions", source_id=sid, reason="synthetic-import-test"))
        else:
            foreign = Account()
            s.add(foreign)
            s.flush()
            aid = foreign.id
            s.get(Subscription, sid).account_id = aid
    before = len(state["calls"])
    assert ops.panel_events.reconcile_owned(sid)["status"] == "denied"
    assert len(state["calls"]) == before
