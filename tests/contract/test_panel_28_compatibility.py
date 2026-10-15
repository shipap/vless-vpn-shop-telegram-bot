"""Pinned Remnawave 2.8.1 UUID commands never widen the original identity guards."""

import json
from uuid import uuid4

import httpx
import pytest
from sunrise_infrastructure.panel import PanelReview
from test_panel_auto import TARGET, auto_panel, binding, panel_user

OWNER = "70000000000000001"
NODE = "20000000-0000-4000-8000-000000000003"


def device(hwid="SYNTHETIC-01", **overrides):
    return {
        "hwid": hwid,
        "userId": 123,
        "platform": "Android",
        "deviceModel": "Синтетический телефон",
        "createdAt": "2026-10-01T00:00:00Z",
        "updatedAt": "2026-10-02T00:00:00Z",
    } | overrides


def fixture_panel(**overrides):
    identity = binding()
    state = {
        "user": panel_user(
            identity, id=123, userTraffic={"usedTrafficBytes": 42}, trafficLimitBytes=0, subRevokedAt=None
        ),
        "devices": [device()],
        "calls": [],
        "timeout": False,
    }

    def handler(request):
        state["calls"].append((request.method, request.url.path))
        if request.url.path.startswith("/api/nodes/"):
            response = {
                "uuid": NODE,
                "name": "Тестовый узел",
                "isConnected": True,
                "isDisabled": False,
                "countryCode": "DE",
            }
        elif request.method == "POST" and request.url.path.endswith("/actions/revoke"):
            body = json.loads(request.content)
            if not body["revokeOnlyPasswords"]:
                state["user"]["shortUuid"] = body["shortUuid"]
                state["user"]["subscriptionUrl"] = (
                    "https://own-new-connect.example.test/subscription/" + body["shortUuid"]
                )
            state["user"]["subRevokedAt"] = "2026-10-03T00:00:00Z"
            if state["timeout"]:
                raise httpx.ReadTimeout("synthetic ambiguous response")
            response = state["user"]
        elif request.url.path.startswith("/api/hwid/devices/"):
            response = {"total": len(state["devices"]), "devices": state["devices"]}
        else:
            response = state["user"]
        return httpx.Response(200, json={"response": response})

    panel = auto_panel(handler, **overrides)
    panel.register_test_binding(identity, telegram_id=OWNER, connection_url=state["user"]["subscriptionUrl"])
    return panel, identity, state


def test_panel28_snapshot_uses_uuid_and_crosschecks_numeric_device_owner():
    panel, identity, state = fixture_panel()
    assert panel.contract == "2.8_uuid"
    snapshot = panel.snapshot(identity)
    assert snapshot["user_id"] == identity["uuid"] and snapshot["traffic_bytes"] == 42
    assert snapshot["traffic_limit_bytes"] == 0 and snapshot["devices"][0]["name"] == "Синтетический телефон"
    state["devices"][0]["userId"] = 124
    with pytest.raises(PanelReview):
        panel.snapshot(identity)


@pytest.mark.parametrize("value", [True, -1, 9007199254740992, "42"])
def test_panel28_invalid_traffic_is_never_zero(value):
    panel, identity, state = fixture_panel()
    state["user"]["userTraffic"]["usedTrafficBytes"] = value
    with pytest.raises(PanelReview):
        panel.snapshot(identity)


@pytest.mark.parametrize("password_only", [False, True])
def test_panel28_rotation_proves_exact_readback_and_preserves_expiry(password_only):
    panel, identity, state = fixture_panel()
    proof = {}
    result = panel.rotate(identity, revoke_only_passwords=password_only, before_rotate=proof.update)
    assert result["uuid"] == identity["uuid"]
    assert state["user"]["expireAt"] == TARGET["expires_at"]
    assert result["short_uuid"] == (identity["short_uuid"] if password_only else proof["next_short_uuid"])
    assert proof["before_expires_at"] == TARGET["expires_at"]
    assert sum(method == "POST" for method, _ in state["calls"]) == 1


@pytest.mark.parametrize("expiry", [None, "invalid", "2026-99-01T00:00:00Z"])
@pytest.mark.parametrize("password_only", [False, True])
def test_panel28_rotation_rejects_unverified_paid_expiry_before_start(expiry, password_only):
    panel, identity, state = fixture_panel()
    state["user"]["expireAt"] = expiry
    starts = []
    with pytest.raises(PanelReview):
        panel.rotate(identity, revoke_only_passwords=password_only, before_rotate=starts.append)
    assert starts == []
    assert all(method != "POST" for method, _ in state["calls"])


def test_panel28_uncertain_rotation_reconciles_without_second_post():
    from sunrise_infrastructure.panel import PanelUnavailable

    panel, identity, state = fixture_panel()
    state["timeout"] = True
    proof = {}
    with pytest.raises(PanelUnavailable):
        panel.rotate(identity, before_rotate=proof.update)
    result = panel.rotate(
        identity,
        uncertain=True,
        **{
            "before_short_uuid": proof["before_short_uuid"],
            "before_expires_at": proof["before_expires_at"],
            "before_sub_revoked_at": proof["before_sub_revoked_at"],
            "expected_short_uuid": proof["next_short_uuid"],
        },
    )
    assert result["short_uuid"] == proof["next_short_uuid"]
    assert sum(method == "POST" for method, _ in state["calls"]) == 1
    state["user"]["shortUuid"] = uuid4().hex[:24]
    with pytest.raises(PanelReview):
        panel.rotate(
            identity,
            uncertain=True,
            before_short_uuid=identity["short_uuid"],
            before_expires_at=TARGET["expires_at"],
            expected_short_uuid=proof["next_short_uuid"],
        )


def test_panel28_node_snapshot_is_explicitly_allowlisted():
    panel, _, state = fixture_panel(allowed_nodes={NODE})
    assert panel.node_snapshot(NODE)["status"] == "CONNECTED"
    before = len(state["calls"])
    with pytest.raises(ValueError):
        panel.node_snapshot(str(uuid4()))
    assert len(state["calls"]) == before


@pytest.mark.parametrize("still_present", [False, True])
def test_panel28_unknown_delete_outcome_is_read_only(still_present):
    panel, identity, state = fixture_panel()
    panel.register_device(identity, "SYNTHETIC-01")
    state["devices"] = [device()] if still_present else []
    starts = []
    if still_present:
        with pytest.raises(PanelReview):
            panel.revoke(identity, "SYNTHETIC-01", uncertain=True, before_delete=lambda: starts.append(True))
    else:
        assert panel.revoke(
            identity, "SYNTHETIC-01", uncertain=True, before_delete=lambda: starts.append(True)
        )
    assert starts == []
    assert all(method != "POST" for method, _ in state["calls"])


def test_panel28_password_only_preserves_already_trusted_nonhex_short_without_registration_expansion():
    panel, identity, state = fixture_panel()
    identity = identity | {"short_uuid": "Trusted_short-01"}
    link = "https://own-new-connect.example.test/subscription/" + identity["short_uuid"]
    with pytest.raises(ValueError):
        panel.register_test_binding(identity, telegram_id=OWNER, connection_url=link)
    # Only emulate an already trusted internal binding for future compatibility;
    # public registration of current generated identities stays hex-only.
    panel.auto_bindings[identity["uuid"]] = dict(identity)
    panel.auto_connection_urls[identity["uuid"]] = link
    state["user"].update(shortUuid=identity["short_uuid"], subscriptionUrl=link)
    proof = {}
    result = panel.rotate(identity, revoke_only_passwords=True, before_rotate=proof.update)
    assert result["short_uuid"] == identity["short_uuid"]
    assert result["connection_url"] == link
    assert proof["next_short_uuid"] == identity["short_uuid"]
