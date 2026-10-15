"""Scoped panel facts are observed for the exact paid owner without provider writes."""

import hashlib
import hmac
import json
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select
from sunrise_core.errors import DomainError
from sunrise_infrastructure.db import (
    Account,
    Device,
    DeviceUnlink,
    Inbox,
    Job,
    Notification,
    Order,
    PanelEvent,
    Payment,
    Product,
    ProviderOperation,
    Quote,
    Segment,
    Setting,
    StaffRole,
    Subscription,
    now,
)
from sunrise_infrastructure.panel import configured_panel
from sunrise_infrastructure.panel_events import PanelEvents

from tests.contract.test_scoped_payment_policy import synthetic_scope  # noqa: F401
from tests.integration.test_scoped_real_invoices import prepared_invoice
from tests.integration.test_scoped_real_invoices import scoped_database as base_scoped_database

NODE = "32345678-1234-1234-1234-123456789abc"


@pytest.fixture
def scoped_panel_database(synthetic_scope):  # noqa: F811
    config, path, data = synthetic_scope
    data["panel"]["node_uuids"] = [NODE]
    data["panel"]["subscription_hosts"] = ["vpn.synthetic.invalid", "alias.synthetic.invalid"]
    config["panel_test_node_uuids"] = [NODE]
    config["panel_subscription_allowed_hosts"] = ["vpn.synthetic.invalid", "alias.synthetic.invalid"]
    config["panel_webhook_test_authorized"] = True
    config["panel_username_prefix"] = "sr"
    config["owner_id"] = data["telegram_id"]
    config["admin_email_allowlist"] = ["owner@example.test"]
    yield from base_scoped_database.__wrapped__((config, path, data))


@pytest.fixture
def paid_panel(scoped_panel_database, request):
    database, config, policy, commerce, account_id = scoped_panel_database
    user = {}
    calls = []
    mode = {
        "user_present": True,
        "node_authorized": True,
        "node_disabled": False,
        "accessible_nodes": [NODE],
        "url_host": "vpn.synthetic.invalid",
        "url_path": "/sub/",
        "devices": [],
        "remote_conflicts": (
            {"sr_synthetic_owner_23456_1"}
            if getattr(request, "param", None) in {"remote", "wrong_code", "auth_401", "crash"}
            else set()
        ),
    }
    request_fixture = request

    def respond(request):
        calls.append((request.method, request.url.path))
        path = request.url.path
        if path == "/api/nodes/" + NODE:
            return (
                httpx.Response(200, json={"response": {"uuid": NODE, "isConnected": True, "isDisabled": mode["node_disabled"]}})
                if mode["node_authorized"]
                else httpx.Response(404)
            )
        if path.startswith("/api/internal-squads/") and path.endswith("/accessible-nodes"):
            squad = path.removeprefix("/api/internal-squads/").removesuffix("/accessible-nodes")
            return httpx.Response(200, json={"response": {
                "squadUuid": squad,
                "accessibleNodes": [{"uuid": item} for item in mode["accessible_nodes"]],
            }})
        if path.startswith("/api/hwid/devices/"):
            return httpx.Response(200, json={"response": {"devices": mode["devices"], "total": len(mode["devices"])}})
        if request.method == "POST" and path == "/api/users":
            body = json.loads(request.content)
            if body["username"] in mode["remote_conflicts"]:
                if getattr(request_fixture, "param", None) == "auth_401":
                    return httpx.Response(401)
                return httpx.Response(400, json={
                    "timestamp": now().isoformat(), "path": "/api/users",
                    "message": "User username already exists",
                    "errorCode": "A018" if getattr(request_fixture, "param", None) == "wrong_code" else "A019",
                })
            user.update(
                body
                | {
                    "id": 432,
                    "activeInternalSquads": [{"uuid": body["activeInternalSquads"][0]}],
                    "subscriptionUrl": "https://vpn.synthetic.invalid/sub/" + body["shortUuid"],
                    "userTraffic": {
                        "usedTrafficBytes": 0,
                        "firstConnectedAt": None,
                        "onlineAt": None,
                        "lifetimeUsedTrafficBytes": 0,
                    },
                    "createdAt": now().isoformat(),
                }
            )
        if path == "/api/users/432" and not mode["user_present"]:
            return httpx.Response(404)
        if user:
            user["subscriptionUrl"] = "https://" + mode["url_host"] + mode["url_path"] + user["shortUuid"]
        return httpx.Response(200, json={"response": user}) if user else httpx.Response(404)

    config.panel_token = "synthetic-panel-key"  # noqa: S105 -- in-memory HTTP only
    panel = configured_panel(config, transport=httpx.MockTransport(respond))
    if getattr(request, "param", None) == "crash":
        register = panel.register_test_binding

        def crash_after_candidate(*args, **kwargs):
            if args[0]["username"].endswith("_2"):
                raise RuntimeError("synthetic_worker_crash")
            return register(*args, **kwargs)

        panel.register_test_binding = crash_after_candidate
    commerce.panel_provider = panel
    with database.transaction() as session:
        session.add(StaffRole(account_id=account_id, role="owner", granted_by=account_id))
        session.get(Account, account_id).telegram_username = "synthetic_owner"
        if getattr(request, "param", None) is True:
            session.add(Setting(key="panel_test:existing-own-name", value={
                "account_id": str(account_id), "naming": {"prefix": "sr", "sequence": 1},
            }))
    provisioning = commerce.provisioning
    order, billing, provider, _, _ = prepared_invoice(scoped_panel_database)
    billing.poll_known_payments()
    assert billing.reconcile_inbox(provider) == 1
    if getattr(request, "param", None) == "crash":
        with pytest.raises(RuntimeError, match="synthetic_worker_crash"):
            provisioning.run_jobs()
        with database.transaction() as session:
            interrupted = session.scalar(select(Job).where(Job.kind == "provision"))
            assert interrupted.payload["create_started"] is False
            assert interrupted.payload["panel_binding"]["username"].endswith("_2")
            interrupted.available_at = now()
        panel.register_test_binding = register
    completed = provisioning.run_jobs()
    with database.transaction() as session:
        errors = [(job.state, job.last_error) for job in session.scalars(select(Job).where(Job.kind == "provision"))]
    if getattr(request, "param", None) in {"wrong_code", "auth_401"}:
        assert completed == 0, errors
    else:
        assert completed == 1, errors
    with database.transaction() as session:
        sub = session.scalar(select(Subscription).where(Subscription.account_id == account_id))
        sid = sub.id
    yield database, config, commerce, account_id, sid, user, mode, calls
    panel.client.close()
    provider.client.close()


def test_scoped_new_binding_uses_configured_prefix_and_customer_label(paid_panel):
    database, _, _, _, sid, user, _, _ = paid_panel
    with database.transaction() as session:
        proof = session.get(Setting, "panel_test:" + sid).value
    assert user["username"] == "sr_synthetic_owner_23456_1"
    assert proof["binding"]["origin_nonce"] not in user["username"]
    assert "test" not in user["username"]


@pytest.mark.parametrize("paid_panel", [True], indirect=True)
def test_scoped_local_username_reservation_advances_sequence_without_foreign_get(paid_panel):
    database, _, _, _, sid, user, _, calls = paid_panel
    with database.transaction() as session:
        proof = session.get(Setting, "panel_test:" + sid).value
    assert user["username"] == "sr_synthetic_owner_23456_2"
    assert proof["naming"]["sequence"] == 2
    assert proof["binding"]["username"] == user["username"]
    assert not any(path.startswith("/api/users/by-username/") for _, path in calls)
    assert sum(method == "POST" and path == "/api/users" for method, path in calls) == 1


@pytest.mark.parametrize("paid_panel", ["remote"], indirect=True)
def test_scoped_typed_username_conflict_retries_without_foreign_get(paid_panel):
    database, _, _, _, sid, user, _, calls = paid_panel
    with database.transaction() as session:
        proof = session.get(Setting, "panel_test:" + sid).value
    assert user["username"] == "sr_synthetic_owner_23456_2"
    assert proof["naming"]["sequence"] == 2
    assert proof["binding"]["username"] == user["username"]
    assert proof["creation_intent"]["username"] == user["username"]
    assert not any(path.startswith("/api/users/by-username/") for _, path in calls)
    assert sum(method == "POST" and path == "/api/users" for method, path in calls) == 2


@pytest.mark.parametrize("paid_panel", ["wrong_code", "auth_401"], indirect=True)
def test_other_panel_create_errors_never_select_a_second_name(paid_panel):
    database, _, _, _, sid, user, _, calls = paid_panel
    with database.transaction() as session:
        proof = session.get(Setting, "panel_test:" + sid).value
    assert user == {}
    assert proof["naming"]["sequence"] == 1
    assert sum(method == "POST" and path == "/api/users" for method, path in calls) == 1
    assert not any(path.startswith("/api/users/by-username/") for _, path in calls)


@pytest.mark.parametrize("paid_panel", ["crash"], indirect=True)
def test_verified_name_conflict_candidate_survives_worker_restart_without_duplicate_first_post(paid_panel):
    database, _, _, _, sid, user, _, calls = paid_panel
    with database.transaction() as session:
        proof = session.get(Setting, "panel_test:" + sid).value
    assert user["username"] == "sr_synthetic_owner_23456_2"
    assert proof["naming"]["sequence"] == 2
    assert sum(method == "POST" and path == "/api/users" for method, path in calls) == 2


def test_scoped_panel_adopts_authenticated_host_only_alias_without_changing_paid_facts(paid_panel):
    database, _, commerce, account_id, sid, _, mode, _ = paid_panel
    commerce.dashboard(account_id, refresh_panel=True)
    with database.transaction() as session:
        sub = session.get(Subscription, sid)
        old_url = sub.connection_url
        paid = [(row.id, row.paid_value) for row in session.scalars(select(Segment))]
        payments = session.scalar(select(func.count()).select_from(Payment))
    mode["url_host"] = "alias.synthetic.invalid"
    result = commerce.dashboard(account_id, refresh_panel=True)
    new_url = old_url.replace("vpn.synthetic.invalid", "alias.synthetic.invalid")
    assert result["subscriptions"][0]["connection_url"] == new_url
    with database.transaction() as session:
        assert session.get(Subscription, sid).connection_url == new_url
        assert session.get(Setting, "panel_test:" + sid).value["connection_url"] == new_url
        assert [(row.id, row.paid_value) for row in session.scalars(select(Segment))] == paid
        assert session.scalar(select(func.count()).select_from(Payment)) == payments


def test_scoped_panel_does_not_adopt_alias_with_changed_subscription_path(paid_panel):
    database, _, commerce, account_id, sid, _, mode, _ = paid_panel
    with database.transaction() as session:
        old_url = session.get(Subscription, sid).connection_url
    mode["url_host"] = "alias.synthetic.invalid"
    mode["url_path"] = "/other/"
    commerce.dashboard(account_id, refresh_panel=True)
    with database.transaction() as session:
        assert session.get(Subscription, sid).connection_url == old_url
        assert session.get(Setting, "panel_test:" + sid).value["connection_url"] == old_url


def test_external_panel_device_deletion_records_unknown_operation_once(paid_panel):
    database, _, commerce, account_id, sid, _, mode, _ = paid_panel
    stamp = now().isoformat()
    mode["devices"] = [{
        "hwid": "synthetic-hwid-1", "userId": 432, "deviceModel": "Test Device",
        "platform": "Android", "createdAt": stamp, "updatedAt": stamp,
    }]
    commerce.dashboard(account_id, refresh_panel=True)
    with database.transaction() as session:
        device = session.scalar(select(Device).where(Device.subscription_id == sid))
        assert device is not None and not device.revoked
    mode["devices"] = []
    commerce.dashboard(account_id, refresh_panel=True)
    commerce.dashboard(account_id, refresh_panel=True)
    with database.transaction() as session:
        event = session.scalar(select(DeviceUnlink).where(DeviceUnlink.device_id == device.id))
        assert event is not None and event.unlink_mode is None and event.unlink_operation is None
        assert session.get(Device, device.id).revoked is True
        assert session.scalar(select(func.count()).select_from(DeviceUnlink).where(DeviceUnlink.device_id == device.id)) == 1
        owner_notice = session.scalar(select(Notification).where(Notification.event_key == "owner-device-unlink:" + event.id))
        assert owner_notice is not None
        assert "synthetic-hwid-1" not in owner_notice.body


def test_quote_freezes_validated_product_name_ast_before_catalog_edit(scoped_panel_database):
    database, _, _, commerce, account_id = scoped_panel_database
    name = [
        {"type": "emoji", "fallback": "⭐", "label": "Премиум"},
        {"type": "text", "text": " Премиум"},
    ]
    with database.transaction() as session:
        product = session.scalar(select(Product).where(Product.code == "standard"))
        product.content = (product.content or {}) | {"name": name}
    quote = commerce.quote(account_id, "standard", 30, "purchase", payment_method="sbp")
    with database.transaction() as session:
        frozen = session.get(Quote, quote["id"]).snapshot["product_content_name"]
        product = session.scalar(select(Product).where(Product.code == "standard"))
        product.content = product.content | {"name": [{"type": "text", "text": "После правки"}]}
    assert frozen == name


def test_signed_scoped_node_disabled_creates_only_mapped_feed_item(paid_panel):
    from sunrise_infrastructure.panel_news import node_news

    database, config, commerce, account_id, _, _, mode, _ = paid_panel
    with database.transaction() as session:
        product = session.scalar(select(Product).where(Product.code == "standard"))
        session.add(
            Setting(
                key="panel_event_routes",
                value={
                    "version": 1,
                    "environment": "scoped_live",
                    "panel_origin": config.panel_base_url,
                    "contract": "3.4_id",
                    "nodes": [{"node_uuid": NODE, "product_ids": [product.id], "safe_label": "Узел 1"}],
                    "infra": [],
                },
            )
        )
    config.panel_webhook_secret = "synthetic-scoped-webhook-secret"  # noqa: S105
    mode["node_disabled"] = True
    raw = json.dumps(
        {"scope": "node", "event": "node.disabled", "timestamp": now().isoformat(), "data": {"uuid": NODE}}
    ).encode()
    stamp = json.loads(raw)["timestamp"]
    headers = {
        "X-Remnawave-Signature": hmac.new(config.panel_webhook_secret.encode(), raw, hashlib.sha256).hexdigest(),
        "X-Remnawave-Timestamp": stamp,
    }
    events = PanelEvents(database, commerce.panel_provider, config)
    assert events.accept(raw, headers)["status"] == "accepted"
    assert events.reconcile_pending() == 1
    feed = node_news(commerce, account_id)
    assert [(item["title"], item["state"]) for item in feed["items"]] == [("Узел 1", "outage")]


def test_scoped_news_ignores_old_fake_card_but_keeps_paid_owned_node(paid_panel):
    from sunrise_infrastructure.panel_news import node_news

    database, config, commerce, account_id, sid, _, _, calls = paid_panel
    with database.transaction() as session:
        product = session.scalar(select(Product).where(Product.code == "standard"))
        legacy_product = session.scalar(select(Product).where(Product.code == "universal"))
        session.add(
            Subscription(
                id="legacy-news-card",
                account_id=account_id,
                product_id=legacy_product.id,
                name="Старая тестовая подписка",
                environment="local_test",
                status="active",
                panel_user_id=123,
                devices_limit=1,
            )
        )
        session.add(
            Setting(
                key="panel_event_routes",
                value={
                    "version": 1,
                    "environment": "scoped_live",
                    "panel_origin": config.panel_base_url,
                    "contract": "3.4_id",
                    "nodes": [{"node_uuid": NODE, "product_ids": [product.id], "safe_label": "Узел 1"}],
                    "infra": [],
                },
            )
        )
        session.add(
            Setting(
                key="node_news:synthetic-owned-node",
                value={
                    "version": 1,
                    "id": "synthetic-owned-news",
                    "node_uuid": NODE,
                    "panel_origin": config.panel_base_url,
                    "environment": "scoped_live",
                    "contract": "3.4_id",
                    "state": "outage",
                    "occurred_at": now().isoformat(),
                },
            )
        )
    before = list(calls)
    assert [(item["title"], item["state"]) for item in node_news(commerce, account_id)["items"]] == [
        ("Узел 1", "outage")
    ]
    assert calls == before
    with database.transaction() as session:
        route = session.get(Setting, "panel_event_routes")
        route.value = route.value | {
            "nodes": [{"node_uuid": NODE, "product_ids": [legacy_product.id], "safe_label": "Узел 1"}]
        }
    assert node_news(commerce, account_id)["items"] == []
    with database.transaction() as session:
        marker = session.get(Setting, "panel_test:" + sid)
        marker.value = marker.value | {"binding": marker.value["binding"] | {"user_id": "999"}}
    with pytest.raises(DomainError) as denied:
        node_news(commerce, account_id)
    assert denied.value.code == "scoped_test_subscription_denied"


def test_scoped_node_event_rechecks_admission_before_durable_inbox(paid_panel):
    database, config, commerce, _, _, _, _, _ = paid_panel
    config.panel_webhook_secret = "synthetic-scoped-webhook-secret"  # noqa: S105
    raw = json.dumps(
        {"scope": "node", "event": "node.disabled", "timestamp": now().isoformat(), "data": {"uuid": NODE}}
    ).encode()
    headers = {
        "X-Remnawave-Signature": hmac.new(config.panel_webhook_secret.encode(), raw, hashlib.sha256).hexdigest(),
        "X-Remnawave-Timestamp": json.loads(raw)["timestamp"],
    }
    with database.transaction() as session:
        marker = session.get(Setting, "scoped_test_admission")
        marker.value = marker.value | {"scope_id": "revoked"}
    with pytest.raises(DomainError):
        PanelEvents(database, commerce.panel_provider, config).accept(raw, headers)
    with database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(PanelEvent)) == 0


def test_scoped_never_connected_help_ignores_foreign_inbox_but_blocks_own_unresolved(paid_panel):
    from sunrise_infrastructure.subscription_engagement import SubscriptionEngagement, state_in

    database, _, commerce, account_id, sid, _, _, _ = paid_panel
    commerce.dashboard(account_id, refresh_panel=True)
    engagement = SubscriptionEngagement(commerce)
    with database.transaction() as session:
        row = session.get(Setting, "engagement:" + sid)
        assert row is not None and row.value["known_never_connected"] is True
        row.value = row.value | {"created_at": (now() - timedelta(hours=25)).isoformat()}
        session.add(
            Inbox(event_key="unrelated-local-test", payload={"order_id": "unrelated"}, state="pending")
        )
        old_quote = Quote(account_id=account_id, snapshot={}, expires_at=now() + timedelta(hours=1))
        session.add(old_quote)
        session.flush()
        old_order = Order(
            account_id=account_id,
            quote_id=old_quote.id,
            idempotency_key="old-local-test",
            amount=Decimal("1.00"),
            currency="RUB",
            environment="local_test",
            status="awaiting_payment",
        )
        session.add(old_order)
        session.flush()
        session.add(ProviderOperation(order_id=old_order.id, terminal="synthetic", state="prepared", request={}))
    with database.transaction() as session:
        sub = session.get(Subscription, sid)
        assert engagement._help_due_in(session, sub, state_in(session, sub), now()) is True
        order = session.scalar(select(Order).where(Order.subscription_id == sid))
        session.add(Inbox(event_key="own-scoped", payload={"order_id": order.id}, state="pending"))
    with database.transaction() as session:
        sub = session.get(Subscription, sid)
        assert engagement._help_due_in(session, sub, state_in(session, sub), now()) is False


@pytest.mark.parametrize("node_authorized", [True, False])
def test_scoped_own_get404_requires_same_token_admitted_node_before_archive(paid_panel, node_authorized):
    database, _, commerce, account_id, sid, _, mode, calls = paid_panel
    commerce.dashboard(account_id, refresh_panel=True)
    with database.transaction() as session:
        original = session.get(Subscription, sid)
        identity = original.panel_user_id
        segments = [(row.id, row.starts_at, row.ends_at, row.paid_value) for row in session.scalars(select(Segment))]
        payments = session.scalar(select(func.count()).select_from(Payment))
    mode["user_present"] = False
    mode["node_authorized"] = node_authorized
    dashboard = commerce.dashboard(account_id, refresh_panel=True)
    with database.transaction() as session:
        archived = session.get(Setting, "archive:" + sid)
        assert (archived is not None) is node_authorized
        if archived is not None:
            from sunrise_infrastructure.subscription_archive import ArchiveState, owner_deletion_message

            text = owner_deletion_message(ArchiveState.model_validate(archived.value))
            assert "Причина: подтверждённое отсутствие в панели" in text
            assert "7 дней после окончания" not in text
        assert session.get(Subscription, sid).panel_user_id == identity
        assert [(row.id, row.starts_at, row.ends_at, row.paid_value) for row in session.scalars(select(Segment))] == segments
        assert session.scalar(select(func.count()).select_from(Payment)) == payments
        assert session.scalar(select(func.count()).select_from(Notification).where(Notification.kind == "subscription_deleted")) == (1 if node_authorized else 0)
    assert (sid not in {item["id"] for item in dashboard["subscriptions"]}) is node_authorized
    assert all(method == "GET" for method, _ in calls[-4:])

