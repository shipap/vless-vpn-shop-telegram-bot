"""Actual SQL/provisioning with pinned 2.8.1 synthetic HTTP; no shared schema or live panel."""

import json
from datetime import datetime, timedelta

import fakeredis
import httpx
import pytest
from sqlalchemy import select
from sunrise_infrastructure.accounts import Accounts
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.customer_operations import CustomerOperations
from sunrise_infrastructure.db import (
    Account,
    Alias,
    Device,
    DeviceUnlink,
    Job,
    PanelObservation,
    Setting,
    Subscription,
    now,
)
from sunrise_infrastructure.panel import RemnawaveTest
from sunrise_infrastructure.panel_events import PanelEvents
from sunrise_infrastructure.provisioning import Provisioning
from sunrise_infrastructure.security import RateGuard

from tests.e2e.local_auth_server import SyntheticSettings
from tests.integration.test_panel_auto_provisioning import SQUAD, new_job, panel_response
from tests.isolated_database import isolated_database
from tests.legal_fixture import accept_test_legal

TG = "70000000000000001"
NODE = "20000000-0000-4000-8000-000000000003"


@pytest.fixture
def panel28_workflow(tmp_path):
    with isolated_database("panel28_workflow") as db:
        state = {"user": None, "devices": [], "calls": [], "lose_rotation": False}

        def handler(request):
            state["calls"].append((request.method, request.url.path))
            hook = state.pop("next_read_hook", None) if request.method == "GET" else None
            if hook:
                hook(request)
            if request.url.path.startswith("/api/nodes/"):
                return (
                    httpx.Response(503)
                    if state.get("node_error")
                    else httpx.Response(
                        200,
                        json={
                            "response": {
                                "uuid": NODE,
                                "name": "Тестовый узел",
                                "isConnected": True,
                                "isDisabled": False,
                                "countryCode": "DE",
                            }
                        },
                    )
                )
            if request.url.path.endswith("/actions/revoke"):
                body = json.loads(request.content)
                if not body["revokeOnlyPasswords"]:
                    state["user"]["shortUuid"] = body["shortUuid"]
                    state["user"]["subscriptionUrl"] = (
                        "https://derived.example.test/subscription/" + body["shortUuid"]
                    )
                state["user"]["subRevokedAt"] = now().isoformat()
                if state["lose_rotation"]:
                    state["lose_rotation"] = False
                    raise httpx.ReadTimeout("synthetic accepted rotation")
            elif request.url.path == "/api/hwid/devices/delete":
                body = json.loads(request.content)
                assert body["userUuid"] == state["user"]["uuid"]
                state["devices"] = [item for item in state["devices"] if item["hwid"] != body["hwid"]]
            elif request.method == "POST":
                state["user"] = panel_response(json.loads(request.content)) | {
                    "id": 123,
                    "userTraffic": {"usedTrafficBytes": 42},
                    "trafficLimitBytes": 0,
                    "subRevokedAt": None,
                }
            if request.url.path.startswith("/api/hwid/devices/"):
                result = {"devices": state["devices"], "total": len(state["devices"])}
            else:
                result = state["user"]
            return (
                httpx.Response(200, json={"response": result}) if result is not None else httpx.Response(404)
            )

        panel = RemnawaveTest(
            base_url="https://panel.example.test",
            token="synthetic-token",  # noqa: S106 - mock HTTP only
            contract="2.8_uuid",
            authorized=True,
            allowed_hosts={"panel.example.test"},
            allowed_squads={SQUAD},
            allowed_nodes={NODE},
            auto_test_telegram_ids={TG},
            transport=httpx.MockTransport(handler),
        )  # noqa: S106 - mock HTTP only
        worker = Provisioning(db, panel)
        with db.transaction() as tx:
            tx.add(Account(id=301, telegram_id=int(TG)))
        sid, jid = new_job(db, 301)
        assert worker.run_jobs() == 1
        config = SyntheticSettings(
            _env_file=None,
            local_test=True,
            database_url=db.engine.url.render_as_string(hide_password=False),
            secret_key="panel28-synthetic-isolated-" * 3,
            panel_test_node_uuids=[NODE],
            email_allowlist=["panel28@example.test"],
            mail_dir=str(tmp_path),
        )
        accounts = Accounts(db, RateGuard(fakeredis.FakeRedis()), config)
        commerce = Commerce(db, local_test=True, panel_provider=panel)
        commerce.accounts = accounts
        accept_test_legal(commerce, 301)
        events = PanelEvents(db, panel, config)
        operations = CustomerOperations(commerce, events)
        state["devices"] = [
            {
                "hwid": "SYNTHETIC-" + str(i),
                "userId": 123,
                "platform": "Android",
                "deviceModel": "Тестовый телефон",
                "createdAt": now().isoformat(),
                "updatedAt": now().isoformat(),
            }
            for i in (1, 2)
        ]
        yield db, panel, commerce, events, operations, state, sid, accounts
        panel.client.close()


def test_panel28_workflow_inventory_and_bulk_revoke_generations_preserve_paid_expiry(panel28_workflow):
    db, _, commerce, events, operations, state, sid, _ = panel28_workflow
    assert events.reconcile_owned(sid, initial=True)["status"] == "ok"
    inventory = operations.inventory(301, sid)
    assert inventory["traffic_bytes"] == "42" and inventory["traffic_limit_bytes"] == "0"
    assert len(inventory["devices"]) == 2
    with db.transaction() as tx:
        expiry = tx.get(Subscription, sid).expires_at
    ids = [item["id"] for item in inventory["devices"]]
    job = operations.revoke_devices(301, sid, ids, "own-bulk", confirmed=True)
    assert commerce.provisioning.run_jobs() == 2
    with db.transaction() as tx:
        assert all(tx.get(Device, did).revoked for did in ids)
        assert all(tx.get(Job, jid).state == "done" for jid in job["job_ids"])
        assert len(list(tx.scalars(select(DeviceUnlink)))) == 2
        assert tx.get(Subscription, sid).expires_at == expiry
    assert state["devices"] == []


def test_panel28_workflow_uncertain_rotation_persists_proof_and_never_reposts(panel28_workflow):
    db, _, commerce, _, operations, state, sid, _ = panel28_workflow
    with db.transaction() as tx:
        sub = tx.get(Subscription, sid)
        old = (sub.panel_uuid, sub.short_uuid, sub.expires_at)
    result = operations.rotate_credentials(301, sid, "own-rotation", confirmed=True)
    state["lose_rotation"] = True
    assert commerce.provisioning.run_jobs() == 0
    with db.transaction() as tx:
        job = tx.get(Job, result["job_id"])
        assert job.payload["rotate_started"] is True
        assert job.payload["next_short_uuid"] == state["user"]["shortUuid"]
        job.available_at = now() - timedelta(seconds=1)
    assert commerce.provisioning.run_jobs() == 1
    with db.transaction() as tx:
        sub = tx.get(Subscription, sid)
        record = tx.get(Setting, "panel_test:" + sid).value
        assert sub.panel_uuid == old[0] and sub.expires_at == old[2]
        assert sub.short_uuid == record["binding"]["short_uuid"] == state["user"]["shortUuid"] != old[1]
        assert sub.connection_url == record["connection_url"] == state["user"]["subscriptionUrl"]
    assert sum(method == "POST" and path.endswith("/actions/revoke") for method, path in state["calls"]) == 1


def test_panel28_workflow_verified_canonical_merge_keeps_origin_snapshot_valid(panel28_workflow):
    db, _, _, events, operations, _, sid, accounts = panel28_workflow
    with db.transaction() as tx:
        tx.add(Account(id=302, email="synthetic-canonical@example.test", email_verified=True))
    proof = accounts.request_link(302, TG, now())["proof"]
    accounts.confirm_telegram_link(301, proof)
    assert events.reconcile_owned(sid, initial=True)["status"] == "ok"
    assert operations.inventory(302, sid)["traffic_bytes"] == "42"
    with db.transaction() as tx:
        assert tx.get(Alias, 301).canonical_id == 302
        assert tx.get(Setting, "panel_test:" + sid).value["account_id"] == "301"


def test_panel28_workflow_imported_or_foreign_record_fails_before_http(panel28_workflow):
    db, _, _, events, _, state, sid, _ = panel28_workflow
    with db.transaction() as tx:
        record = tx.get(Setting, "panel_test:" + sid)
        record.value = record.value | {"origin": "legacy_import"}
    calls = len(state["calls"])
    assert events.reconcile_owned(sid)["status"] == "denied"
    assert len(state["calls"]) == calls
    with db.transaction() as tx:
        assert not list(tx.scalars(select(PanelObservation)))


def test_panel28_workflow_snapshot_rechecks_origin_after_http(panel28_workflow):
    db, _, _, events, _, state, sid, _ = panel28_workflow

    def replace_origin(_):
        with db.transaction() as tx:
            record = tx.get(Setting, "panel_test:" + sid)
            record.value = record.value | {"account_id": "999"}

    state["next_read_hook"] = replace_origin
    assert events.reconcile_owned(sid)["status"] == "denied"
    with db.transaction() as tx:
        assert not list(tx.scalars(select(PanelObservation)))
        assert not list(tx.scalars(select(Device)))


def test_panel28_workflow_rotation_rechecks_origin_before_post(panel28_workflow):
    db, _, commerce, _, operations, state, sid, _ = panel28_workflow
    result = operations.rotate_credentials(301, sid, "race-rotation", confirmed=True)

    def replace_origin(_):
        with db.transaction() as tx:
            record = tx.get(Setting, "panel_test:" + sid)
            record.value = record.value | {"account_id": "999"}

    state["next_read_hook"] = replace_origin
    assert commerce.provisioning.run_jobs() == 0
    with db.transaction() as tx:
        assert tx.get(Job, result["job_id"]).state == "review"
    assert not any(method == "POST" and path.endswith("/actions/revoke") for method, path in state["calls"])


def test_panel28_workflow_password_only_rotation_preserves_url_and_proves_marker(panel28_workflow):
    db, _, commerce, _, operations, state, sid, _ = panel28_workflow
    with db.transaction() as tx:
        sub = tx.get(Subscription, sid)
        before = (sub.panel_uuid, sub.short_uuid, sub.connection_url, sub.expires_at)
    result = operations.rotate_credentials(
        301, sid, "password-only", confirmed=True, revoke_only_passwords=True
    )
    assert commerce.provisioning.run_jobs() == 1
    with db.transaction() as tx:
        sub = tx.get(Subscription, sid)
        assert (sub.panel_uuid, sub.short_uuid, sub.connection_url, sub.expires_at) == before
        job = tx.get(Job, result["job_id"])
        assert job.state == "done" and job.payload["before_sub_revoked_at"] is None
    assert state["user"]["subRevokedAt"]


def test_panel28_idle_reconcile_never_polls_owned_or_imported_subscriptions(panel28_workflow):
    db, _, _, events, _, state, sid, _ = panel28_workflow
    with db.transaction() as tx:
        tx.add(
            Subscription(
                devices_limit=4,
                id="0" * 64,
                account_id=301,
                panel_uuid="10000000-0000-4000-8000-000000000001",
                environment="sandbox",
            )
        )
    events.reconcile_pending(limit=1)
    with db.transaction() as tx:
        observations = list(tx.scalars(select(PanelObservation)))
        assert observations == []
    assert not any("10000000-0000-4000-8000-000000000001" in path for _, path in state["calls"])


def test_panel28_idle_reconcile_never_creates_cursor_or_refreshes_denied_records(panel28_workflow):
    db, panel, _, events, _, state, sid, accounts = panel28_workflow
    denied = "0" * 64
    with db.transaction() as tx:
        own = tx.get(Setting, "panel_test:" + sid).value
        tx.add(
            Subscription(
                devices_limit=4,
                id=denied,
                account_id=301,
                panel_uuid="10000000-0000-4000-8000-000000000001",
                environment="sandbox",
            )
        )
        tx.add(
            Setting(key="panel_test:" + denied, value=own | {"subscription_id": denied, "telegram_id": "999"})
        )
    before = len(state["calls"])
    events.reconcile_pending(limit=1)
    assert len(state["calls"]) == before
    with db.transaction() as tx:
        assert tx.scalar(select(PanelObservation).where(PanelObservation.subscription_id == sid)) is None
    # No absent event is synthesized by a restarted worker.
    PanelEvents(db, panel, accounts.settings).reconcile_pending(limit=1)
    with db.transaction() as tx:
        assert tx.scalar(select(PanelObservation).where(PanelObservation.subscription_id == sid)) is None
        assert tx.scalar(select(PanelObservation).where(PanelObservation.subscription_id == denied)) is None


def test_panel28_workflow_node_failure_marks_previous_observation_stale(panel28_workflow):
    from sunrise_infrastructure.db import Product
    from sunrise_infrastructure.panel_v3 import RemnawaveV3

    db, uuid_panel, commerce, _, _, state, _, accounts = panel28_workflow
    # Explicit node routes use the numeric contract; the shared UUID user fixture stays intact.
    panel = RemnawaveV3(
        base_url="https://panel.example.test",
        token="synthetic-token",  # noqa: S106 -- MockTransport only
        authorized=True,
        allowed_hosts={"panel.example.test"},
        allowed_squads={SQUAD},
        auto_test_telegram_ids={TG},
        allowed_nodes={NODE},
        transport=uuid_panel.client._transport,
    )
    commerce.panel_provider, commerce.public_settings = panel, accounts.settings
    with db.transaction() as tx:
        product = tx.scalar(select(Product.id).where(Product.available, Product.code == "standard"))
        assert product is not None
        tx.add(
            Setting(
                key="panel_event_routes",
                value={
                    "version": 1,
                    "environment": "local_test",
                    "panel_origin": "https://panel.example.test",
                    "contract": "3.4_id",
                    "nodes": [
                        {"node_uuid": NODE, "product_ids": [product], "safe_label": "Synthetic server"}
                    ],
                    "infra": [],
                },
            )
        )
    events = PanelEvents(db, panel, accounts.settings)
    try:
        result = events.nodes_snapshot()
        assert result[0]["status"] == "CONNECTED"
        with db.transaction() as tx:
            observation = tx.scalar(select(PanelObservation).where(PanelObservation.kind == "node"))
            observation.observed_at = now() - timedelta(hours=1)
            previous = observation.observed_at.isoformat()
        state["node_error"] = True
        result = events.nodes_snapshot()
        assert result[0]["error"] == "panel_unavailable"
        assert datetime.fromisoformat(result[0]["observed_at"]) == datetime.fromisoformat(previous)
    finally:
        panel.client.close()


def test_panel28_workflow_verified_email_and_bot_unlink_keeps_own_uuid_manageable(panel28_workflow):
    from pathlib import Path

    from sunrise_infrastructure.account_features import AccountsFeatures
    from sunrise_infrastructure.security import digest

    db, _, commerce, _, operations, _, sid, accounts = panel28_workflow
    with db.transaction() as tx:
        tx.get(Account, 301).email = "panel28@example.test"
    challenge = accounts.request_code("panel28@example.test", "127.0.0.1")
    code = json.loads((Path(accounts.settings.mail_dir) / (challenge["challenge_id"] + ".json")).read_text())[
        "code"
    ]
    token, _ = accounts.verify_code(
        challenge["challenge_id"],
        code,
        "127.0.0.1",
        legal_accepted=True,
        documents=commerce.legal.public()["documents"],
    )
    features = AccountsFeatures(accounts)
    proof = features.request_unlink(301, digest(token), "127.0.0.1")["proof"]
    features.confirm_unlink(int(TG), proof)
    assert operations.inventory(301, sid, refresh=True)["traffic_bytes"] == "42"
    operations.rotate_credentials(301, sid, "after-verified-unlink", confirmed=True)
    assert commerce.provisioning.run_jobs() == 1
    with db.transaction() as tx:
        assert tx.get(Account, 301).telegram_id is None
        assert tx.get(Subscription, sid).panel_uuid


def test_panel28_workflow_generation_race_prevents_device_delete(panel28_workflow):
    db, _, commerce, events, operations, state, sid, _ = panel28_workflow
    assert events.reconcile_owned(sid, initial=True)["status"] == "ok"
    did = operations.inventory(301, sid)["devices"][0]["id"]
    result = operations.revoke_devices(301, sid, [did], "generation-race", confirmed=True)

    def next_generation(_):
        with db.transaction() as tx:
            tx.get(Device, did).generation += 1

    state["next_read_hook"] = next_generation
    assert commerce.provisioning.run_jobs() == 0
    with db.transaction() as tx:
        assert tx.get(Job, result["job_ids"][0]).state == "review"
        assert not tx.get(Device, did).revoked
    assert not any(path == "/api/hwid/devices/delete" for _, path in state["calls"])
