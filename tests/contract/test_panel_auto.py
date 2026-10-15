"""Automatic mode is separate from static guards and never follows connection URLs."""

import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sunrise_infrastructure import panel as panel_module
from sunrise_infrastructure.panel import PanelReview, PanelUnavailable, RemnawaveTest

SQUAD = "20000000-0000-4000-8000-000000000002"
SYNTHETIC = "synthetic-credential-placeholder"
TARGET = {
    "expires_at": "2027-01-01T00:00:00+00:00",
    "devices_limit": 4,
    "internal_squad_uuid": SQUAD,
}


def auto_panel(handler, **overrides):
    return RemnawaveTest(
        **(
            dict(
                base_url="https://panel.example.test",
                token=SYNTHETIC,
                contract="2.8_uuid",
                authorized=True,
                allowed_hosts={"panel.example.test"},
                allowed_squads={SQUAD},
                auto_test_telegram_ids={"70000000000000001"},
                transport=httpx.MockTransport(handler),
            )
            | overrides
        )
    )


def binding():
    value = uuid4()
    return {"uuid": str(value), "short_uuid": uuid4().hex[:24], "username": "sunrise_test_" + value.hex[:20]}


def panel_user(identity, **overrides):
    return (
        dict(
            uuid=identity["uuid"],
            shortUuid=identity["short_uuid"],
            username=identity["username"],
            expireAt=TARGET["expires_at"],
            hwidDeviceLimit=4,
            activeInternalSquads=[{"uuid": SQUAD}],
            status="ACTIVE",
            subscriptionUrl="https://own-new-connect.example.test/subscription/" + identity["short_uuid"],
        )
        | overrides
    )


def test_auto_mode_requires_registered_own_binding_before_any_request():
    panel = auto_panel(lambda _: pytest.fail("Unregistered identity must not reach HTTP"))
    value = binding()
    with pytest.raises(ValueError):
        panel.apply(value, TARGET, create=True)
    with pytest.raises(ValueError):
        panel.register_test_binding(value, telegram_id="70000000000000002")


@pytest.mark.parametrize(
    "create, uncertain, known_not_started",
    [
        (True, False, True),
        (True, True, False),
        (False, False, False),
    ],
)
def test_initial_get_failure_proves_no_create_only_for_fresh_attempt(create, uncertain, known_not_started):
    identity = binding()
    calls = []

    def handler(request):
        calls.append(request.method)
        raise httpx.ConnectError("synthetic loopback connection failure")

    panel = auto_panel(handler)
    panel.register_test_binding(identity, telegram_id="70000000000000001")
    with pytest.raises(PanelUnavailable) as error:
        panel.apply(identity, TARGET, create=create, uncertain=uncertain)
    assert error.value.create_not_started is known_not_started
    assert calls == ["GET"]


def test_panel_unavailability_is_unknown_by_default():
    assert PanelUnavailable("panel_unavailable").create_not_started is False
    assert PanelUnavailable().create_not_started is False


def test_post_connection_failure_does_not_claim_creation_never_started():
    identity = binding()
    methods = []

    def handler(request):
        methods.append(request.method)
        if request.method == "GET":
            return httpx.Response(404)
        raise httpx.ConnectError("synthetic uncertain write connection failure")

    panel = auto_panel(handler)
    panel.register_test_binding(identity, telegram_id="70000000000000001")
    with pytest.raises(PanelUnavailable) as error:
        panel.apply(identity, TARGET, create=True)
    assert error.value.create_not_started is False
    assert methods == ["GET", "POST"]


@pytest.mark.parametrize(
    "source, expected",
    [
        ("2027-01-01T00:00:00.123793+00:00", "2027-01-01T00:00:00.124+00:00"),
        ("2027-01-01T00:00:00.000001+00:00", "2027-01-01T00:00:00.001+00:00"),
        ("2027-01-01T23:59:59.999999+00:00", "2027-01-02T00:00:00.000+00:00"),
        ("2027-12-31T23:59:59.999999+03:00", "2028-01-01T00:00:00.000+03:00"),
        ("2027-02-28T23:59:59.999001-04:30", "2027-03-01T00:00:00.000-04:30"),
        ("2027-01-01T00:00:00.123000+00:00", "2027-01-01T00:00:00.123000+00:00"),
        ("2027-01-01T00:00:00.123Z", "2027-01-01T00:00:00.123Z"),
        ("2027-01-01T00:00:00+00:00", "2027-01-01T00:00:00+00:00"),
    ],
)
def test_panel_expiry_ceil_preserves_paid_absolute_time_and_already_ms_strings(source, expected):
    assert panel_module.panel_expiry(source) == expected


@pytest.mark.parametrize("source", ["2027-01-01T00:00:00.000001", "9999-12-31T23:59:59.999999+00:00"])
def test_panel_expiry_rejects_naive_or_unrepresentable_targets(source):
    with pytest.raises(ValueError):
        panel_module.panel_expiry(source)


def test_panel_verifies_same_ceiled_absolute_target_on_repeated_updates_without_changing_source():
    identity, writes = binding(), []
    source = "2027-01-01T00:00:00.123793+00:00"
    target = TARGET | {"expires_at": source}

    def handler(request):
        if request.method == "PATCH":
            writes.append(json.loads(request.content))
        return httpx.Response(
            200, json={"response": panel_user(identity, expireAt="2027-01-01T00:00:00.124Z")}
        )

    panel = auto_panel(handler)
    panel.register_test_binding(identity, telegram_id="70000000000000001")
    for _ in range(2):
        result = panel.apply(identity, target, create=False)
        assert result["uuid"] == identity["uuid"]
    assert [value["expireAt"] for value in writes] == [
        "2027-01-01T00:00:00.124+00:00",
        "2027-01-01T00:00:00.124+00:00",
    ]
    assert target["expires_at"] == source


def test_new_creation_derives_https_link_only_after_identity_and_target_match():
    identity = binding()
    calls, state = [], {}

    def handler(request):
        calls.append(request)
        assert request.url.host == "panel.example.test"
        if request.method == "POST":
            body = json.loads(request.content)
            assert body["uuid"] == identity["uuid"]
            assert body["expireAt"] == "2027-01-01T00:00:00+00:00"
            state.update(panel_user(identity))
        return httpx.Response(200, json={"response": state}) if state else httpx.Response(404)

    panel = auto_panel(handler)
    panel.register_test_binding(identity, telegram_id="70000000000000001")
    result = panel.apply(identity, TARGET, create=True)
    assert (
        result["connection_url"]
        == "https://own-new-connect.example.test/subscription/" + identity["short_uuid"]
    )
    assert [r.method for r in calls] == ["GET", "POST", "GET"]
    assert panel.subscription_hosts == set()


@pytest.mark.parametrize(
    "link",
    [
        "http://own.example.test/s",
        "https://name:password@own.example.test/s",
        "https://own.example.test:444/s",
        "https://own.example.test/s?token=one",
        "https://own.example.test/s#fragment",
        "https:///missing-host",
        "https://own.example.test/s\n",
    ],
)
def test_auto_link_derivation_rejects_unsafe_urls(link):
    identity = binding()
    panel = auto_panel(
        lambda _: httpx.Response(200, json={"response": panel_user(identity, subscriptionUrl=link)})
    )
    panel.register_test_binding(identity, telegram_id="70000000000000001")
    with pytest.raises(PanelReview):
        panel.apply(identity, TARGET, create=False)


def test_first_name_collision_requires_review_even_when_identity_matches():
    identity, calls = binding(), []

    def handler(request):
        calls.append(request.method)
        return httpx.Response(200, json={"response": panel_user(identity)})

    panel = auto_panel(handler)
    panel.register_test_binding(identity, telegram_id="70000000000000001")
    with pytest.raises(PanelReview):
        panel.apply(identity, TARGET, create=True, uncertain=False)
    assert calls == ["GET"]


def test_restart_link_pin_rejects_host_change_on_matching_owned_identity():
    identity = binding()
    panel = auto_panel(lambda _: httpx.Response(200, json={"response": panel_user(identity)}))
    panel.register_test_binding(
        identity, telegram_id="70000000000000001", connection_url="https://pinned.example.test/s"
    )
    with pytest.raises(PanelReview):
        panel.apply(identity, TARGET, create=False)


def test_creation_response_cannot_claim_another_identity():
    identity, calls = binding(), []

    def handler(request):
        calls.append(request.method)
        return (
            httpx.Response(404)
            if request.method == "GET"
            else httpx.Response(201, json={"response": panel_user(identity, uuid=str(uuid4()))})
        )

    panel = auto_panel(handler)
    panel.register_test_binding(identity, telegram_id="70000000000000001")
    with pytest.raises(PanelReview):
        panel.apply(identity, TARGET, create=True)
    assert calls == ["GET", "POST"]


def test_durable_binding_revalidation_denies_concurrent_panel_identity_change():
    from sunrise_infrastructure.db import Account
    from sunrise_infrastructure.provisioning import Provisioning

    identity = binding()
    panel = auto_panel(lambda _: pytest.fail("Revalidation cannot make HTTP requests"))
    service = object.__new__(Provisioning)
    service.panel = panel
    service._historical_auto_jobs = set()
    account = SimpleNamespace(id=42, telegram_id=70000000000000001, legacy_consent={})
    subscription = SimpleNamespace(
        id="synthetic-subscription",
        account_id=42,
        environment="local_test",
        panel_uuid=str(uuid4()),
        short_uuid=None,
        connection_url=None,
    )
    record = SimpleNamespace(
        value={
            "origin": "new_test_identity",
            "subscription_id": subscription.id,
            "account_id": "42",
            "telegram_id": "70000000000000001",
            "environment": "local_test",
            "binding": identity,
            "connection_url": None,
            "panel_host": "panel.example.test",
        }
    )
    results = iter([None, record])
    session = SimpleNamespace(
        get=lambda model, _: account if model is Account else None,
        scalar=lambda _: next(results),
    )
    with pytest.raises(ValueError):
        service._auto_binding(
            session, SimpleNamespace(account_id=42), subscription, {"environment": "local_test"}
        )
