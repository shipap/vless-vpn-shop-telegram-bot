import json
from types import SimpleNamespace

import httpx
import pytest
from sunrise_core.errors import DomainError
from sunrise_infrastructure.panel import PanelReview
from sunrise_infrastructure.panel_v3 import RemnawaveProduction


class Policy:
    production = True

    def __init__(self):
        self.allowed = True
        self.scope = SimpleNamespace(
            scope_id="synthetic-contacts",
            panel=SimpleNamespace(
                origin="https://panel.synthetic.test",
                internal_squads=[],
                external_squads=[],
                subscription_hosts=["sub.synthetic.test"],
                node_uuids=[],
            ),
        )

    def require(self, capability=None):
        if not self.allowed:
            raise DomainError("production_stage_denied", "Недоступно", 503)


def fixture(handle):
    binding = dict(
        scope_id="synthetic-contacts",
        generation=1,
        origin_nonce="a" * 32,
        user_id="7",
        uuid=None,
        username="legacy.stable",
        short_uuid="syntheticshortuuid123",
    )
    policy = Policy()
    panel = RemnawaveProduction(
        runtime_policy=policy,
        token="synthetic-token",  # noqa: S106 -- mock only
        transport=httpx.MockTransport(handle),
    )
    panel.register_project_binding(
        binding, connection_url="https://sub.synthetic.test/" + binding["short_uuid"]
    )
    return panel, binding, policy


def user(binding, capacity):
    return dict(
        id=7,
        username=binding["username"],
        shortUuid=binding["short_uuid"],
        subscriptionUrl="https://sub.synthetic.test/" + binding["short_uuid"],
        expireAt="2030-01-01T00:00:00Z",
        status="ACTIVE",
        activeInternalSquads=[],
        externalSquadUuid=None,
        hwidDeviceLimit=capacity,
        email="old@synthetic.test",
        telegramId=12345,
    )


@pytest.mark.parametrize("capacity", [4, None])
def test_contacts_patch_has_only_id_and_verified_fields_preserving_identity_and_rights(capacity):
    calls = []
    current = {}

    def handle(request):
        calls.append((request.method, request.url.path))
        if request.method == "PATCH":
            body = json.loads(request.content)
            assert body == {"id": 7, "email": "new@synthetic.test", "telegramId": None}
            current.update(body)
        return httpx.Response(200, json={"response": current})

    panel, binding, _ = fixture(handle)
    current.update(user(binding, capacity))
    before = dict(current)
    assert hasattr(panel, "sync_contacts"), "Owned contact transport is missing"
    validations = []
    panel.sync_contacts(
        binding,
        {"email": "new@synthetic.test", "telegramId": None},
        before_patch=lambda: validations.append("fresh"),
    )
    assert calls == [("GET", "/api/users/7"), ("PATCH", "/api/users"), ("GET", "/api/users/7")]
    assert validations == ["fresh"]
    assert {k: v for k, v in current.items() if k not in {"email", "telegramId"}} == {
        k: v for k, v in before.items() if k not in {"email", "telegramId"}
    }


@pytest.mark.parametrize("revoke", ["gate", "authority", "foreign"])
def test_fresh_post_get_authority_blocks_contact_patch(revoke):
    calls = []
    denied = False

    def handle(request):
        nonlocal denied
        calls.append(request.method)
        denied = True
        row = user(binding, 4)
        if revoke == "gate":
            policy.allowed = False
        if revoke == "foreign":
            row["shortUuid"] = "foreignshortuuid123"
        return httpx.Response(200, json={"response": row})

    panel, binding, policy = fixture(handle)

    def authorize():
        if denied and revoke == "authority":
            raise PanelReview("panel_contact_changed")

    assert hasattr(panel, "sync_contacts"), "Owned contact transport is missing"
    with pytest.raises((PanelReview, DomainError)):
        panel.sync_contacts(binding, {"telegramId": None}, before_patch=authorize)
    assert calls == ["GET"]


def test_unknown_create_recovers_short_uuid_without_another_post():
    calls = []

    def handle(request):
        calls.append((request.method, request.url.path))
        return httpx.Response(404)

    panel, binding, _ = fixture(handle)
    binding = binding | {"user_id": None}
    panel.register_project_binding(binding)
    assert hasattr(panel, "recover_creation"), "ShortUuid recovery is missing"
    with pytest.raises(PanelReview):
        panel.recover_creation(binding)
    assert calls == [("GET", "/api/users/by-short-uuid/" + binding["short_uuid"])]
