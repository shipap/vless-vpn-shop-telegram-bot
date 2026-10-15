# ruff: noqa: F811 -- pytest injects the imported synthetic fixture
import hashlib
import hmac
import importlib.util
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.contract.test_scoped_payment_policy import synthetic_scope  # noqa: F401


def router_context(synthetic_scope):
    assert importlib.util.find_spec("sunrise_api.webhook_router") is not None
    from sunrise_api.webhook_router import CallbackRouterSettings, create_router

    requests = []

    def forward(request):
        requests.append(request)
        return httpx.Response(200, json={"status": "accepted"})

    settings = CallbackRouterSettings(
        _env_file=None,
        signing_secret="synthetic-signing-key",  # noqa: S106 -- фиктивная подпись
        scoped_test_admission_file=synthetic_scope[0]["scoped_test_admission_file"],
        scoped_test_admission_sha256=synthetic_scope[0]["scoped_test_admission_sha256"],
        primary_url="http://primary:8000/api/v1/webhooks/rollypay",
        primary_host="main.example.test",
        test_url="http://isolated:8000/api/v1/webhooks/rollypay",
        test_host="qa.example.test",
        allowed_backend_hosts=["primary", "isolated"],
    )
    return TestClient(create_router(settings, transport=httpx.MockTransport(forward))), requests


def signature(raw):
    timestamp = str(int(time.time()))
    return {
        "x-timestamp": timestamp,
        "x-signature": hmac.new(
            b"synthetic-signing-key", timestamp.encode() + b"." + raw, hashlib.sha256
        ).hexdigest(),
        "content-type": "application/json",
    }


@pytest.mark.parametrize(
    "order,target,host",
    [
        ("qa_own", "isolated", "qa.example.test"),
        ("7b0e5c66-a648-40d3-9067-8c040a18052d", "primary", "main.example.test"),
    ],
)
def test_signed_callback_routes_only_test_namespace_and_preserves_bytes(synthetic_scope, order, target, host):
    client, requests = router_context(synthetic_scope)
    raw = json.dumps(
        {"order_id": order, "payment_id": "payment", "status": "paid"}, separators=(",", ":")
    ).encode()
    headers = signature(raw)
    with client:
        response = client.post("/api/v1/webhooks/rollypay", content=raw, headers=headers)
    assert response.status_code == 200 and len(requests) == 1
    assert requests[0].url.host == target and requests[0].headers["host"] == host
    assert requests[0].content == raw
    assert requests[0].headers["x-signature"] == headers["x-signature"]


@pytest.mark.parametrize("raw", [b'{"order_id":"qa_one","order_id":"main"}', b"[]", b"{"])
def test_invalid_json_never_forwards(synthetic_scope, raw):
    client, requests = router_context(synthetic_scope)
    with client:
        response = client.post("/api/v1/webhooks/rollypay", content=raw, headers=signature(raw))
    assert response.status_code == 400 and not requests


def test_invalid_signature_never_forwards(synthetic_scope):
    client, requests = router_context(synthetic_scope)
    with client:
        response = client.post(
            "/api/v1/webhooks/rollypay", content=b"{}", headers={"x-timestamp": "0", "x-signature": "wrong"}
        )
    assert response.status_code == 403 and not requests


def test_oversized_body_never_forwards(synthetic_scope):
    client, requests = router_context(synthetic_scope)
    raw = b"x" * 65537
    with client:
        response = client.post("/api/v1/webhooks/rollypay", content=raw, headers=signature(raw))
    assert response.status_code == 413 and not requests


@pytest.mark.parametrize("order", [None, "different_test_order", 123])
def test_unclassifiable_callback_never_reaches_primary(synthetic_scope, order):
    client, requests = router_context(synthetic_scope)
    raw = json.dumps({"order_id": order, "status": "created"}).encode()
    with client:
        response = client.post("/api/v1/webhooks/rollypay", content=raw, headers=signature(raw))
    assert response.status_code == 503 and not requests


def test_admission_drift_blocks_test_callbacks_but_preserves_primary(synthetic_scope):
    client, requests = router_context(synthetic_scope)
    with client:
        synthetic_scope[1].write_text("{}", encoding="utf-8")
        test_raw = b'{"order_id":"qa_own","status":"paid"}'
        response = client.post("/api/v1/webhooks/rollypay", content=test_raw, headers=signature(test_raw))
        assert response.status_code == 503 and not requests
        main_raw = b'{"order_id":"7b0e5c66-a648-40d3-9067-8c040a18052d","status":"paid"}'
        response = client.post("/api/v1/webhooks/rollypay", content=main_raw, headers=signature(main_raw))
        assert response.status_code == 200 and requests[0].url.host == "primary"


def test_invalid_initial_admission_prevents_router_startup(synthetic_scope):
    synthetic_scope[1].write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError, match="callback_router_configuration_denied"):
        router_context(synthetic_scope)
