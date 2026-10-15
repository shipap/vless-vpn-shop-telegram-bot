# ruff: noqa: F811 -- imported shared pytest fixtures are requested by parameter name.
import hashlib
import hmac
import json
from datetime import timedelta

import pytest
from sqlalchemy import select
from sunrise_infrastructure.db import DeviceUnlink, Job, Product, Setting, Subscription, now
from sunrise_infrastructure.notifications import NotificationService
from sunrise_infrastructure.panel_events import PanelEvents
from sunrise_infrastructure.subscription_engagement import SubscriptionEngagement, state_in

from tests.contract.test_scoped_payment_policy import synthetic_scope  # noqa: F401
from tests.integration.test_scoped_panel_runtime_events import paid_panel, scoped_panel_database  # noqa: F401


def recorder(db, settings):
    sent = []
    notifier = NotificationService(
        db, settings, telegram_transport=lambda recipient, payload, key: sent.append((recipient, payload))
    )
    return notifier, sent


def test_scoped_missing_owned_panel_user_delivers_user_and_owner_once(paid_panel):
    db, settings, commerce, aid, _, _, mode, _ = paid_panel
    commerce.dashboard(aid, refresh_panel=True)
    mode["user_present"] = False
    assert commerce.dashboard(aid, refresh_panel=True)["subscriptions"] == []
    notifier, sent = recorder(db, settings)
    notifier.deliver_pending(scoped_only=True)
    assert {p["kind"] for _, p in sent} >= {"subscription_deleted", "subscription_deleted_owner"}
    size = len(sent)
    notifier.deliver_pending(scoped_only=True)
    assert len(sent) == size


def test_archived_subscription_with_removed_admission_binding_cannot_deliver(paid_panel):
    db, settings, commerce, aid, sid, _, mode, _ = paid_panel
    commerce.dashboard(aid, refresh_panel=True)
    mode["user_present"] = False
    assert commerce.dashboard(aid, refresh_panel=True)["subscriptions"] == []
    with db.transaction() as s:
        binding = s.get(Setting, "panel_test:" + sid)
        binding.value = binding.value | {"account_id": "987654321"}
    notifier, sent = recorder(db, settings)
    notifier.deliver_pending(scoped_only=True)
    assert not any(p["kind"].startswith("subscription_deleted") for _, p in sent)


@pytest.mark.parametrize("connected", [False, True])
def test_scoped_help_delivery_uses_persisted_current_intent_and_cancels_after_connection(
    paid_panel, connected
):
    db, settings, commerce, aid, sid, _, _, _ = paid_panel
    commerce.dashboard(aid, refresh_panel=True)
    engagement = SubscriptionEngagement(commerce)
    with db.transaction() as s:
        row = s.get(Setting, "engagement:" + sid)
        row.value = row.value | {"created_at": (now() - timedelta(hours=25)).isoformat()}
        state = state_in(s, s.get(Subscription, sid))
        notification = engagement.notifications.emit_in(
            s,
            aid,
            "connection-help:" + sid + ":" + state.origin_nonce,
            "connection_help",
            "Помощь с подключением",
            "Если есть проблемы с подключением, обратитесь в поддержку.",
            channel="telegram",
            allow_push=False,
        )
        row.value = row.value | {"help_intent_id": notification.id}
        if connected:
            row.value = row.value | {"ever_connected_at": now().isoformat(), "known_never_connected": False}
    notifier, sent = recorder(db, settings)
    notifier.deliver_pending(scoped_only=True)
    assert [p["kind"] for _, p in sent].count("connection_help") == int(not connected)
    assert notifier.deliver_pending(scoped_only=True) == 0


def test_scoped_new_device_and_external_unlink_owner_notice_are_delivered_once(paid_panel):
    db, settings, commerce, aid, sid, _, mode, _ = paid_panel
    commerce.dashboard(aid, refresh_panel=True)
    stamp = now().isoformat()
    mode["devices"] = [
        {
            "hwid": "synthetic-hwid-1",
            "userId": 432,
            "deviceModel": "Owner Device",
            "platform": "Android",
            "createdAt": stamp,
            "updatedAt": stamp,
        }
    ]
    commerce.dashboard(aid, refresh_panel=True)
    notifier, sent = recorder(db, settings)
    notifier.deliver_pending(scoped_only=True)
    assert [p["kind"] for _, p in sent].count("new_device") == 1
    notifier.deliver_pending(scoped_only=True)
    assert [p["kind"] for _, p in sent].count("new_device") == 1
    mode["devices"] = []
    commerce.dashboard(aid, refresh_panel=True)
    notifier.deliver_pending(scoped_only=True)
    assert [p["kind"] for _, p in sent].count("device_unlink") == 1
    with db.transaction() as s:
        event = s.scalar(select(DeviceUnlink).where(DeviceUnlink.account_id == int(aid)))
        assert event and event.unlink_mode is None
    assert all("synthetic-hwid-1" not in p["body"] for _, p in sent)


@pytest.mark.parametrize("baseline", [False, True])
def test_signed_new_device_webhook_reconciles_then_notifies_once(paid_panel, baseline):
    db, settings, commerce, aid, sid, user, mode, _ = paid_panel
    with db.transaction() as session:
        subscription = session.get(Subscription, sid)
        subscription.name = "Моя подписка"
        product = session.get(Product, subscription.product_id)
        product.name = "Стандартный"
        product.content = {
            "name": [
                {"type": "emoji", "id": "9007199254740993123", "fallback": "💎", "label": "Тариф"},
                {"type": "text", "text": " Стандартный"},
            ]
        }
    if baseline:
        commerce.dashboard(aid, refresh_panel=True)
    stamp = now().isoformat()
    device = {
        "hwid": "synthetic-hwid-webhook",
        "userId": 432,
        "deviceModel": "Owner Device",
        "platform": "Android",
        "createdAt": stamp,
        "updatedAt": stamp,
    }
    mode["devices"] = [device]
    settings.panel_webhook_secret = "synthetic-webhook-secret"  # noqa: S105 -- synthetic HTTP transport only.
    raw = json.dumps(
        {
            "scope": "user_hwid_devices",
            "event": "user_hwid_devices.added",
            "timestamp": stamp,
            "data": {"user": user, "hwidUserDevice": device},
        }
    ).encode()
    headers = {
        "X-Remnawave-Timestamp": stamp,
        "X-Remnawave-Signature": hmac.new(
            settings.panel_webhook_secret.encode(), raw, hashlib.sha256
        ).hexdigest(),
    }
    events = PanelEvents(db, commerce.panel_provider, settings)
    assert events.accept(raw, headers)["status"] == "accepted"
    assert events.accept(raw, headers)["status"] == "duplicate"
    assert events.reconcile_pending() == 1
    notifier, sent = recorder(db, settings)
    notifier.deliver_pending(scoped_only=True)
    assert [p["kind"] for _, p in sent].count("new_device") == 1
    notice = next(payload for _, payload in sent if payload["kind"] == "new_device")
    assert "«💎 Стандартный»" in notice["body"]
    assert "Моя подписка" not in notice["body"]
    from sunrise_core.telegram_content import render_classic, render_plain

    assert render_plain(notice["nodes"]) == notice["body"]
    assert '<tg-emoji emoji-id="9007199254740993123">💎</tg-emoji>' in render_classic(
        notice["nodes"], custom_emoji=True
    )
    assert events.reconcile_pending() == 0
    notifier.deliver_pending(scoped_only=True)
    assert [p["kind"] for _, p in sent].count("new_device") == 1


@pytest.mark.parametrize("case", ["mismatched", "absent", "stale_generation"])
def test_signed_added_device_does_not_announce_unmatched_current_generation(paid_panel, case):
    db, settings, commerce, _, _, user, mode, _ = paid_panel
    stamp = now().isoformat()
    old = (now() - timedelta(hours=1)).isoformat()
    event_device = {
        "hwid": "synthetic-hwid-webhook",
        "userId": 432,
        "deviceModel": "Old Device",
        "platform": "Android",
        "createdAt": old,
        "updatedAt": old,
    }
    mode["devices"] = (
        []
        if case == "absent"
        else [event_device | {"hwid": "synthetic-hwid-other"}]
        if case == "mismatched"
        else [event_device | {"createdAt": stamp, "updatedAt": stamp}]
    )
    settings.panel_webhook_secret = "synthetic-webhook-secret"  # noqa: S105 -- synthetic HTTP transport.
    raw = json.dumps(
        {
            "scope": "user_hwid_devices",
            "event": "user_hwid_devices.added",
            "timestamp": stamp,
            "data": {"user": user, "hwidUserDevice": event_device},
        }
    ).encode()
    headers = {
        "X-Remnawave-Timestamp": stamp,
        "X-Remnawave-Signature": hmac.new(
            settings.panel_webhook_secret.encode(), raw, hashlib.sha256
        ).hexdigest(),
    }
    events = PanelEvents(db, commerce.panel_provider, settings)
    assert events.accept(raw, headers)["status"] == "accepted"
    assert events.reconcile_pending() == 1
    notifier, sent = recorder(db, settings)
    notifier.deliver_pending(scoped_only=True)
    assert not any(payload["kind"] == "new_device" for _, payload in sent)


def test_added_webhook_announces_present_device_even_after_dashboard_baseline(paid_panel):
    db, settings, commerce, aid, _, user, mode, _ = paid_panel
    stamp = now().isoformat()
    device = {
        "hwid": "synthetic-hwid-webhook",
        "userId": 432,
        "deviceModel": "Owner Device",
        "platform": "Android",
        "createdAt": stamp,
        "updatedAt": stamp,
    }
    mode["devices"] = [device]
    commerce.dashboard(aid, refresh_panel=True)
    settings.panel_webhook_secret = "synthetic-webhook-secret"  # noqa: S105 -- synthetic HTTP transport.
    raw = json.dumps(
        {
            "scope": "user_hwid_devices",
            "event": "user_hwid_devices.added",
            "timestamp": stamp,
            "data": {"user": user, "hwidUserDevice": device},
        }
    ).encode()
    headers = {
        "X-Remnawave-Timestamp": stamp,
        "X-Remnawave-Signature": hmac.new(
            settings.panel_webhook_secret.encode(), raw, hashlib.sha256
        ).hexdigest(),
    }
    events = PanelEvents(db, commerce.panel_provider, settings)
    assert events.accept(raw, headers)["status"] == "accepted"
    assert events.reconcile_pending() == 1
    notifier, sent = recorder(db, settings)
    notifier.deliver_pending(scoped_only=True)
    assert [payload["kind"] for _, payload in sent].count("new_device") == 1


@pytest.mark.parametrize("connected", [False, True])
def test_due_24h_timer_rechecks_panel_and_only_emits_for_never_connected(paid_panel, connected):
    db, settings, commerce, aid, sid, user, _, _ = paid_panel
    commerce.dashboard(aid, refresh_panel=True)
    created = (now() - timedelta(hours=25)).isoformat()
    user["createdAt"] = created
    if connected:
        user["userTraffic"]["firstConnectedAt"] = now().isoformat()
        user["userTraffic"]["lifetimeUsedTrafficBytes"] = 100
    with db.transaction() as s:
        state = s.get(Setting, "engagement:" + sid)
        state.value = state.value | {"created_at": created}
        job = s.scalar(select(Job).where(Job.kind == "subscription_engagement", Job.account_id == int(aid)))
        assert job and job.available_at > now()
        job.available_at = now() - timedelta(seconds=1)
    engagement = SubscriptionEngagement(commerce)
    assert engagement.run_due() == 1
    notifier, sent = recorder(db, settings)
    notifier.deliver_pending(scoped_only=True)
    assert [p["kind"] for _, p in sent].count("connection_help") == int(not connected)
    assert engagement.run_due() == 0
    notifier.deliver_pending(scoped_only=True)
    assert [p["kind"] for _, p in sent].count("connection_help") == int(not connected)


def test_notification_pages_hide_technical_ready_and_do_not_repeat_items(paid_panel):
    db, settings, _, aid, _, _, _, _ = paid_panel
    notifier, _ = recorder(db, settings)
    with db.transaction() as s:
        for i in range(12):
            notifier.emit_in(
                s, aid, "page:" + str(i), "generic", "Страница " + str(i), "Текст", channel="inbox"
            )
        notifier.emit_in(
            s, aid, "technical-ready", "access_ready", "Доступ готов", "Технический маркер", channel="inbox"
        )
    first = notifier.list(aid, limit=5, offset=0)
    second = notifier.list(aid, limit=5, offset=5)
    assert len(first["items"]) == len(second["items"]) == 5
    assert first["next_offset"] == 5 and second["previous_offset"] == 0
    assert not {n["id"] for n in first["items"]} & {n["id"] for n in second["items"]}
    assert all(n["kind"] != "access_ready" for n in first["items"] + second["items"])
