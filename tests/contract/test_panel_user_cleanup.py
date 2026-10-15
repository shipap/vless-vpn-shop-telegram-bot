"""Actual versioned adapter transport contracts; no external panel requests."""

import httpx
import pytest
from sunrise_infrastructure.panel import PanelReview, PanelUnavailable, RemnawaveTest
from sunrise_infrastructure.panel_v3 import RemnawaveV3

TG = "70000000000000001"
UUID = "30000000-0000-4000-8000-000000000001"
NONCE = "30000000000040008000000000000001"
SQUAD = "20000000-0000-4000-8000-000000000002"


def adapter(version, handler):
    values = dict(
        base_url="https://panel.example.test",
        token="synthetic-token",  # noqa: S106
        authorized=True,
        allowed_hosts={"panel.example.test"},
        allowed_squads={SQUAD},
        auto_test_telegram_ids={TG},
        transport=httpx.MockTransport(handler),
    )
    if version == "2.8_uuid":
        panel = RemnawaveTest(contract=version, **values)
        binding = {"uuid": UUID, "username": "sunrise_test_" + NONCE[:20], "short_uuid": "a" * 24}
    else:
        panel = RemnawaveV3(**values)
        binding = {
            "user_id": "123",
            "origin_nonce": NONCE,
            "username": "sunrise_test_" + NONCE[:20],
            "short_uuid": "a" * 24,
        }
    panel.register_test_binding(binding, telegram_id=TG)
    return panel, binding


@pytest.mark.parametrize(
    "version,expected", [("2.8_uuid", "/api/users/" + UUID), ("3.4_id", "/api/users/123")]
)
def test_delete_one_registered_user_uses_exact_versioned_endpoint(version, expected):
    observed = []

    def handler(request):
        assert observed == ["durable-attempt"]
        assert request.method == "DELETE" and request.url.path == expected
        observed.append("delete")
        return (
            httpx.Response(204)
            if version == "3.4_id"
            else httpx.Response(200, json={"response": {"isDeleted": True}})
        )

    panel, binding = adapter(version, handler)
    try:
        assert panel.delete_user(binding, before_delete=lambda: observed.append("durable-attempt")) is None
        assert observed == ["durable-attempt", "delete"]
    finally:
        panel.client.close()


@pytest.mark.parametrize("version", ["2.8_uuid", "3.4_id"])
def test_failed_predelete_guard_performs_no_http(version):
    def unexpected(_request):
        pytest.fail("DELETE must not run after failed durable guard")

    def deny():
        raise PanelReview("synthetic_stale_version")

    panel, binding = adapter(version, unexpected)
    try:
        with pytest.raises(PanelReview, match="synthetic_stale_version"):
            panel.delete_user(binding, before_delete=deny)
    finally:
        panel.client.close()


@pytest.mark.parametrize("version", ["2.8_uuid", "3.4_id"])
@pytest.mark.parametrize("status", [400, 401, 403, 404, 500])
def test_delete_error_is_never_confirmed_absence(version, status):
    panel, binding = adapter(version, lambda _request: httpx.Response(status))
    try:
        with pytest.raises(PanelUnavailable if status == 500 else PanelReview):
            panel.delete_user(binding, before_delete=lambda: None)
    finally:
        panel.client.close()


@pytest.mark.parametrize("version", ["2.8_uuid", "3.4_id"])
def test_unregistered_binding_cannot_delete(version):
    panel, binding = adapter(version, lambda _request: pytest.fail("foreign binding reached HTTP"))
    binding = binding | {"short_uuid": "b" * 24}
    try:
        with pytest.raises(PanelReview):
            panel.delete_user(binding, before_delete=lambda: None)
    finally:
        panel.client.close()


@pytest.mark.parametrize("version", ["2.8_uuid", "3.4_id"])
def test_timeout_is_an_unknown_outcome(version):
    def timeout(_request):
        raise httpx.ReadTimeout("synthetic accepted-or-not unknown")

    panel, binding = adapter(version, timeout)
    try:
        with pytest.raises(PanelUnavailable):
            panel.delete_user(binding, before_delete=lambda: None)
    finally:
        panel.client.close()


@pytest.mark.parametrize("body", [{"isDeleted": False}, {"isDeleted": 1}, {}, {"isDeleted": "true"}])
def test_legacy_unverified_success_requires_review(body):
    panel, binding = adapter("2.8_uuid", lambda _request: httpx.Response(200, json={"response": body}))
    try:
        with pytest.raises(PanelReview):
            panel.delete_user(binding, before_delete=lambda: None)
    finally:
        panel.client.close()


def test_v3_nonempty_204_is_not_a_verified_contract():
    panel, binding = adapter("3.4_id", lambda _request: httpx.Response(204, content=b"unexpected"))
    try:
        with pytest.raises(PanelReview):
            panel.delete_user(binding, before_delete=lambda: None)
    finally:
        panel.client.close()


@pytest.mark.parametrize("version", ["2.8_uuid", "3.4_id"])
@pytest.mark.parametrize("change", ["binding", "registration", "host", "scheme", "port", "path"])
def test_callback_cannot_change_authorized_delete_scope(version, change):
    panel, binding = adapter(version, lambda _request: pytest.fail("changed scope reached DELETE"))

    def change_scope():
        if change == "binding":
            binding["uuid" if version == "2.8_uuid" else "user_id"] = (
                "30000000-0000-4000-8000-000000000009" if version == "2.8_uuid" else "456"
            )
        elif change == "registration":
            (panel.auto_bindings if version == "2.8_uuid" else panel.registered).clear()
        elif change == "host":
            panel.api_host = "foreign.example.test"
        else:
            panel.client.base_url = {
                "scheme": "http://panel.example.test/api/",
                "port": "https://panel.example.test:8443/api/",
                "path": "https://panel.example.test/foreign/",
            }[change]

    try:
        with pytest.raises(PanelReview):
            panel.delete_user(binding, before_delete=change_scope)
    finally:
        panel.client.close()


@pytest.mark.parametrize("version", ["2.8_uuid", "3.4_id"])
@pytest.mark.parametrize(
    "url",
    [
        "http://panel.example.test/api/",
        "https://panel.example.test:8443/api/",
        "https://panel.example.test/foreign/",
    ],
)
def test_preexisting_origin_drift_cannot_authorize_delete(version, url):
    panel, binding = adapter(version, lambda _request: pytest.fail("changed origin reached HTTP"))
    panel.client.base_url = url
    try:
        with pytest.raises(PanelReview):
            panel.delete_user(binding, before_delete=lambda: None)
    finally:
        panel.client.close()


@pytest.mark.parametrize("version", ["2.8_uuid", "3.4_id"])
def test_scoped_cleanup_read_distinguishes_absent_from_foreign(version):
    state = {"missing": True}

    def handler(_request):
        if state["missing"]:
            return httpx.Response(404)
        return httpx.Response(
            200,
            json={
                "response": {
                    "uuid": UUID,
                    "id": 456,
                    "username": "foreign_user",
                    "shortUuid": "a" * 24,
                    "expireAt": "2026-01-01T00:00:00+00:00",
                    "status": "EXPIRED",
                    "activeInternalSquads": [{"uuid": SQUAD}],
                }
            },
        )

    panel, binding = adapter(version, handler)
    try:
        assert panel.cleanup_snapshot(binding) is None
        state["missing"] = False
        with pytest.raises(PanelReview):
            panel.cleanup_snapshot(binding)
    finally:
        panel.client.close()
