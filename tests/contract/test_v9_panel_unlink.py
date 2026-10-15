import json

import httpx
import pytest
from sunrise_infrastructure.panel import PanelUnavailable
from test_test_integrations import BINDING, panel, user


def test_already_absent_device_is_not_deleted_or_counted_as_new_unlink():
    calls = []

    def handler(request):
        calls.append(request.method)
        return httpx.Response(
            200, json={"response": {"devices": []} if "hwid" in request.url.path else user()}
        )

    assert panel(handler).revoke(BINDING, "synthetic_hwid") is False
    assert "POST" not in calls


def test_uncertain_success_reconciles_without_repeating_delete():
    state = {"present": True, "fail_read": False, "deletes": 0}

    def handler(request):
        if request.method == "POST":
            assert json.loads(request.content)["hwid"] == "synthetic_hwid"
            state.update(present=False, fail_read=True, deletes=state["deletes"] + 1)
            return httpx.Response(200, json={"response": {}})
        if "hwid" in request.url.path:
            if state["fail_read"]:
                state["fail_read"] = False
                raise httpx.ReadTimeout("synthetic transport")
            return httpx.Response(
                200, json={"response": {"devices": [{"hwid": "synthetic_hwid"}] if state["present"] else []}}
            )
        return httpx.Response(200, json={"response": user()})

    provider = panel(handler)
    with pytest.raises(PanelUnavailable):
        provider.revoke(BINDING, "synthetic_hwid")
    assert provider.revoke(BINDING, "synthetic_hwid", uncertain=True) is True
    assert state["deletes"] == 1
