"""Remnawave relay preserves primary delivery without leaking unknown payloads to tests."""

# ruff: noqa: F811 -- pytest injects the imported synthetic fixture
import hashlib
import hmac
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.contract.test_scoped_payment_policy import synthetic_scope  # noqa: F401

SECRET = "synthetic-remna-signing-key-" + "x" * 32
STAMP = "2026-10-13T12:00:00+00:00"
NOW = 1791892800
PATH = "/api/v1/webhooks/remnawave"


def event(kind="user.modified", identifier=432):
    data = {"id": identifier, "email": "unknown-private@example.invalid", "username": "private_customer"}
    if kind.startswith("node."):
        data = {"uuid": identifier, "name": "private node"}
    return json.dumps({"event": kind, "scope": kind.split(".")[0], "timestamp": STAMP, "data": data}).encode()


def headers(raw):
    return {
        "x-remnawave-timestamp": STAMP,
        "x-remnawave-signature": hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest(),
    }


def context(
    synthetic_scope,
    *,
    admitted=False,
    probe_error=False,
    forged_response=False,
    response_change=None,
    test_failure=False,
):
    from sunrise_api.webhook_router import (
        RemnawaveRouterSettings,
        create_remnawave_router,
        sign_remna_probe_response,
    )

    captured = []

    def transport(request):
        captured.append(request)
        if request.url.path.endswith("remnawave-membership"):
            if probe_error:
                if probe_error == "timeout":
                    raise httpx.ReadTimeout("synthetic timeout")
                return httpx.Response(503)
            payload = json.loads(request.content) | {"admitted": admitted}
            if response_change:
                payload |= response_change
            raw = json.dumps(payload, separators=(",", ":")).encode()
            signature = sign_remna_probe_response(
                SECRET,
                raw,
                request.headers["x-sunrise-remna-timestamp"],
                request.headers["x-sunrise-remna-nonce"],
            )
            return httpx.Response(
                200,
                content=raw,
                headers={"x-sunrise-remna-response-signature": "0" * 64 if forged_response else signature},
            )
        return httpx.Response(
            503 if test_failure and request.url.host == "isolated" else 200, json={"status": "accepted"}
        )

    settings = RemnawaveRouterSettings(
        _env_file=None,
        signing_secret=SECRET,
        scoped_test_admission_file=synthetic_scope[0]["scoped_test_admission_file"],
        scoped_test_admission_sha256=synthetic_scope[0]["scoped_test_admission_sha256"],
        primary_url="http://primary:8000" + PATH,
        primary_host="main.example.test",
        test_url="http://isolated:8000" + PATH,
        test_host="qa.example.test",
        membership_url="http://isolated:8000/api/v1/internal/remnawave-membership",
        membership_host="127.0.0.1",
        allowed_backend_hosts=["primary", "isolated"],
        ingress_hosts=["testserver"],
    )
    client = TestClient(
        create_remnawave_router(settings, transport=httpx.MockTransport(transport), clock=lambda: NOW)
    )
    return client, captured


@pytest.mark.parametrize("admitted,target", [(False, "primary"), (True, "isolated")])
def test_user_membership_routes_only_own_payload_and_probe_contains_no_private_identity(
    synthetic_scope, admitted, target
):
    client, captured = context(synthetic_scope, admitted=admitted)
    raw = event()
    with client:
        response = client.post(PATH, content=raw, headers=headers(raw))
    assert response.status_code == 200
    probe, forwarded = captured
    assert probe.url.path.endswith("remnawave-membership")
    assert b"432" not in probe.content and b"private" not in probe.content and b"@" not in probe.content
    assert set(json.loads(probe.content)) == {
        "scope_id",
        "admission_sha256",
        "resource_kind",
        "resource_digest",
    }
    assert forwarded.url.host == target and forwarded.content == raw
    assert forwarded.headers["x-remnawave-signature"] == headers(raw)["x-remnawave-signature"]


@pytest.mark.parametrize("admitted,targets", [(False, ["primary"]), (True, ["primary", "isolated"])])
def test_node_membership_preserves_primary_and_adds_only_admitted_test(synthetic_scope, admitted, targets):
    client, captured = context(synthetic_scope, admitted=admitted)
    raw = event("node.disabled", "32345678-1234-1234-1234-123456789abc")
    with client:
        response = client.post(PATH, content=raw, headers=headers(raw))
    assert response.status_code == 200
    forwards = [request for request in captured if request.url.path == PATH]
    assert [request.url.host for request in forwards] == targets
    assert all(request.content == raw for request in forwards)


@pytest.mark.parametrize("probe_error,forged_response", [(True, False), ("timeout", False), (False, True)])
def test_unknown_probe_outcome_preserves_primary_and_requires_retry_without_test_payload(
    synthetic_scope, probe_error, forged_response
):
    client, captured = context(synthetic_scope, probe_error=probe_error, forged_response=forged_response)
    raw = event()
    with client:
        response = client.post(PATH, content=raw, headers=headers(raw))
    assert response.status_code == 503
    forwards = [request for request in captured if request.url.path == PATH]
    assert len(forwards) == 1 and forwards[0].url.host == "primary" and forwards[0].content == raw


@pytest.mark.parametrize(
    "change", [{"scope_id": "wrong"}, {"resource_digest": "0" * 64}, {"admitted": "true"}]
)
def test_signed_response_with_wrong_attestation_never_copies_payload_to_test(synthetic_scope, change):
    client, captured = context(synthetic_scope, admitted=True, response_change=change)
    raw = event()
    with client:
        response = client.post(PATH, content=raw, headers=headers(raw))
    assert response.status_code == 503
    forwards = [request for request in captured if request.url.path == PATH]
    assert len(forwards) == 1 and forwards[0].url.host == "primary"


@pytest.mark.parametrize(
    "kind,identifier,targets",
    [
        ("user.modified", 432, ["isolated"]),
        ("node.disabled", "32345678-1234-1234-1234-123456789abc", ["primary", "isolated"]),
    ],
)
def test_scoped_backend_failure_requires_retry_with_exact_primary_policy(
    synthetic_scope, kind, identifier, targets
):
    client, captured = context(synthetic_scope, admitted=True, test_failure=True)
    raw = event(kind, identifier)
    with client:
        response = client.post(PATH, content=raw, headers=headers(raw))
    assert response.status_code == 503
    assert [request.url.host for request in captured if request.url.path == PATH] == targets


def test_probe_auth_domains_timestamp_and_nonce_prevent_reuse():
    from sunrise_api.webhook_router import (
        sign_remna_probe_request,
        sign_remna_probe_response,
        verify_remna_probe_request,
    )

    raw, stamp, nonce = b'{"opaque":"proof"}', str(NOW), "a" * 32
    signature = sign_remna_probe_request(SECRET, raw, stamp, nonce)
    verify_remna_probe_request(SECRET, raw, stamp, nonce, signature, NOW)
    for changed_raw, changed_stamp, changed_nonce, signed, now in [
        (raw, stamp, nonce, sign_remna_probe_response(SECRET, raw, stamp, nonce), NOW),
        (raw, stamp, "b" * 32, signature, NOW),
        (raw + b" ", stamp, nonce, signature, NOW),
        (raw, stamp, nonce, signature, NOW + 31),
    ]:
        with pytest.raises(ValueError):
            verify_remna_probe_request(SECRET, changed_raw, changed_stamp, changed_nonce, signed, now)


def test_admission_drift_still_delivers_primary_but_never_tests(synthetic_scope):
    client, captured = context(synthetic_scope, admitted=True)
    raw = event()
    with client:
        synthetic_scope[1].write_text("{}", encoding="utf-8")
        response = client.post(PATH, content=raw, headers=headers(raw))
    assert response.status_code == 503
    assert len(captured) == 1 and captured[0].url.host == "primary"


def test_service_event_passes_original_bytes_only_to_primary_without_probe(synthetic_scope):
    client, captured = context(synthetic_scope)
    raw = event("service.status")
    with client:
        response = client.post(PATH, content=raw, headers=headers(raw))
    assert response.status_code == 200
    assert len(captured) == 1 and captured[0].url.host == "primary" and captured[0].content == raw


@pytest.mark.parametrize("device_user_id", [432, 999])
def test_hwid_nested_user_identity_must_agree_before_test_payload(synthetic_scope, device_user_id):
    client, captured = context(synthetic_scope, admitted=True)
    raw = json.dumps(
        {
            "event": "user_hwid_devices.added",
            "scope": "user_hwid_devices",
            "timestamp": STAMP,
            "data": {
                "user": {"id": 432},
                "hwidUserDevice": {"userId": device_user_id, "hwid": "private-hwid"},
            },
        }
    ).encode()
    with client:
        response = client.post(PATH, content=raw, headers=headers(raw))
    if device_user_id == 432:
        assert response.status_code == 200
        assert captured[-1].url.host == "isolated" and captured[-1].content == raw
    else:
        assert response.status_code == 400 and not captured


@pytest.mark.parametrize(
    "failure", ["signature", "stale", "duplicate", "oversize", "timestamp_format", "identity_format"]
)
def test_invalid_provider_event_never_probes_or_forwards(synthetic_scope, failure):
    client, captured = context(synthetic_scope)
    raw = event()
    signed = headers(raw)
    if failure == "signature":
        signed["x-remnawave-signature"] = "0" * 64
    if failure == "stale":
        raw = raw.replace(STAMP.encode(), b"2025-01-01T00:00:00+00:00")
        signed = headers(raw) | {"x-remnawave-timestamp": "2025-01-01T00:00:00+00:00"}
    if failure == "duplicate":
        raw = b'{"event":"user.modified","event":"node.disabled"}'
        signed = headers(raw)
    if failure == "oversize":
        raw = b"x" * 262145
        signed = headers(raw)
    if failure == "timestamp_format":
        raw = raw.replace(STAMP.encode(), b"malformed-time")
        signed = headers(raw) | {"x-remnawave-timestamp": "malformed-time"}
    if failure == "identity_format":
        raw = event(identifier="private-user-name")
        signed = headers(raw)
    with client:
        response = client.post(PATH, content=raw, headers=signed)
    assert response.status_code in {400, 401, 413} and not captured
