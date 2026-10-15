"""Contract boundaries for numeric Remnawave; all HTTP uses synthetic MockTransport."""

import copy
import importlib
import json
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from sunrise_infrastructure.db import now
from sunrise_infrastructure.panel import PanelReview, PanelUnavailable

SQUAD = "20000000-0000-4000-8000-000000000002"
TG = "70000000000000001"
NONCE = "a" * 32
BINDING = {
    "user_id": None,
    "short_uuid": "b" * 24,
    "username": "sunrise_test_" + NONCE[:20],
    "origin_nonce": NONCE,
}
TARGET = {
    "expires_at": "2027-01-01T00:00:00+00:00",
    "devices_limit": 4,
    "internal_squad_uuid": SQUAD,
}


def user(**overrides):
    return {
        "id": 123,
        "shortUuid": BINDING["short_uuid"],
        "username": BINDING["username"],
        "status": "ACTIVE",
        "expireAt": TARGET["expires_at"],
        "trafficLimitBytes": 0,
        "trafficLimitStrategy": "NO_RESET",
        "telegramId": None,
        "email": None,
        "description": None,
        "tag": None,
        "hwidDeviceLimit": 4,
        "externalSquadUuid": None,
        "trojanPassword": "synthetic-password",
        "ssPassword": "synthetic-password",
        "vlessUuid": "30000000-0000-4000-8000-000000000003",
        "lastTriggeredThreshold": 0,
        "subRevokedAt": None,
        "lastTrafficResetAt": None,
        "createdAt": "2026-01-01T00:00:00.000Z",
        "updatedAt": "2026-01-01T00:00:00.000Z",
        "subscriptionUrl": "https://subscription.example.test/" + BINDING["short_uuid"],
        "activeInternalSquads": [{"uuid": SQUAD, "name": "Synthetic"}],
        "userTraffic": {
            "usedTrafficBytes": 42,
            "lifetimeUsedTrafficBytes": 42,
            "onlineAt": None,
            "firstConnectedAt": None,
            "lastConnectedNodeUuid": None,
        },
    } | overrides


def provider(handler, binding=None, **overrides):
    module = importlib.import_module("sunrise_infrastructure.panel_v3")
    panel = module.RemnawaveV3(
        **(
            dict(
                base_url="https://panel.example.test",
                token="synthetic-placeholder",  # noqa: S106 - MockTransport fixture only
                authorized=True,
                allowed_hosts={"panel.example.test"},
                allowed_squads={SQUAD},
                allowed_subscription_hosts={"subscription.example.test"},
                auto_test_telegram_ids={TG},
                transport=httpx.MockTransport(handler),
            )
            | overrides
        )
    )
    value = copy.deepcopy(binding or BINDING)
    panel.register_test_binding(value, telegram_id=TG)
    return panel, value


def test_numeric_creation_posts_no_fake_uuid_and_returns_verified_id():
    state, calls = {}, []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            body = json.loads(request.content)
            assert "uuid" not in body and "id" not in body
            assert body["username"] == BINDING["username"]
            assert body["shortUuid"] == BINDING["short_uuid"]
            state.update(user())
        return httpx.Response(200, json={"response": state}) if state else httpx.Response(404)

    panel, binding = provider(handler)
    result = panel.apply(binding, TARGET, create=True)
    assert result == {
        "user_id": "123",
        "uuid": None,
        "short_uuid": "b" * 24,
        "connection_url": "https://subscription.example.test/" + "b" * 24,
        "applied_target": {
            "status": "ACTIVE",
            "expires_at": TARGET["expires_at"],
            "devices_limit": TARGET["devices_limit"],
            "internal_squad_uuids": [SQUAD],
        },
        "connection_metadata": {
            "created_at": "2026-01-01T00:00:00+00:00",
            "first_connected_at": None,
            "online_at": None,
            "lifetime_traffic_bytes": "42",
        },
    }
    assert calls == [
        ("GET", "/api/users/by-username/" + BINDING["username"]),
        ("POST", "/api/users"),
        ("GET", "/api/users/123"),
    ]
    assert binding["user_id"] is None


def test_first_username_collision_requires_review_before_any_write():
    calls = []

    def handler(request):
        calls.append(request.method)
        return httpx.Response(200, json={"response": user()})

    panel, binding = provider(handler)
    with pytest.raises(PanelReview):
        panel.apply(binding, TARGET, create=True)
    assert calls == ["GET"]


def test_uncertain_creation_recovers_by_saved_username_without_second_post():
    calls = []

    def handler(request):
        calls.append(request.method)
        assert request.method != "POST"
        if request.method == "PATCH":
            assert json.loads(request.content)["id"] == 123
        return httpx.Response(200, json={"response": user()})

    panel, binding = provider(handler)
    assert panel.apply(binding, TARGET, create=True, uncertain=True)["user_id"] == "123"
    assert calls == ["GET", "PATCH", "GET"]


def test_unknown_creation_missing_after_restart_never_recreates():
    calls = []
    panel, binding = provider(lambda r: calls.append(r.method) or httpx.Response(404))
    with pytest.raises(PanelReview):
        panel.apply(binding, TARGET, create=True, uncertain=True)
    assert calls == ["GET"]


@pytest.mark.parametrize("bad_id", [True, 0, -1, 1.5, "123", 9007199254740992])
def test_untrusted_numeric_response_id_rejected_before_followup_requests(bad_id):
    calls = []

    def handler(request):
        calls.append(request.method)
        return (
            httpx.Response(404)
            if request.method == "GET"
            else httpx.Response(200, json={"response": user(id=bad_id)})
        )

    panel, binding = provider(handler)
    with pytest.raises(PanelReview):
        panel.apply(binding, TARGET, create=True)
    assert calls == ["GET", "POST"]


def test_unregistered_or_forged_origin_cannot_reach_transport():
    panel, binding = provider(lambda _: pytest.fail("Unauthorized identity reached HTTP"))
    with pytest.raises(ValueError):
        panel.apply(binding | {"origin_nonce": "c" * 32}, TARGET, create=True)
    with pytest.raises(ValueError):
        panel.register_test_binding(binding, telegram_id="70000000000000002")


def test_snapshot_is_sanitized_and_individual_delete_uses_verified_numeric_owner():
    device = {
        "hwid": "SYNTHETIC-DEVICE-01",
        "userId": 123,
        "platform": "Android",
        "osVersion": "16",
        "deviceModel": "Synthetic phone",
        "userAgent": "Synthetic/1",
        "requestIp": "192.0.2.10",
        "createdAt": "2026-01-01T00:00:00.000Z",
        "updatedAt": "2026-01-01T00:00:00.000Z",
    }
    devices, writes = [device], []

    def handler(request):
        if request.url.path == "/api/users/123":
            return httpx.Response(200, json={"response": user()})
        if request.method == "POST":
            writes.append(json.loads(request.content))
            devices.clear()
        return httpx.Response(200, json={"response": {"total": len(devices), "devices": devices}})

    panel, binding = provider(handler, BINDING | {"user_id": "123"})
    result = panel.snapshot(binding)
    assert result["traffic_bytes"] == 42
    assert result["devices"][0]["name"] == "Synthetic phone"
    assert "synthetic-password" not in json.dumps(result)
    assert "requestIp" not in json.dumps(result)
    panel.register_device(binding, device["hwid"])
    assert panel.revoke(binding, device["hwid"]) is True
    assert writes == [{"userId": 123, "hwid": "SYNTHETIC-DEVICE-01"}]


def test_snapshot_missing_traffic_remains_unknown_not_zero():
    def handler(request):
        response = (
            user(userTraffic={}) if request.url.path == "/api/users/123" else {"total": 0, "devices": []}
        )
        return httpx.Response(200, json={"response": response})

    panel, binding = provider(handler, BINDING | {"user_id": "123"})
    assert panel.snapshot(binding)["traffic_bytes"] is None


@pytest.mark.parametrize("present", [False, True])
def test_uncertain_numeric_device_delete_is_get_only_even_after_recreation(present):
    calls, starts = [], []
    device = {
        "hwid": "SYNTHETIC-RECREATED",
        "userId": 123,
        "platform": "Android",
        "deviceModel": "Synthetic",
        "createdAt": "2026-10-09T00:00:00Z",
        "updatedAt": "2026-10-09T00:00:00Z",
    }

    def handler(request):
        calls.append(request.method)
        result = (
            user()
            if request.url.path == "/api/users/123"
            else {"total": int(present), "devices": [device] if present else []}
        )
        return httpx.Response(200, json={"response": result})

    panel, binding = provider(handler, BINDING | {"user_id": "123"})
    panel.register_device(binding, device["hwid"])
    if present:
        with pytest.raises(PanelReview):
            panel.revoke(binding, device["hwid"], uncertain=True, before_delete=lambda: starts.append(True))
    else:
        assert (
            panel.revoke(binding, device["hwid"], uncertain=True, before_delete=lambda: starts.append(True))
            is True
        )
    assert calls == ["GET", "GET"] and starts == []


@pytest.mark.parametrize("expiry", [None, "invalid", "2026-10-09T00:00:00"])
def test_numeric_rotation_requires_valid_expiry_before_durable_start_or_post(expiry):
    calls, starts = [], []

    def handler(request):
        calls.append(request.method)
        return httpx.Response(200, json={"response": user(expireAt=expiry)})

    panel, binding = provider(handler, BINDING | {"user_id": "123"})
    with pytest.raises(PanelReview):
        panel.rotate(binding, before_rotate=starts.append)
    assert calls == ["GET"] and starts == []


def test_numeric_rotation_requires_current_connection_pin_before_post():
    calls, starts = [], []
    panel, binding = provider(
        lambda request: (
            calls.append(request.method)
            or httpx.Response(
                200,
                json={"response": user(subscriptionUrl="https://subscription.example.test/unexpected-path")},
            )
        ),
        BINDING | {"user_id": "123"},
    )
    panel.register_test_binding(
        binding, telegram_id=TG, connection_url="https://subscription.example.test/" + binding["short_uuid"]
    )
    with pytest.raises(PanelReview):
        panel.rotate(binding, before_rotate=starts.append)
    assert calls == ["GET"] and starts == []


def test_numeric_fresh_rotation_requires_durable_callback_before_post():
    calls = []
    panel, binding = provider(
        lambda request: calls.append(request.method) or httpx.Response(200, json={"response": user()}),
        BINDING | {"user_id": "123"},
    )
    with pytest.raises(ValueError):
        panel.rotate(binding)
    assert calls == ["GET"]


def test_expired_disabled_target_does_not_extend_or_send_invalid_past_patch():
    past = (now() - timedelta(days=1)).replace(microsecond=0).isoformat()
    target = TARGET | {"expires_at": past, "panel_status": "DISABLED"}
    state, writes = user(expireAt=past), []

    def handler(request):
        if request.method == "PATCH":
            body = json.loads(request.content)
            writes.append(body)
            state["status"] = body["status"]
        return httpx.Response(200, json={"response": state})

    panel, binding = provider(handler, BINDING | {"user_id": "123"})
    panel.apply(binding, target)
    assert "expireAt" not in writes[0]
    assert state["expireAt"] == past
    assert state["status"] == "DISABLED"


def test_uncertain_rotation_changed_link_recovers_without_second_post():
    calls = []
    state = user(shortUuid="c" * 24, subscriptionUrl="https://subscription.example.test/" + "c" * 24)
    panel, binding = provider(
        lambda r: calls.append(r.method) or httpx.Response(200, json={"response": state}),
        BINDING | {"user_id": "123"},
    )
    result = panel.rotate(
        binding, uncertain=True, before_short_uuid="b" * 24, before_expires_at=TARGET["expires_at"]
    )
    assert result["short_uuid"] == "c" * 24
    assert calls == ["GET"]


def test_uncertain_rotation_unchanged_state_requires_review_without_second_post():
    calls = []
    panel, binding = provider(
        lambda r: calls.append(r.method) or httpx.Response(200, json={"response": user()}),
        BINDING | {"user_id": "123"},
    )
    with pytest.raises(PanelReview):
        panel.rotate(
            binding, uncertain=True, before_short_uuid="b" * 24, before_expires_at=TARGET["expires_at"]
        )
    assert calls == ["GET"]


def test_connect_failure_before_creation_is_the_only_safe_fresh_retry():
    def handler(_):
        raise httpx.ConnectError("synthetic network unavailable")

    panel, binding = provider(handler)
    with pytest.raises(PanelUnavailable) as failure:
        panel.apply(binding, TARGET, create=True)
    assert failure.value.create_not_started is True


def test_configured_numeric_contract_uses_separate_adapter_without_v2_uuid_lists():
    from sunrise_infrastructure.panel import configured_panel

    config = SimpleNamespace(
        panel_test_authorized=True,
        panel_base_url="https://panel.example.test",
        panel_contract="3.4_id",
        panel_token="synthetic-placeholder",  # noqa: S106 - synthetic fixture
        panel_allowed_hosts=["panel.example.test"],
        panel_test_squads=[SQUAD],
        panel_subscription_allowed_hosts=["subscription.example.test"],
        panel_test_bindings={},
        test_telegram_recipients=TG,
    )
    panel = configured_panel(
        config, transport=httpx.MockTransport(lambda _: pytest.fail("Composition must not call HTTP"))
    )
    assert panel.contract == "3.4_id" and not hasattr(panel, "uuids")
    panel.client.close()


def test_numeric_device_owner_mismatch_never_reaches_delete():
    writes = []
    foreign = {
        "userId": 124,
        "hwid": "SYNTHETIC-01",
        "createdAt": now().isoformat(),
        "updatedAt": now().isoformat(),
    }

    def handler(request):
        if request.method != "GET":
            writes.append(request.method)
        result = user() if request.url.path == "/api/users/123" else {"total": 1, "devices": [foreign]}
        return httpx.Response(200, json={"response": result})

    panel, binding = provider(handler, BINDING | {"user_id": "123"})
    panel.register_device(binding, foreign["hwid"])
    with pytest.raises(PanelReview):
        panel.revoke(binding, foreign["hwid"])
    assert writes == []


def test_uncertain_rotation_with_changed_paid_expiry_requires_review():
    panel, binding = provider(
        lambda _: httpx.Response(
            200,
            json={
                "response": user(
                    shortUuid="r" * 24,
                    subscriptionUrl="https://subscription.example.test/" + "r" * 24,
                    expireAt="2028-01-01T00:00:00+00:00",
                )
            },
        ),
        BINDING | {"user_id": "123"},
    )
    with pytest.raises(PanelReview):
        panel.rotate(
            binding,
            uncertain=True,
            before_short_uuid=BINDING["short_uuid"],
            before_expires_at=TARGET["expires_at"],
        )


def test_uncertain_rotation_missing_durable_baseline_fails_closed():
    calls = []
    panel, binding = provider(
        lambda request: (
            calls.append(request.method)
            or httpx.Response(
                200,
                json={
                    "response": user(
                        shortUuid="r" * 24, subscriptionUrl="https://subscription.example.test/" + "r" * 24
                    )
                },
            )
        ),
        BINDING | {"user_id": "123"},
    )
    with pytest.raises(PanelReview):
        panel.rotate(binding, uncertain=True, before_short_uuid=BINDING["short_uuid"])
    assert "POST" not in calls
