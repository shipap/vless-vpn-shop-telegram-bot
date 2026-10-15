"""Owned numeric panel integration: isolated SQL and synthetic HTTP only."""

import hashlib
import hmac
import importlib
import json
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select
from sunrise_core.errors import DomainError
from sunrise_infrastructure.db import (
    Account,
    Device,
    Job,
    Notification,
    NotificationDelivery,
    PanelEvent,
    PanelObservation,
    Setting,
    Subscription,
    now,
    uid,
)
from sunrise_infrastructure.provisioning import Provisioning

from tests.isolated_database import isolated_database

TG = "70000000000000001"
SQUAD = "20000000-0000-4000-8000-000000000002"
SECRET = "synthetic-webhook-secret-only"  # noqa: S105 - synthetic HMAC fixture


@pytest.fixture
def owned_panel():
    with isolated_database("v11_panel") as db:
        state = {
            "user": None,
            "devices": [],
            "calls": [],
            "lose_create_response": False,
            "lose_rotate_response": False,
        }

        def handler(request):
            state["calls"].append((request.method, request.url.path))
            if request.url.path.endswith("/actions/revoke"):
                state["user"]["shortUuid"] = "r" * 24
                state["user"]["subscriptionUrl"] = "https://subscription.example.test/" + "r" * 24
                if state["lose_rotate_response"]:
                    state["lose_rotate_response"] = False
                    raise httpx.ReadError("synthetic lost rotate response")
                return httpx.Response(200, json={"response": state["user"]})
            if request.url.path == "/api/hwid/devices/delete":
                body = json.loads(request.content)
                assert body["userId"] == 123
                state["devices"] = [item for item in state["devices"] if item["hwid"] != body["hwid"]]
            if request.url.path.startswith("/api/hwid/devices/"):
                return httpx.Response(
                    200, json={"response": {"total": len(state["devices"]), "devices": state["devices"]}}
                )
            if request.method == "POST":
                body = json.loads(request.content)
                assert "uuid" not in body
                state["user"] = body | {
                    "id": 123,
                    "expireAt": body["expireAt"],
                    "activeInternalSquads": [{"uuid": SQUAD}],
                    "subscriptionUrl": "https://subscription.example.test/" + body["shortUuid"],
                    "userTraffic": {"usedTrafficBytes": 42},
                    "subRevokedAt": None,
                }
                if state["lose_create_response"]:
                    state["lose_create_response"] = False
                    raise httpx.ReadError("synthetic lost create response")
            if request.method == "PATCH":
                body = json.loads(request.content)
                state["user"].update(body | {"activeInternalSquads": [{"uuid": SQUAD}]})
            return (
                httpx.Response(200, json={"response": state["user"]})
                if state["user"]
                else httpx.Response(404)
            )

        from sunrise_infrastructure.panel_v3 import RemnawaveV3

        panel = RemnawaveV3(
            base_url="https://panel.example.test",
            token="synthetic-token",  # noqa: S106 - MockTransport only
            authorized=True,
            allowed_hosts={"panel.example.test"},
            allowed_squads={SQUAD},
            auto_test_telegram_ids={TG},
            allowed_subscription_hosts={"subscription.example.test"},
            transport=httpx.MockTransport(handler),
        )
        worker = Provisioning(db, panel)
        with db.transaction() as s:
            account = Account(telegram_id=int(TG))
            s.add(account)
            s.flush()
            aid, sid, jid = account.id, uid(), uid()
            expires = (now() + timedelta(days=30)).replace(microsecond=0)
            s.add(
                Subscription(
                    devices_limit=4, id=sid, account_id=aid, expires_at=expires, environment="local_test"
                )
            )
            s.flush()
            s.add(
                Job(
                    id=jid,
                    key="synthetic:" + jid,
                    account_id=aid,
                    kind="provision",
                    payload={
                        "subscription_id": sid,
                        "environment": "local_test",
                        "version": 1,
                        "panel_object_type": "internal_squad",
                        "internal_squad_uuid": SQUAD,
                        "expires_at": expires.isoformat(),
                        "devices_limit": 4,
                    },
                )
            )
        yield db, panel, worker, state, aid, sid, jid
        panel.client.close()


def settings():
    return SimpleNamespace(
        panel_webhook_test_authorized=True,
        panel_webhook_secret=SECRET,
        panel_webhook_max_bytes=262144,
        panel_webhook_max_age_seconds=300,
        panel_test_user_ids=[],
        panel_test_node_uuids=[],
        panel_reconcile_interval_seconds=300,
    )


class Notifications:
    def __init__(self, db):
        self.events = []
        from sunrise_infrastructure.notifications import NotificationService

        self.real = NotificationService(db, settings())

    def emit_in(self, session, account_id, event_key, kind, title, body, **kwargs):
        self.real.emit_in(session, account_id, event_key, kind, title, body, **kwargs)
        self.events.append((account_id, event_key, kind))


def service(db, panel, notifications=None, config=None):
    cls = importlib.import_module("sunrise_infrastructure.panel_events").PanelEvents
    return cls(db, panel, config or settings(), notifications=notifications or Notifications(db))


def signed_event(kind="user.modified", user_id=123, hwid=None, timestamp=None, **extra):
    timestamp = timestamp or now().isoformat()
    data = {
        "id": user_id,
        "password": "must-never-persist",
        "subscriptionUrl": "https://private.invalid/secret",
    }
    if hwid is not None:
        data = {
            "user": data,
            "hwidUserDevice": {
                "userId": user_id,
                "hwid": hwid,
                "createdAt": timestamp,
                "requestIp": "192.0.2.10",
            },
        }
    raw = json.dumps(
        {"scope": kind.split(".")[0], "event": kind, "timestamp": timestamp, "data": data} | extra
    ).encode()
    return raw, {
        "X-Remnawave-Signature": hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest(),
        "X-Remnawave-Timestamp": timestamp,
    }


def device(hwid):
    return {
        "userId": 123,
        "hwid": hwid,
        "deviceModel": "Synthetic phone",
        "platform": "Android",
        "createdAt": now().isoformat(),
        "updatedAt": now().isoformat(),
    }


def test_v11_provisioning_persists_numeric_identity_and_restarts_without_create(owned_panel):
    db, panel, worker, state, aid, sid, jid = owned_panel
    assert worker.run_jobs() == 1
    with db.transaction() as s:
        sub, job, record = s.get(Subscription, sid), s.get(Job, jid), s.get(Setting, "panel_test:" + sid)
        assert sub.panel_user_id == 123 and sub.panel_uuid is None
        assert record.value["binding"]["user_id"] == "123"
        assert job.payload["panel_binding"]["user_id"] == "123"
        assert sub.connection_url.endswith(sub.short_uuid)
        expires = sub.expires_at
        job.state, job.available_at = "pending", now()
    restarted = Provisioning(db, panel)
    assert restarted.run_jobs() == 1
    assert sum(method == "POST" for method, _ in state["calls"]) == 1
    with db.transaction() as s:
        assert s.get(Subscription, sid).expires_at == expires


@pytest.mark.parametrize("mode", ["signature", "timestamp", "stale", "oversize", "shape", "device_owner"])
def test_v11_panel_webhook_fails_closed_before_persistence(owned_panel, mode):
    db, panel, worker, state, aid, sid, jid = owned_panel
    worker.run_jobs()
    raw, headers = signed_event("user_hwid_devices.added", hwid="SYNTHETIC-01")
    config = settings()
    if mode == "signature":
        headers["X-Remnawave-Signature"] = "0" * 64
    elif mode == "timestamp":
        headers["X-Remnawave-Timestamp"] = (now() + timedelta(seconds=1)).isoformat()
    elif mode == "stale":
        raw, headers = signed_event(timestamp=(now() - timedelta(hours=1)).isoformat())
    elif mode == "oversize":
        config.panel_webhook_max_bytes = len(raw) - 1
    else:
        value = json.loads(raw)
        if mode == "shape":
            value = []
        else:
            value["data"]["hwidUserDevice"]["userId"] = 124
        raw = json.dumps(value).encode()
        headers["X-Remnawave-Signature"] = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    with pytest.raises(DomainError):
        service(db, panel, config=config).accept(raw, headers)
    with db.transaction() as s:
        assert list(s.scalars(select(PanelEvent))) == []


def test_v11_foreign_event_is_ignored_without_storing_customer_payload(owned_panel):
    db, panel, worker, state, aid, sid, jid = owned_panel
    worker.run_jobs()
    raw, headers = signed_event(user_id=124)
    assert service(db, panel).accept(raw, headers)["status"] == "ignored"
    with db.transaction() as s:
        assert list(s.scalars(select(PanelEvent))) == []


def test_v11_snapshot_baseline_device_generation_and_duplicate_events(owned_panel):
    db, panel, worker, state, aid, sid, jid = owned_panel
    worker.run_jobs()
    notifications = Notifications(db)
    events = service(db, panel, notifications)
    state["devices"] = [device("SYNTHETIC-01")]
    assert events.reconcile_owned(sid, initial=True)["status"] == "ok"
    assert notifications.events == []
    state["devices"].append(device("SYNTHETIC-02"))
    raw, headers = signed_event("user_hwid_devices.added", hwid="SYNTHETIC-02")
    assert events.accept(raw, headers)["status"] == "accepted"
    assert events.accept(raw, headers)["status"] == "duplicate"
    assert events.reconcile_pending() == 1
    assert len(notifications.events) == 1
    with db.transaction() as s:
        event = s.scalar(select(PanelEvent))
        assert "must-never-persist" not in json.dumps(event.payload)
        assert "requestIp" not in json.dumps(event.payload)
        sub = s.get(Subscription, sid)
        assert sub.status == "active" and sub.panel_user_id == 123
        observation = s.scalar(select(PanelObservation))
        assert observation.traffic_bytes == 42
        assert "subscriptionUrl" not in json.dumps(observation.payload)
    state["devices"] = [device("SYNTHETIC-01")]
    events.reconcile_owned(sid)
    state["devices"].append(device("SYNTHETIC-02"))
    events.reconcile_owned(sid)
    assert len(notifications.events) == 2
    with db.transaction() as s:
        found = s.scalar(select(Device).where(Device.panel_hwid == "SYNTHETIC-02"))
        assert found.generation == 2 and not found.revoked
        assert len(list(s.scalars(select(Notification)))) == 2
        assert len(list(s.scalars(select(NotificationDelivery)))) == 2


@pytest.mark.parametrize("kind", ["user.modified", "user.expired"])
@pytest.mark.parametrize(
    "payload",
    [
        {"import_evidence_sha256": "a" * 64},
        [],
        {"device_count": None},
        {"device_count": False},
        {"device_count": True},
        {"device_count": "0"},
        {"device_count": -1},
        {"device_count": 1001},
    ],
)
def test_v11_imported_observation_baselines_historical_devices(owned_panel, kind, payload):
    db, panel, worker, state, aid, sid, jid = owned_panel
    worker.run_jobs()
    with db.transaction() as s:
        s.add(PanelObservation(kind="user", resource_id="123", subscription_id=sid, payload=payload))
        assert list(s.scalars(select(Device))) == []
    state["devices"] = [device("HISTORICAL-01"), device("HISTORICAL-02")]
    notifications = Notifications(db)
    events = service(db, panel, notifications)
    raw, headers = signed_event(kind)
    assert events.accept(raw, headers)["status"] == "accepted"
    assert events.reconcile_pending() == 1
    assert notifications.events == []
    with db.transaction() as s:
        assert s.scalar(select(PanelObservation)).payload["device_count"] == 2
        assert len(list(s.scalars(select(Device)))) == 2
        assert list(s.scalars(select(Notification))) == []
        assert list(s.scalars(select(NotificationDelivery))) == []


@pytest.mark.parametrize("matches_created_at", [True, False])
def test_v11_imported_baseline_preserves_signed_added_device_notice(owned_panel, matches_created_at):
    db, panel, worker, state, aid, sid, jid = owned_panel
    worker.run_jobs()
    with db.transaction() as s:
        s.add(
            PanelObservation(
                kind="user",
                resource_id="123",
                subscription_id=sid,
                payload={"import_evidence_sha256": "a" * 64},
            )
        )
    historical, added = device("HISTORICAL-01"), device("NEW-01")
    state["devices"] = [historical, added]
    notifications = Notifications(db)
    events = service(db, panel, notifications)
    timestamp = added["createdAt"] if matches_created_at else (now() - timedelta(minutes=1)).isoformat()
    raw, headers = signed_event("user_hwid_devices.added", hwid="NEW-01", timestamp=timestamp)
    assert events.accept(raw, headers)["status"] == "accepted"
    assert events.reconcile_pending() == 1
    expected_count = 1 if matches_created_at else 0
    assert len(notifications.events) == expected_count
    if matches_created_at:
        assert notifications.events[0][2] == "new_device"
    with db.transaction() as s:
        assert len(list(s.scalars(select(Notification)))) == expected_count
        assert len(list(s.scalars(select(NotificationDelivery)))) == expected_count


@pytest.mark.parametrize("device_count", [None, 0, 1000])
def test_v11_device_baseline_preserves_new_and_returning_device_notices(owned_panel, device_count):
    db, panel, worker, state, aid, sid, jid = owned_panel
    worker.run_jobs()
    with db.transaction() as s:
        s.add(
            PanelObservation(
                kind="user",
                resource_id="123",
                subscription_id=sid,
                payload={"import_evidence_sha256": "a" * 64}
                if device_count is None
                else {"device_count": device_count},
            )
        )
    state["devices"] = [device("NEW-01")]
    notifications = Notifications(db)
    events = service(db, panel, notifications)
    if device_count is None:
        assert events.reconcile_owned(sid)["status"] == "ok"
        assert notifications.events == []
        state["devices"].append(device("NEW-02"))
    assert events.reconcile_owned(sid)["status"] == "ok"
    assert len(notifications.events) == 1
    state["devices"] = []
    assert events.reconcile_owned(sid)["status"] == "ok"
    state["devices"] = [device("NEW-02" if device_count is None else "NEW-01")]
    assert events.reconcile_owned(sid)["status"] == "ok"
    assert len(notifications.events) == 2
    with db.transaction() as s:
        found = s.scalar(
            select(Device).where(Device.panel_hwid == ("NEW-02" if device_count is None else "NEW-01"))
        )
        assert found.generation == 2 and not found.revoked
        assert len(list(s.scalars(select(Notification)))) == 2
        assert len(list(s.scalars(select(NotificationDelivery)))) == 2


def test_v11_out_of_order_add_only_notifies_current_inventory_and_never_changes_paid_rights(owned_panel):
    from decimal import Decimal

    from sunrise_infrastructure.db import Segment

    db, panel, worker, state, aid, sid, jid = owned_panel
    worker.run_jobs()
    with db.transaction() as s:
        expiry = s.get(Subscription, sid).expires_at
        periods = [
            Segment(
                subscription_id=sid,
                product_id="standard",
                starts_at=now() - timedelta(days=1),
                ends_at=expiry,
                devices=4,
                paid_value=Decimal("99.00"),
                price_basis={"synthetic": "paid-history"},
            ),
            Segment(
                subscription_id=sid,
                product_id="standard",
                starts_at=expiry,
                ends_at=expiry + timedelta(days=30),
                devices=4,
                paid_value=Decimal("99.00"),
                price_basis={"synthetic": "future-history"},
            ),
        ]
        s.add_all(periods)
        s.flush()
        history = [(p.id, p.starts_at, p.ends_at, p.paid_value, dict(p.price_basis)) for p in periods]
    notifications = Notifications(db)
    events = service(db, panel, notifications)
    events.reconcile_owned(sid, initial=True)
    raw, headers = signed_event("user_hwid_devices.added", hwid="ALREADY-DELETED")
    events.accept(raw, headers)
    state["user"]["status"] = "DISABLED"
    state["user"]["expireAt"] = "2025-01-01T00:00:00+00:00"
    events.reconcile_pending()
    assert notifications.events == []
    with db.transaction() as s:
        sub = s.get(Subscription, sid)
        assert sub.status == "active"
        assert s.scalar(select(PanelObservation)).payload["expires_at"] == state["user"]["expireAt"]
        for identity, starts, ends, value, basis in history:
            period = s.get(Segment, identity)
            assert (period.starts_at, period.ends_at, period.paid_value, period.price_basis) == (
                starts,
                ends,
                value,
                basis,
            )
        assert sub.expires_at == periods[1].ends_at
        assert s.scalar(select(PanelObservation)).status == "DISABLED"


def test_v11_snapshot_failure_keeps_previous_observation_and_queue_retry(owned_panel):
    db, panel, worker, state, aid, sid, jid = owned_panel
    worker.run_jobs()
    events = service(db, panel)
    events.reconcile_owned(sid, initial=True)
    state["user"]["userTraffic"] = {"usedTrafficBytes": "invalid"}
    raw, headers = signed_event()
    events.accept(raw, headers)
    assert events.reconcile_pending() == 0
    with db.transaction() as s:
        event = s.scalar(select(PanelEvent))
        assert event.state == "review" and event.last_error == "panel_review_required"
        observation = s.scalar(select(PanelObservation))
        assert observation.traffic_bytes == 42 and observation.last_error == "panel_review_required"


def test_v11_no_node_allowlist_never_calls_global_panel_nodes(owned_panel):
    db, panel, worker, state, aid, sid, jid = owned_panel
    assert service(db, panel).nodes_snapshot() == []
    assert state["calls"] == []


def test_v11_rotation_persists_new_link_and_job_proof_without_changing_paid_expiry(owned_panel):
    db, panel, worker, state, aid, sid, jid = owned_panel
    assert worker.run_jobs() == 1
    with db.transaction() as s:
        sub = s.get(Subscription, sid)
        expiry = sub.expires_at
        s.add(
            Job(
                key="synthetic:rotate:" + sid,
                account_id=aid,
                kind="credential_rotate",
                payload={
                    "subscription_id": sid,
                    "environment": sub.environment,
                    "version": sub.version,
                    "revoke_only_passwords": False,
                },
            )
        )
    assert worker.run_jobs() == 1
    with db.transaction() as s:
        sub = s.get(Subscription, sid)
        assert sub.panel_uuid is None and sub.panel_user_id == 123
        assert sub.short_uuid == "r" * 24 and sub.connection_url.endswith("r" * 24)
        assert sub.expires_at == expiry and sub.status == "active"
        job = s.scalar(select(Job).where(Job.kind == "credential_rotate"))
        assert job.payload["rotate_started"] is True and job.payload["before_expires_at"]
        assert s.get(Setting, "panel_test:" + sid).value["binding"]["short_uuid"] == "r" * 24


def test_v11_cross_account_provision_job_cannot_reach_transport(owned_panel):
    db, panel, worker, state, aid, sid, jid = owned_panel
    with db.transaction() as s:
        other = Account(telegram_id=70000000000000002)
        s.add(other)
        s.flush()
        s.get(Job, jid).account_id = other.id
    assert worker.run_jobs() == 0
    assert state["calls"] == []
    with db.transaction() as s:
        assert s.get(Job, jid).state == "blocked"


def test_v11_lost_create_response_recovers_saved_intent_without_duplicate_user(owned_panel):
    db, panel, worker, state, aid, sid, jid = owned_panel
    state["lose_create_response"] = True
    assert worker.run_jobs() == 0
    with db.transaction() as s:
        job = s.get(Job, jid)
        assert job.state == "pending" and job.payload["create_started"] is True
        assert s.get(Subscription, sid).panel_user_id is None
        job.available_at = now()
    assert Provisioning(db, panel).run_jobs() == 1
    assert sum(method == "POST" and path == "/api/users" for method, path in state["calls"]) == 1
    with db.transaction() as s:
        assert s.get(Subscription, sid).panel_user_id == 123


def test_v11_lost_rotation_response_recovers_from_persisted_proof_without_second_post(owned_panel):
    db, panel, worker, state, aid, sid, jid = owned_panel
    assert worker.run_jobs() == 1
    with db.transaction() as s:
        s.add(
            Job(
                key="synthetic:rotate:" + sid,
                account_id=aid,
                kind="credential_rotate",
                payload={
                    "subscription_id": sid,
                    "environment": "local_test",
                    "version": 1,
                    "revoke_only_passwords": False,
                },
            )
        )
    state["lose_rotate_response"] = True
    assert worker.run_jobs() == 0
    with db.transaction() as s:
        job = s.scalar(select(Job).where(Job.kind == "credential_rotate"))
        assert job.state == "pending" and job.payload["rotate_started"] is True
        assert job.payload["before_expires_at"]
        job.available_at = now()
    assert Provisioning(db, panel).run_jobs() == 1
    assert sum(path.endswith("/actions/revoke") and method == "POST" for method, path in state["calls"]) == 1
    with db.transaction() as s:
        assert s.get(Subscription, sid).short_uuid == "r" * 24


def test_v11_pending_unlink_inventory_absence_does_not_preempt_verified_accounting(owned_panel):
    db, panel, worker, state, aid, sid, jid = owned_panel
    worker.run_jobs()
    state["devices"] = [device("SYNTHETIC-01")]
    events = service(db, panel)
    events.reconcile_owned(sid, initial=True)
    with db.transaction() as s:
        found = s.scalar(select(Device))
        did = found.id
        s.add(
            Job(
                key="synthetic:delete:" + did,
                account_id=aid,
                kind="device_revoke",
                payload={
                    "subscription_id": sid,
                    "environment": "local_test",
                    "device_id": did,
                    "hwid": found.panel_hwid,
                    "revoke_started": True,
                },
            )
        )
    state["devices"] = []
    events.reconcile_owned(sid)
    with db.transaction() as s:
        assert not s.get(Device, did).revoked


def test_v11_idle_worker_does_not_refresh_missing_events_or_stale_users(owned_panel):
    db, panel, worker, state, aid, sid, jid = owned_panel
    worker.run_jobs()
    notifications = Notifications(db)
    events = service(db, panel, notifications)
    events.reconcile_owned(sid, initial=True)
    state["devices"] = [device("SYNTHETIC-MISSING-EVENT")]
    with db.transaction() as s:
        s.scalar(select(PanelObservation)).observed_at = now() - timedelta(hours=1)
    assert events.reconcile_pending() == 0
    assert notifications.events == []


def test_v11_stale_device_generation_never_reaches_panel_delete(owned_panel):
    db, panel, worker, state, aid, sid, jid = owned_panel
    worker.run_jobs()
    state["devices"] = [device("SYNTHETIC-01")]
    service(db, panel).reconcile_owned(sid, initial=True)
    with db.transaction() as s:
        found = s.scalar(select(Device))
        found.generation = 2
        s.add(
            Job(
                key="synthetic:stale-delete:" + found.id,
                account_id=aid,
                kind="device_revoke",
                payload={
                    "subscription_id": sid,
                    "environment": "local_test",
                    "device_id": found.id,
                    "hwid": found.panel_hwid,
                    "generation": 1,
                },
            )
        )
    before = len(state["calls"])
    assert worker.run_jobs() == 0
    assert len(state["calls"]) == before
    with db.transaction() as s:
        assert s.scalar(select(Job).where(Job.kind == "device_revoke")).state == "blocked"
