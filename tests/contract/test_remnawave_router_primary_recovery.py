"""Only an explicit primary user acknowledgement can recover a failed membership probe."""

# ruff: noqa: F811 -- pytest injects the imported synthetic fixture
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.contract.test_remnawave_callback_router import NOW, PATH, SECRET, context, event, headers
from tests.contract.test_scoped_payment_policy import synthetic_scope  # noqa: F401


def recovery_context(
    synthetic_scope,
    monkeypatch,
    *,
    enabled=True,
    body=b'{"status":"accepted"}',
    status=200,
    primary_error=None,
    probe_error="unavailable",
):
    from sunrise_api.webhook_router import (
        RemnawaveRouterSettings,
        create_remnawave_router,
        sign_remna_probe_response,
    )

    key = "SUNRISE_REMNA_ROUTER_USER_PRIMARY_ACK_ON_MEMBERSHIP_FAILURE"
    if enabled is None:
        monkeypatch.delenv(key, raising=False)
    else:
        monkeypatch.setenv(key, "true" if enabled else "false")
    captured = []

    def transport(request):
        captured.append(request)
        if request.url.path.endswith("remnawave-membership"):
            if probe_error == "timeout":
                raise httpx.ReadTimeout("synthetic probe timeout")
            if probe_error == "forged":
                payload = json.loads(request.content) | {"admitted": True}
                raw = json.dumps(payload).encode()
                signature = sign_remna_probe_response(
                    SECRET,
                    raw,
                    request.headers["x-sunrise-remna-timestamp"],
                    request.headers["x-sunrise-remna-nonce"],
                )
                return httpx.Response(
                    200,
                    content=raw,
                    headers={
                        "x-sunrise-remna-response-signature": "0" * 64 if signature else "",
                    },
                )
            return httpx.Response(503)
        if primary_error == "timeout":
            raise httpx.ReadTimeout("synthetic primary timeout")
        if primary_error == "connect":
            raise httpx.ConnectError("synthetic primary unavailable")
        return httpx.Response(status, content=body, headers={"content-type": "application/json"})

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
        create_remnawave_router(
            settings,
            transport=httpx.MockTransport(transport),
            clock=lambda: NOW,
        )
    )
    return client, captured


def deliver(client, captured, raw):
    with client:
        response = client.post(PATH, content=raw, headers=headers(raw))
    forwards = [request for request in captured if request.url.path == PATH]
    assert len(forwards) == 1
    assert forwards[0].url.host == "primary"
    assert forwards[0].content == raw
    assert forwards[0].headers["x-remnawave-signature"] == headers(raw)["x-remnawave-signature"]
    assert forwards[0].headers["x-remnawave-timestamp"] == headers(raw)["x-remnawave-timestamp"]
    return response


@pytest.mark.parametrize("body", [b'{"status":"accepted"}', b' { "status": "duplicate" }\n'])
@pytest.mark.parametrize("probe_error", ["unavailable", "timeout", "forged"])
def test_opt_in_returns_exact_confirmed_primary_user_ack_after_probe_failure(
    synthetic_scope,
    monkeypatch,
    body,
    probe_error,
):
    client, captured = recovery_context(synthetic_scope, monkeypatch, body=body, probe_error=probe_error)
    response = deliver(client, captured, event())
    assert response.status_code == 200
    assert response.content == body
    assert response.headers["content-type"] == "application/json"
    assert len(captured) == 2


@pytest.mark.parametrize("enabled", [None, False])
def test_default_or_explicitly_disabled_recovery_preserves_retry(synthetic_scope, monkeypatch, enabled):
    client, captured = recovery_context(synthetic_scope, monkeypatch, enabled=enabled)
    response = deliver(client, captured, event())
    assert response.status_code == 503
    assert response.json() == {"code": "membership_unavailable"}


@pytest.mark.parametrize("body", [b'{"status":"accepted"}', b'{"status":"duplicate"}'])
def test_primary_node_ack_never_hides_unavailable_test_fanout(synthetic_scope, monkeypatch, body):
    client, captured = recovery_context(synthetic_scope, monkeypatch, body=body)
    response = deliver(client, captured, event("node.disabled", "32345678-1234-1234-1234-123456789abc"))
    assert response.status_code == 503
    assert response.json() == {"code": "membership_unavailable"}


@pytest.mark.parametrize(
    "body",
    [
        b'{"status":"ignored"}',
        b'{"status":"unknown"}',
        b'{"status":"Accepted"}',
        b'{"status":"accepted","extra":true}',
        b'{"status":"accepted","status":"accepted"}',
        b'{"status":"ignored","status":"accepted"}',
        b'{"status":true}',
        b'{"status":200}',
        b'{"status":null}',
        b'{"status":["accepted"]}',
        b'{"status":{"value":"accepted"}}',
        b"{}",
        b"[]",
        b'"accepted"',
        b"null",
        b"not-json",
        b"\xff",
        b'{"status":"accepted"} trailing',
        b'{"status":"accepted"}\n{"status":"accepted"}',
        b'{"status":' + b"[" * 1100 + b"0" + b"]" * 1100 + b"}",
    ],
)
def test_unconfirmed_primary_body_requires_retry_without_test_delivery(synthetic_scope, monkeypatch, body):
    client, captured = recovery_context(synthetic_scope, monkeypatch, body=body)
    response = deliver(client, captured, event())
    assert response.status_code == 503
    assert response.json() == {"code": "membership_unavailable"}


@pytest.mark.parametrize("status", [201, 202, 204, 301, 400, 401, 403, 429, 500, 503])
def test_non_200_primary_response_cannot_acknowledge_recovery(synthetic_scope, monkeypatch, status):
    client, captured = recovery_context(synthetic_scope, monkeypatch, status=status)
    response = deliver(client, captured, event())
    assert response.status_code == 503
    assert response.json() == {"code": "membership_unavailable"}


@pytest.mark.parametrize("primary_error", ["timeout", "connect"])
def test_unknown_primary_transport_outcome_stays_retryable_without_extra_post(
    synthetic_scope,
    monkeypatch,
    primary_error,
):
    client, captured = recovery_context(synthetic_scope, monkeypatch, primary_error=primary_error)
    response = deliver(client, captured, event())
    assert response.status_code == 503
    assert response.json() == {"code": "backend_unavailable"}


def test_opt_in_never_bypasses_provider_signature(synthetic_scope, monkeypatch):
    client, captured = recovery_context(synthetic_scope, monkeypatch)
    raw = event()
    with client:
        response = client.post(PATH, content=raw, headers=headers(raw) | {"x-remnawave-signature": "0" * 64})
    assert response.status_code == 401
    assert not captured


@pytest.mark.parametrize(
    "kind,identifier,admitted,targets",
    [
        ("user.modified", 432, False, ["primary"]),
        ("user.modified", 432, True, ["isolated"]),
        ("node.disabled", "32345678-1234-1234-1234-123456789abc", False, ["primary"]),
        ("node.disabled", "32345678-1234-1234-1234-123456789abc", True, ["primary", "isolated"]),
    ],
)
def test_opt_in_preserves_successful_membership_routing(
    synthetic_scope,
    monkeypatch,
    kind,
    identifier,
    admitted,
    targets,
):
    monkeypatch.setenv("SUNRISE_REMNA_ROUTER_USER_PRIMARY_ACK_ON_MEMBERSHIP_FAILURE", "true")
    client, captured = context(synthetic_scope, admitted=admitted)
    raw = event(kind, identifier)
    with client:
        response = client.post(PATH, content=raw, headers=headers(raw))
    assert response.status_code == 200
    forwards = [request for request in captured if request.url.path == PATH]
    assert [request.url.host for request in forwards] == targets
    assert all(request.content == raw for request in forwards)


def test_oversized_primary_response_keeps_existing_bounded_failure(synthetic_scope, monkeypatch):
    client, captured = recovery_context(
        synthetic_scope, monkeypatch, body=b" " * 65536 + b'{"status":"accepted"}'
    )
    response = deliver(client, captured, event())
    assert response.status_code == 503
    assert response.json() == {"code": "backend_unavailable"}
