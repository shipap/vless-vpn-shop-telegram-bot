"""Admitted Remnawave groups remain the source of node-to-product membership."""

import hashlib
import hmac
import json
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select, text
from sunrise_infrastructure.db import Audit, Job, Order, PanelEvent, Product, Setting, now
from sunrise_infrastructure.panel_event_routes import _digest, sync_accessible_nodes
from sunrise_infrastructure.panel_events import PanelEvents
from sunrise_infrastructure.panel_news import cleanup_node_news, node_news
from sunrise_infrastructure.scheduler import Scheduler

from tests.contract.test_scoped_payment_policy import synthetic_scope  # noqa: F401
from tests.integration.test_scoped_panel_runtime_events import (  # noqa: F401
    NODE,
    scoped_panel_database,
)
from tests.integration.test_scoped_panel_runtime_events import paid_panel as base_paid_panel  # noqa: F401

PROFILE = "33333333-3333-4333-8333-333333333333"
INBOUND = "44444444-4444-4444-8444-444444444444"


@pytest.fixture
def paid_panel(base_paid_panel):  # noqa: F811
    db, config, commerce, account_id, sid, user, mode, calls = base_paid_panel
    panel, transport = commerce.panel_provider, commerce.panel_provider.client._transport
    mode["host_records"] = []

    def metadata(request):
        path = request.url.path
        if path == "/api/hosts":
            calls.append((request.method, path))
            return httpx.Response(200, json={"response": mode["host_records"]})
        if path.startswith("/api/internal-squads/") and not path.endswith("/accessible-nodes"):
            calls.append((request.method, path))
            return httpx.Response(200, json={"response": {
                "uuid": path.removeprefix("/api/internal-squads/"), "info": {"inboundsCount": 1},
                "inbounds": [{"uuid": INBOUND, "profileUuid": PROFILE, "tag": "entry-tag"}],
            }})
        response = transport.handle_request(request)
        if path.endswith("/accessible-nodes"):
            body = response.json()
            for node in body["response"]["accessibleNodes"]:
                node.update(configProfileUuid=PROFILE, activeInbounds=["entry-tag"])
            return httpx.Response(200, json=body)
        return response

    panel.client._transport = httpx.MockTransport(metadata)
    yield db, config, commerce, account_id, sid, user, mode, calls


def host_record(remark="🇫🇮 Финляндия #1"):
    return {
        "uuid": "66666666-6666-4666-8666-666666666666", "remark": remark, "nodes": [NODE],
        "isDisabled": False, "isHidden": False, "address": "private.synthetic.invalid",
        "inbound": {"configProfileUuid": PROFILE, "configProfileInboundUuid": INBOUND},
        "internalSquads": {"mode": "EXCLUDE", "squads": []},
    }


def expire_success(db):
    with db.transaction() as session:
        job = session.scalar(select(Job).where(Job.kind == "panel_route_sync"))
        job.payload = job.payload | {"success_at": (now() - timedelta(days=1, seconds=1)).isoformat()}
        job.available_at = now() - timedelta(seconds=1)


def test_daily_host_caption_uses_five_gets_once_per_successful_24_hours(paid_panel):
    db, config, commerce, _, _, _, mode, calls = paid_panel
    panel = commerce.panel_provider
    mode["host_records"] = [host_record()]
    calls.clear()
    assert sync_accessible_nodes(db, panel, config) == {"status": "ok", "changed": True}
    assert len(calls) == 5 and all(method == "GET" for method, _ in calls)
    assert sum(path.endswith("/accessible-nodes") for _, path in calls) == 2
    assert sum(path == "/api/hosts" for _, path in calls) == 1
    with db.transaction() as session:
        assert session.get(Setting, "panel_event_routes").value["nodes"][0]["safe_label"] == "🇫🇮 Финляндия #1"
        job = session.scalar(select(Job).where(Job.kind == "panel_route_sync"))
        assert job.payload["panel_labels"] == {NODE: "🇫🇮 Финляндия #1"}
        assert job.payload["generated_labels"] == {}
    mode["host_records"] = [host_record("🇫🇮 Финляндия #2")]
    assert sync_accessible_nodes(db, panel, config) == {"status": "current"}
    assert len(calls) == 5
    expire_success(db)
    assert sync_accessible_nodes(db, panel, config) == {"status": "ok", "changed": True}
    assert len(calls) == 10
    with db.transaction() as session:
        assert session.get(Setting, "panel_event_routes").value["nodes"][0]["safe_label"] == "🇫🇮 Финляндия #2"


@pytest.mark.parametrize("proven", [False, True, "malformed"])
def test_initial_technical_labels_change_only_with_matching_technical_audit(paid_panel, proven):
    db, config, commerce, _, _, _, mode, _ = paid_panel
    mode["host_records"] = [host_record()]
    with db.transaction() as session:
        ids = sorted(session.scalars(select(Product.id).where(Product.available)))
        original = {
            "version": 1, "environment": "scoped_live", "panel_origin": config.panel_base_url,
            "contract": "3.4_id", "nodes": [{
                "node_uuid": NODE, "product_ids": ids, "safe_label": "RV-CLOUD-DE-01",
            }], "infra": [],
        }
        session.add(Setting(key="panel_event_routes", value=original))
        session.add(Audit(actor_id=None, action="panel_event_routes_technical_init", target="panel_event_routes",
                          reason="Синтетическая исходная карта.", details=[] if proven == "malformed" else {"policy_sha256": _digest(original) if proven else "0" * 64}))
    assert sync_accessible_nodes(db, commerce.panel_provider, config)["status"] == "ok"
    with db.transaction() as session:
        assert session.get(Setting, "panel_event_routes").value["nodes"][0]["safe_label"] == (
            "🇫🇮 Финляндия #1" if proven is True else "RV-CLOUD-DE-01"
        )


def test_host_caption_refresh_preserves_owner_edit_since_last_panel_derivation(paid_panel):
    db, config, commerce, _, _, _, mode, _ = paid_panel
    mode["host_records"] = [host_record()]
    assert sync_accessible_nodes(db, commerce.panel_provider, config)["status"] == "ok"
    with db.transaction() as session:
        row = session.get(Setting, "panel_event_routes")
        row.value = row.value | {"nodes": [{**row.value["nodes"][0], "safe_label": "Подпись владельца"}]}
    mode["host_records"] = [host_record("🇫🇮 Финляндия #2")]
    expire_success(db)
    assert sync_accessible_nodes(db, commerce.panel_provider, config)["status"] == "ok"
    with db.transaction() as session:
        assert session.get(Setting, "panel_event_routes").value["nodes"][0]["safe_label"] == "Подпись владельца"
        job = session.scalar(select(Job).where(Job.kind == "panel_route_sync"))
        assert job.payload["panel_labels"] == {} and job.payload["generated_labels"] == {}


def test_failed_host_read_preserves_previous_complete_snapshot(paid_panel):
    db, config, commerce, _, _, _, mode, _ = paid_panel
    panel = commerce.panel_provider
    mode["host_records"] = [host_record()]
    assert sync_accessible_nodes(db, panel, config)["status"] == "ok"
    with db.transaction() as session:
        original = session.get(Setting, "panel_event_routes").value
    transport = panel.client._transport
    panel.client._transport = httpx.MockTransport(lambda request: httpx.Response(503) if request.url.path == "/api/hosts" else transport.handle_request(request))
    expire_success(db)
    assert sync_accessible_nodes(db, panel, config) == {"status": "error", "error": "panel_unavailable"}
    with db.transaction() as session:
        assert session.get(Setting, "panel_event_routes").value == original


def test_scoped_group_sync_bootstraps_route_and_preserves_safe_label_and_infra(paid_panel):  # noqa: F811
    db, config, commerce, _, _, _, mode, calls = paid_panel
    panel = commerce.panel_provider
    with db.transaction() as session:
        standard = session.scalar(select(Product).where(Product.code == "standard"))
        session.add(Setting(key="panel_event_routes", value={
            "version": 1, "environment": "scoped_live", "panel_origin": config.panel_base_url,
            "contract": "3.4_id", "nodes": [{
                "node_uuid": NODE, "product_ids": [standard.id], "safe_label": "Надёжный сервер",
            }], "infra": [{
                "provider_name": "Synthetic", "node_name": "Infrastructure", "safe_label": "Сервер связи",
            }],
        }))
    result = sync_accessible_nodes(db, panel, config)
    assert result == {"status": "ok", "changed": True}
    with db.transaction() as session:
        product_ids = sorted(session.scalars(select(Product.id).where(Product.available)))
        routes = session.get(Setting, "panel_event_routes").value
        assert routes["nodes"] == [{
            "node_uuid": NODE, "product_ids": product_ids, "safe_label": "Надёжный сервер",
        }]
        assert routes["infra"] == [{
            "provider_name": "Synthetic", "node_name": "Infrastructure", "safe_label": "Сервер связи",
        }]
        job = session.scalar(select(Job).where(Job.kind == "panel_route_sync"))
        assert job.state == "done" and job.payload["success_at"]
    assert sum(path.endswith("/accessible-nodes") for _, path in calls) == 2
    mode["accessible_nodes"] = []
    expire_success(db)
    assert sync_accessible_nodes(db, panel, config)["status"] == "ok"
    with db.transaction() as session:
        assert session.get(Setting, "panel_event_routes").value["nodes"] == []


@pytest.mark.parametrize("kind", [
    "created", "modified", "disabled", "enabled", "deleted",
    "connection_lost", "connection_restored", "traffic_notify",
])
def test_signed_cached_node_event_never_reads_group_membership(paid_panel, kind):  # noqa: F811
    db, config, commerce, account_id, _, _, _, _ = paid_panel
    panel = commerce.panel_provider
    assert sync_accessible_nodes(db, panel, config)["status"] == "ok"
    calls = paid_panel[-1]
    calls.clear()
    config.panel_webhook_secret = "synthetic-scoped-webhook-secret"  # noqa: S105
    stamp = now().isoformat()
    raw = json.dumps({
        "scope": "node", "event": "node." + kind, "timestamp": stamp, "data": {"uuid": NODE},
    }).encode()
    headers = {
        "X-Remnawave-Signature": hmac.new(config.panel_webhook_secret.encode(), raw, hashlib.sha256).hexdigest(),
        "X-Remnawave-Timestamp": stamp,
    }
    events = PanelEvents(db, commerce.panel_provider, config)
    assert events.accept(raw, headers)["status"] == "accepted"
    assert events.reconcile_pending() == 1
    assert not any(path.endswith("/accessible-nodes") for _, path in calls)
    state = {"disabled": "outage", "connection_lost": "outage", "enabled": "recovered", "connection_restored": "recovered"}.get(kind)
    assert [item["state"] for item in node_news(commerce, account_id)["items"]] == ([state] if state else [])
    if kind == "deleted":
        assert calls == []


def test_signed_admitted_node_without_cached_membership_keeps_audit_without_http_or_news(paid_panel):  # noqa: F811
    db, config, commerce, account_id, _, _, _, calls = paid_panel
    calls.clear()
    config.panel_webhook_secret = "synthetic-scoped-webhook-secret"  # noqa: S105
    stamp = now().isoformat()
    raw = json.dumps({"scope": "node", "event": "node.disabled", "timestamp": stamp, "data": {"uuid": NODE}}).encode()
    headers = {
        "X-Remnawave-Signature": hmac.new(config.panel_webhook_secret.encode(), raw, hashlib.sha256).hexdigest(),
        "X-Remnawave-Timestamp": stamp,
    }
    events = PanelEvents(db, commerce.panel_provider, config)
    assert events.accept(raw, headers)["status"] == "accepted"
    assert events.reconcile_pending() == 0
    assert calls == [] and node_news(commerce, account_id)["items"] == []
    with db.transaction() as session:
        assert session.scalar(select(PanelEvent)).state == "ignored"
        assert session.get(Setting, "panel_event_routes") is None


@pytest.mark.parametrize("invalid", ["missing", "invalid", "origin", "environment", "unmapped"])
def test_signed_node_event_denies_invalid_current_cache_before_any_http(paid_panel, invalid):  # noqa: F811
    db, config, commerce, _, _, _, _, calls = paid_panel
    assert sync_accessible_nodes(db, commerce.panel_provider, config)["status"] == "ok"
    with db.transaction() as session:
        row = session.get(Setting, "panel_event_routes")
        if invalid == "missing":
            session.delete(row)
        elif invalid == "invalid":
            row.value = {"untrusted": True}
        elif invalid == "origin":
            row.value = row.value | {"panel_origin": "https://foreign.synthetic.invalid"}
        elif invalid == "environment":
            row.value = row.value | {"environment": "live"}
        else:
            row.value = row.value | {"nodes": []}
    calls.clear()
    config.panel_webhook_secret = "synthetic-scoped-webhook-secret"  # noqa: S105
    stamp = now().isoformat()
    raw = json.dumps({"scope": "node", "event": "node.disabled", "timestamp": stamp, "data": {"uuid": NODE}}).encode()
    headers = {
        "X-Remnawave-Signature": hmac.new(config.panel_webhook_secret.encode(), raw, hashlib.sha256).hexdigest(),
        "X-Remnawave-Timestamp": stamp,
    }
    events = PanelEvents(db, commerce.panel_provider, config)
    assert events.accept(raw, headers)["status"] == "accepted"
    assert events.reconcile_pending() == 0
    assert calls == []
    with db.transaction() as session:
        assert session.scalar(select(PanelEvent)).state == "ignored"


@pytest.mark.parametrize("name,expected", [
    ("Финляндия", "Сервер 1"),
    ("https://private.synthetic.invalid", "Сервер 1"),
    ("33333333-3333-4333-8333-333333333333", "Сервер 1"),
])
def test_node_webhook_never_promotes_technical_node_name_over_cached_host_caption(paid_panel, name, expected):  # noqa: F811
    db, config, commerce, _, _, _, _, calls = paid_panel
    panel = commerce.panel_provider
    assert sync_accessible_nodes(db, panel, config)["status"] == "ok"
    calls.clear()
    transport = panel.client._transport

    def named(request):
        if request.url.path == "/api/nodes/" + NODE:
            return httpx.Response(200, json={"response": {
                "uuid": NODE, "name": name, "isConnected": True, "isDisabled": False,
            }})
        return transport.handle_request(request)

    panel.client._transport = httpx.MockTransport(named)
    events = PanelEvents(db, panel, config)
    assert events._reconcile_node(NODE)["status"] == "ok"
    with db.transaction() as session:
        assert session.get(Setting, "panel_event_routes").value["nodes"][0]["safe_label"] == expected
    assert not any(path.endswith("/accessible-nodes") for _, path in calls)


def test_current_node_name_does_not_overwrite_concurrent_owner_label(paid_panel):  # noqa: F811
    db, config, commerce, _, _, _, _, _ = paid_panel
    panel = commerce.panel_provider
    assert sync_accessible_nodes(db, panel, config)["status"] == "ok"
    transport = panel.client._transport

    def edited(request):
        if request.url.path == "/api/nodes/" + NODE:
            with db.transaction() as session:
                row = session.get(Setting, "panel_event_routes")
                row.value = row.value | {"nodes": [{**row.value["nodes"][0], "safe_label": "Ручная подпись"}]}
            return httpx.Response(200, json={"response": {
                "uuid": NODE, "name": "Финляндия", "isConnected": True, "isDisabled": False,
            }})
        return transport.handle_request(request)

    panel.client._transport = httpx.MockTransport(edited)
    assert PanelEvents(db, panel, config)._reconcile_node(NODE)["status"] == "ok"
    with db.transaction() as session:
        assert session.get(Setting, "panel_event_routes").value["nodes"][0]["safe_label"] == "Ручная подпись"


def test_current_node_name_preserves_existing_owner_generic_label(paid_panel):  # noqa: F811
    db, config, commerce, _, _, _, _, _ = paid_panel
    panel = commerce.panel_provider
    with db.transaction() as session:
        ids = sorted(session.scalars(select(Product.id).where(Product.available)))
        session.add(Setting(key="panel_event_routes", value={
            "version": 1, "environment": "scoped_live", "panel_origin": config.panel_base_url,
            "contract": "3.4_id", "nodes": [{
                "node_uuid": NODE, "product_ids": ids, "safe_label": "Сервер 7",
            }], "infra": [],
        }))
    transport = panel.client._transport

    def named(request):
        if request.url.path == "/api/nodes/" + NODE:
            return httpx.Response(200, json={"response": {
                "uuid": NODE, "name": "Финляндия", "isConnected": True, "isDisabled": False,
            }})
        return transport.handle_request(request)

    panel.client._transport = httpx.MockTransport(named)
    assert PanelEvents(db, panel, config)._reconcile_node(NODE)["status"] == "ok"
    with db.transaction() as session:
        assert session.get(Setting, "panel_event_routes").value["nodes"][0]["safe_label"] == "Сервер 7"


def test_signed_deleted_node_keeps_cache_until_daily_refresh_without_getting_deleted_node(paid_panel):  # noqa: F811
    db, config, commerce, _, _, _, mode, calls = paid_panel
    config.panel_webhook_secret = "synthetic-scoped-webhook-secret"  # noqa: S105
    assert sync_accessible_nodes(db, commerce.panel_provider, config)["status"] == "ok"
    mode["accessible_nodes"] = []
    mode["node_authorized"] = False
    before = list(calls)
    stamp = now().isoformat()
    raw = json.dumps({
        "scope": "node", "event": "node.deleted", "timestamp": stamp, "data": {"uuid": NODE},
    }).encode()
    headers = {
        "X-Remnawave-Signature": hmac.new(config.panel_webhook_secret.encode(), raw, hashlib.sha256).hexdigest(),
        "X-Remnawave-Timestamp": stamp,
    }
    events = PanelEvents(db, commerce.panel_provider, config)
    assert events.accept(raw, headers)["status"] == "accepted"
    assert events.reconcile_pending() == 1
    assert not any(path == "/api/nodes/" + NODE for _, path in calls[len(before):])
    with db.transaction() as session:
        assert session.get(Setting, "panel_event_routes").value["nodes"] != []
    assert not any(path.endswith("/accessible-nodes") for _, path in calls[len(before):])
    expire_success(db)
    assert sync_accessible_nodes(db, commerce.panel_provider, config)["status"] == "ok"
    with db.transaction() as session:
        assert session.get(Setting, "panel_event_routes").value["nodes"] == []


def test_failed_group_read_keeps_previous_routes_and_retries(paid_panel):  # noqa: F811
    db, config, commerce, _, _, _, _, _ = paid_panel
    panel = commerce.panel_provider
    with db.transaction() as session:
        standard = session.scalar(select(Product).where(Product.code == "standard"))
        original = {
            "version": 1, "environment": "scoped_live", "panel_origin": config.panel_base_url,
            "contract": "3.4_id", "nodes": [{
                "node_uuid": NODE, "product_ids": [standard.id], "safe_label": "Сервер 1",
            }], "infra": [],
        }
        session.add(Setting(key="panel_event_routes", value=original))
    transport = panel.client._transport

    def broken(request):
        if request.url.path.endswith("/accessible-nodes"):
            return httpx.Response(200, json={"response": {"squadUuid": "wrong", "accessibleNodes": []}})
        return transport.handle_request(request)

    panel.client._transport = httpx.MockTransport(broken)
    assert sync_accessible_nodes(db, panel, config) == {
        "status": "error", "error": "panel_route_sync_unverified",
    }
    with db.transaction() as session:
        assert session.get(Setting, "panel_event_routes").value == original
        job = session.scalar(select(Job).where(Job.kind == "panel_route_sync"))
        assert job.state == "pending" and job.attempts == 1


def test_admission_revocation_stops_before_next_group_get(paid_panel):  # noqa: F811
    db, config, commerce, _, _, _, _, calls = paid_panel
    panel = commerce.panel_provider
    transport = panel.client._transport
    changed = False

    def revoked(request):
        nonlocal changed
        if request.url.path.endswith("/accessible-nodes") and not changed:
            changed = True
            with db.transaction() as session:
                marker = session.get(Setting, "scoped_test_admission")
                marker.value = marker.value | {"scope_id": "revoked"}
        return transport.handle_request(request)

    panel.client._transport = httpx.MockTransport(revoked)
    assert sync_accessible_nodes(db, panel, config)["status"] == "error"
    assert sum(path.endswith("/accessible-nodes") for _, path in calls) == 1
    with db.transaction() as session:
        assert session.get(Setting, "panel_event_routes") is None


def test_concurrent_admin_edit_wins_and_refresh_retries_from_new_basis(paid_panel):  # noqa: F811
    db, config, commerce, _, _, _, _, calls = paid_panel
    panel = commerce.panel_provider
    with db.transaction() as session:
        product = session.scalar(select(Product).where(Product.code == "standard"))
        session.add(Setting(key="panel_event_routes", value={
            "version": 1, "environment": "scoped_live", "panel_origin": config.panel_base_url,
            "contract": "3.4_id", "nodes": [{
                "node_uuid": NODE, "product_ids": [product.id], "safe_label": "Сервер 1",
            }], "infra": [],
        }))
    transport = panel.client._transport
    changed = False

    def raced(request):
        nonlocal changed
        if request.url.path.endswith("/accessible-nodes") and not changed:
            changed = True
            with db.transaction() as session:
                row = session.get(Setting, "panel_event_routes")
                value = dict(row.value)
                value["nodes"] = [{**value["nodes"][0], "safe_label": "Новая подпись"}]
                row.value = value
            assert sync_accessible_nodes(db, panel, config) == {"status": "busy"}
        return transport.handle_request(request)

    panel.client._transport = httpx.MockTransport(raced)
    assert sync_accessible_nodes(db, panel, config) == {
        "status": "error", "error": "panel_route_sync_conflict",
    }
    with db.transaction() as session:
        assert session.get(Setting, "panel_event_routes").value["nodes"][0]["safe_label"] == "Новая подпись"
    assert sum(path.endswith("/accessible-nodes") for _, path in calls) == 2


def test_old_news_is_hidden_then_only_owned_valid_rows_are_cleaned(paid_panel):  # noqa: F811
    db, config, commerce, account_id, _, _, _, _ = paid_panel
    moment = now()
    cutoff = moment - timedelta(hours=24)
    with db.transaction() as session:
        product = session.scalar(select(Product).where(Product.code == "standard"))
        session.add(Setting(key="panel_event_routes", value={
            "version": 1, "environment": "scoped_live", "panel_origin": config.panel_base_url,
            "contract": "3.4_id", "nodes": [{
                "node_uuid": NODE, "product_ids": [product.id], "safe_label": "Сервер 1",
            }], "infra": [],
        }))
        for suffix, stamp, environment in (
            ("a", cutoff - timedelta(microseconds=1), "scoped_live"),
            ("b", cutoff, "scoped_live"),
            ("c", cutoff - timedelta(hours=1), "live"),
        ):
            session.add(Setting(key="node_news:" + suffix * 64, value={
                "version": 1, "id": suffix, "node_uuid": NODE,
                "panel_origin": config.panel_base_url, "environment": environment,
                "contract": "3.4_id", "state": "outage", "occurred_at": stamp.isoformat(),
            }))
        session.add(Setting(key="node_news:" + "d" * 64, value={
            "version": 1, "id": "d", "node_uuid": NODE,
            "panel_origin": config.panel_base_url, "environment": "scoped_live",
            "contract": "3.4_id", "state": "outage", "occurred_at": "invalid",
        }))
        session.add(Setting(key="node_news:" + "e" * 64, value={
            "version": 1, "id": "e", "node_uuid": "44444444-4444-4444-8444-444444444444",
            "panel_origin": config.panel_base_url, "environment": "scoped_live",
            "contract": "3.4_id", "state": "outage",
            "occurred_at": (cutoff - timedelta(hours=1)).isoformat(),
        }))
    assert [item["id"] for item in node_news(commerce, account_id, moment=moment)["items"]] == ["b"]
    assert node_news(commerce, account_id, before="a", moment=moment) == {"items": [], "next_cursor": None}
    with db.transaction() as session:
        session.scalar(select(Product).where(Product.code == "standard")).available = False
    assert cleanup_node_news(db, commerce.panel_provider, config, moment=moment) == (1, None)
    with db.transaction() as session:
        assert session.get(Setting, "node_news:" + "a" * 64) is None
        assert session.get(Setting, "node_news:" + "b" * 64) is not None
        assert session.get(Setting, "node_news:" + "c" * 64) is not None
        assert session.get(Setting, "node_news:" + "d" * 64) is not None
        assert session.get(Setting, "node_news:" + "e" * 64) is not None


def test_generic_dispatcher_preserves_route_claim_and_processes_normal_job(paid_panel):  # noqa: F811
    db, _, commerce, account_id, _, _, _, _ = paid_panel
    with db.transaction() as session:
        order = session.scalar(select(Order).where(Order.account_id == account_id))
        session.add(Job(key="panel-route-sync:scoped_live", kind="panel_route_sync", payload={}, state="pending"))
        session.add(Job(
            key="synthetic-normal-notification", kind="notification", account_id=account_id,
            payload={"order_id": order.id}, state="pending",
        ))
    commerce.run_jobs(limit=50)
    with db.transaction() as session:
        route_job = session.scalar(select(Job).where(Job.key == "panel-route-sync:scoped_live"))
        normal_job = session.scalar(select(Job).where(Job.key == "synthetic-normal-notification"))
        assert route_job.state == "pending" and route_job.attempts == 0
        assert normal_job.state == "done" and normal_job.attempts == 1


def test_no_available_products_removes_stale_membership_without_panel_get(paid_panel):  # noqa: F811
    db, config, commerce, _, _, _, _, calls = paid_panel
    with db.transaction() as session:
        product = session.scalar(select(Product).where(Product.code == "standard"))
        session.add(Setting(key="panel_event_routes", value={
            "version": 1, "environment": "scoped_live", "panel_origin": config.panel_base_url,
            "contract": "3.4_id", "nodes": [{
                "node_uuid": NODE, "product_ids": [product.id], "safe_label": "Сервер 1",
            }], "infra": [],
        }))
        for item in session.scalars(select(Product)):
            item.available = False
    before = list(calls)
    assert sync_accessible_nodes(db, commerce.panel_provider, config)["status"] == "ok"
    assert calls == before
    with db.transaction() as session:
        assert session.get(Setting, "panel_event_routes").value["nodes"] == []


def test_daily_fallback_rechecks_group_after_success_interval(paid_panel):  # noqa: F811
    db, config, commerce, _, _, _, mode, calls = paid_panel
    scheduler = Scheduler(commerce, config)
    assert scheduler.sync_panel_routes() == 1
    first_calls = len(calls)
    mode["accessible_nodes"] = []
    assert scheduler.sync_panel_routes() == 0
    assert len(calls) == first_calls
    with db.transaction() as session:
        job = session.scalar(select(Job).where(Job.kind == "panel_route_sync"))
        job.payload = job.payload | {"success_at": (now() - timedelta(days=1, seconds=1)).isoformat()}
        job.available_at = now() - timedelta(seconds=1)
    assert scheduler.sync_panel_routes() == 1
    with db.transaction() as session:
        assert session.get(Setting, "panel_event_routes").value["nodes"] == []


def test_catalog_mapping_change_waits_for_successful_daily_interval(paid_panel):  # noqa: F811
    db, config, commerce, _, _, _, mode, calls = paid_panel
    scheduler = Scheduler(commerce, config)
    assert scheduler.sync_panel_routes() == 1
    first_calls = len(calls)
    mode["accessible_nodes"] = []
    with db.transaction() as session:
        session.scalar(select(Product).where(Product.code == "standard")).version += 1
    assert scheduler.sync_panel_routes() == 0
    assert len(calls) == first_calls
    with db.transaction() as session:
        job = session.scalar(select(Job).where(Job.kind == "panel_route_sync"))
        job.payload = job.payload | {"success_at": (now() - timedelta(days=1, seconds=1)).isoformat()}
        job.available_at = now() - timedelta(seconds=1)
    assert scheduler.sync_panel_routes() == 1
    assert len(calls) == first_calls + 5
    with db.transaction() as session:
        assert session.get(Setting, "panel_event_routes").value["nodes"] == []


def test_failed_daily_refresh_is_retried_after_backoff(paid_panel):  # noqa: F811
    db, config, commerce, _, _, _, mode, _ = paid_panel
    panel = commerce.panel_provider
    assert sync_accessible_nodes(db, panel, config)["status"] == "ok"
    expire_success(db)
    transport = panel.client._transport
    panel.client._transport = httpx.MockTransport(lambda request: httpx.Response(503))
    assert sync_accessible_nodes(db, panel, config)["status"] == "error"
    panel.client._transport = transport
    mode["accessible_nodes"] = []
    assert sync_accessible_nodes(db, panel, config) == {"status": "busy"}
    with db.transaction() as session:
        job = session.scalar(select(Job).where(Job.kind == "panel_route_sync"))
        job.available_at = now() - timedelta(seconds=1)
    assert sync_accessible_nodes(db, panel, config) == {"status": "ok", "changed": True}
    with db.transaction() as session:
        assert session.get(Setting, "panel_event_routes").value["nodes"] == []


def test_crashed_daily_refresh_is_recovered_after_lease(paid_panel):  # noqa: F811
    db, config, commerce, _, _, _, mode, _ = paid_panel
    panel = commerce.panel_provider
    assert sync_accessible_nodes(db, panel, config)["status"] == "ok"
    expire_success(db)
    transport = panel.client._transport

    def crash(request):
        raise RuntimeError("synthetic-process-crash")

    panel.client._transport = httpx.MockTransport(crash)
    with pytest.raises(RuntimeError, match="synthetic-process-crash"):
        sync_accessible_nodes(db, panel, config)
    panel.client._transport = transport
    mode["accessible_nodes"] = []
    assert sync_accessible_nodes(db, panel, config) == {"status": "busy"}
    with db.transaction() as session:
        job = session.scalar(select(Job).where(Job.kind == "panel_route_sync"))
        job.payload = job.payload | {"lease_until": (now() - timedelta(seconds=1)).isoformat()}
    assert sync_accessible_nodes(db, panel, config) == {"status": "ok", "changed": True}
    with db.transaction() as session:
        assert session.get(Setting, "panel_event_routes").value["nodes"] == []


def test_sync_lock_timeout_keeps_routes_and_durably_retries_owned_claim(paid_panel):  # noqa: F811
    db, config, commerce, _, _, _, mode, _ = paid_panel
    panel = commerce.panel_provider
    assert sync_accessible_nodes(db, panel, config)["status"] == "ok"
    expire_success(db)
    with db.transaction() as session:
        original = session.get(Setting, "panel_event_routes").value
    mode["accessible_nodes"] = []
    with db.engine.begin() as competing:
        competing.execute(text("SELECT pg_advisory_xact_lock(617045501)"))
        assert sync_accessible_nodes(db, panel, config) == {
            "status": "error", "error": "panel_unavailable",
        }
    with db.transaction() as session:
        assert session.get(Setting, "panel_event_routes").value == original
        job = session.scalar(select(Job).where(Job.kind == "panel_route_sync"))
        assert job.state == "pending" and job.attempts == 1
        assert job.available_at > now()


def test_manual_route_edit_waits_for_successful_daily_interval(paid_panel):  # noqa: F811
    db, config, commerce, _, _, _, mode, calls = paid_panel
    panel = commerce.panel_provider
    assert sync_accessible_nodes(db, panel, config)["status"] == "ok"
    first_calls = len(calls)
    assert sync_accessible_nodes(db, panel, config)["status"] == "current"
    assert len(calls) == first_calls
    with db.transaction() as session:
        row = session.get(Setting, "panel_event_routes")
        row.value = row.value | {"nodes": [{**row.value["nodes"][0], "safe_label": "Новое имя"}]}
    assert sync_accessible_nodes(db, panel, config)["status"] == "current"
    assert len(calls) == first_calls
    with db.transaction() as session:
        assert session.get(Setting, "panel_event_routes").value["nodes"][0]["safe_label"] == "Новое имя"
    mode["accessible_nodes"] = []
    with db.transaction() as session:
        session.scalar(select(Product).where(Product.code == "standard")).version += 1
    assert sync_accessible_nodes(db, panel, config)["status"] == "current"
    assert len(calls) == first_calls
    with db.transaction() as session:
        job = session.scalar(select(Job).where(Job.kind == "panel_route_sync"))
        job.payload = job.payload | {"success_at": (now() - timedelta(days=1, seconds=1)).isoformat()}
    assert sync_accessible_nodes(db, panel, config)["status"] == "ok"
    with db.transaction() as session:
        assert session.get(Setting, "panel_event_routes").value["nodes"] == []
