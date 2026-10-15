"""Scoped renewal must revalidate after observing the provider state."""

import json
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from sunrise_core.errors import DomainError
from sunrise_infrastructure.db import now
from sunrise_infrastructure.panel import PanelReview
from sunrise_infrastructure.panel_v3 import RemnawaveV3


def test_scoped_panel_revoked_on_get_cannot_patch_existing_identity():
    squad = "12345678-1234-1234-1234-123456789abc"
    binding = {
        "user_id": "123",
        "origin_nonce": "a" * 32,
        "username": "sunrise_test_" + "a" * 20,
        "short_uuid": "a" * 24,
    }
    target = {
        "expires_at": (now() + timedelta(days=30)).isoformat(),
        "devices_limit": 4,
        "internal_squad_uuid": squad,
    }
    observed = {
        "id": 123,
        "username": binding["username"],
        "shortUuid": binding["short_uuid"],
        "expireAt": target["expires_at"],
        "hwidDeviceLimit": 4,
        "activeInternalSquads": [{"uuid": squad}],
        "status": "ACTIVE",
        "subscriptionUrl": "https://vpn.synthetic.invalid/sub/" + binding["short_uuid"],
    }
    permitted, methods = [True], []

    def handle(request):
        methods.append(request.method)
        if request.method == "GET":
            permitted[0] = False
        return httpx.Response(200, json={"response": observed})

    def authorize():
        if not permitted[0]:
            raise DomainError("scoped_test_admission_denied", "Допуск отозван.", 503)

    panel = RemnawaveV3(
        base_url="https://panel.synthetic.invalid",
        token="synthetic-key",  # noqa: S106 -- in-memory test adapter only
        authorized=True,
        allowed_hosts={"panel.synthetic.invalid"},
        allowed_squads={squad},
        allowed_subscription_hosts={"vpn.synthetic.invalid"},
        auto_test_telegram_ids={"12345"},
        transport=httpx.MockTransport(handle),
    )
    panel.register_test_binding(binding, telegram_id="12345")
    with pytest.raises(DomainError, match="отозван"):
        panel.apply(binding, target, before_mutation=authorize)
    assert methods == ["GET"]
    panel.client.close()


def test_scoped_contact_goal_changed_while_attempt_commits_cannot_patch():
    binding = {
        "user_id": "123",
        "origin_nonce": "a" * 32,
        "username": "sunrise_test_" + "a" * 20,
        "short_uuid": "a" * 24,
    }
    observed = {
        "id": 123,
        "username": binding["username"],
        "shortUuid": binding["short_uuid"],
        "subscriptionUrl": "https://vpn.synthetic.invalid/sub/" + binding["short_uuid"],
        "telegramId": 12345,
        "email": "old@external.invalid",
    }
    goal = {"telegramId": 12345, "email": "new@external.invalid"}
    generation, methods = [1], []

    def handle(request):
        methods.append(request.method)
        if request.method == "PATCH":
            observed.update(json.loads(request.content))
        return httpx.Response(200, json={"response": observed})

    def authorize():
        if generation[0] != 1:
            raise PanelReview("panel_contact_goal_changed")

    def before_start(current):
        generation[0] = 2

    panel = RemnawaveV3(
        base_url="https://panel.synthetic.invalid",
        token="synthetic-key",  # noqa: S106 -- synthetic in-memory provider
        authorized=True,
        allowed_hosts={"panel.synthetic.invalid"},
        allowed_squads={"12345678-1234-1234-1234-123456789abc"},
        allowed_subscription_hosts={"vpn.synthetic.invalid"},
        auto_test_telegram_ids={"12345"},
        transport=httpx.MockTransport(handle),
    )  # noqa: S106 -- synthetic in-memory provider
    panel.register_test_binding(binding, telegram_id="12345")
    panel.scoped_runtime_policy = SimpleNamespace(require=lambda *_: None)
    with pytest.raises(PanelReview, match="panel_contact_goal_changed"):
        panel.sync_contacts(binding, goal, before_patch=authorize, before_start=before_start)
    assert methods == ["GET"]
    panel.client.close()
