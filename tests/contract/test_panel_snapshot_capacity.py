"""Pinned provider snapshots retain capacity facts without pricing guesses."""

import httpx
import pytest
from sunrise_infrastructure.panel import PanelReview
from test_panel_28_compatibility import fixture_panel
from test_panel_v3 import BINDING, TG, provider, user


@pytest.mark.parametrize("contract", ["2.8_uuid", "3.4_id"])
@pytest.mark.parametrize("squads", [[], [{"uuid": "20000000-0000-4000-8000-000000000001"}]])
def test_snapshot_exposes_validated_squad_uuids(contract, squads):
    if contract == "2.8_uuid":
        panel, binding, state = fixture_panel()
        state["user"]["activeInternalSquads"] = squads
    else:
        binding = BINDING | {"user_id": "123"}
        panel, binding = provider(
            lambda request: httpx.Response(
                200,
                json={
                    "response": {"devices": [], "total": 0}
                    if "/hwid/" in request.url.path
                    else user(activeInternalSquads=squads)
                },
            ),
            binding,
        )
    try:
        assert panel.snapshot(binding)["internal_squad_uuids"] == sorted(item["uuid"] for item in squads)
    finally:
        panel.client.close()


@pytest.mark.parametrize("contract", ["2.8_uuid", "3.4_id"])
@pytest.mark.parametrize("squads", [None, [123], [{"uuid": 123}], [{"uuid": "not-a-uuid"}]])
def test_snapshot_rejects_invalid_squad_types(contract, squads):
    if contract == "2.8_uuid":
        panel, binding, state = fixture_panel()
        state["user"]["activeInternalSquads"] = squads
    else:
        binding = BINDING | {"user_id": "123"}
        panel, binding = provider(
            lambda request: httpx.Response(
                200,
                json={
                    "response": {"devices": [], "total": 0}
                    if "/hwid/" in request.url.path
                    else user(activeInternalSquads=squads)
                },
            ),
            binding,
        )
    try:
        with pytest.raises(PanelReview):
            panel.snapshot(binding)
    finally:
        panel.client.close()


@pytest.mark.parametrize("contract", ["2.8_uuid", "3.4_id"])
@pytest.mark.parametrize("limit", [12, 0, None])
def test_snapshot_exposes_exact_capacity_including_unlimited_and_unresolved_fallback(contract, limit):
    if contract == "2.8_uuid":
        panel, binding, state = fixture_panel()
        state["user"]["hwidDeviceLimit"] = limit
    else:
        binding = BINDING | {"user_id": "123"}

        def handler(request):
            result = (
                {"devices": [], "total": 0} if "/hwid/" in request.url.path else user(hwidDeviceLimit=limit)
            )
            return httpx.Response(200, json={"response": result})

        panel, binding = provider(handler, binding)
        panel.register_test_binding(binding, telegram_id=TG, connection_url=user()["subscriptionUrl"])
    try:
        assert panel.snapshot(binding)["devices_limit"] == limit
    finally:
        panel.client.close()


@pytest.mark.parametrize("contract", ["2.8_uuid", "3.4_id"])
@pytest.mark.parametrize("limit", [True, -1, "12", 1.5])
def test_snapshot_rejects_unverified_capacity_before_it_can_determine_a_price(contract, limit):
    if contract == "2.8_uuid":
        panel, binding, state = fixture_panel()
        state["user"]["hwidDeviceLimit"] = limit
    else:
        binding = BINDING | {"user_id": "123"}
        panel, binding = provider(
            lambda request: httpx.Response(
                200,
                json={
                    "response": {"devices": [], "total": 0}
                    if "/hwid/" in request.url.path
                    else user(hwidDeviceLimit=limit)
                },
            ),
            binding,
        )
    try:
        with pytest.raises(PanelReview):
            panel.snapshot(binding)
    finally:
        panel.client.close()
