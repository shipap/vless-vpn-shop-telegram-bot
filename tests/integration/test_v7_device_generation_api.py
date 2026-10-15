"""HTTP transport retains the exact device generation the user confirmed."""

import pytest
from sunrise_infrastructure.customer_operations import CustomerOperations

from tests.integration.test_v11_api import api_context as api_context
from tests.integration.test_v11_notifications import notification_context as notification_context


def test_device_batch_forwards_approved_generation_without_bypassing_auth_or_csrf(api_context, monkeypatch):
    client, _, _ = api_context
    calls = []

    def capture(
        self,
        account_id,
        sid,
        device_ids,
        key,
        confirmed=False,
        *,
        expected_generations=None,
        operation_mode=None,
    ):
        calls.append((account_id, sid, device_ids, key, confirmed, expected_generations, operation_mode))
        return {"status": "revoke_pending", "device_ids": device_ids, "job_ids": ["synthetic-job"]}

    monkeypatch.setattr(CustomerOperations, "revoke_devices", capture)
    path = "/api/v1/subscriptions/synthetic-subscription/devices/revoke-bulk"
    payload = {
        "device_ids": ["synthetic-device"],
        "expected_generations": {"synthetic-device": 3},
        "idempotency_key": "synthetic-generation-request",
        "confirmed": True,
    }
    assert client.post(path, json=payload).status_code == 401
    client.cookies.set("sunrise_session", "synthetic-session")
    assert client.post(path, json=payload).status_code == 403
    assert not calls
    response = client.post(path, headers={"X-CSRF-Token": "synthetic-csrf"}, json=payload)
    assert response.status_code == 200
    assert len(calls) == 1
    assert calls[0][1:] == (
        "synthetic-subscription",
        ["synthetic-device"],
        "synthetic-generation-request",
        True,
        {"synthetic-device": 3},
        None,
    )


@pytest.mark.parametrize("value", [True, 1.1, "3", 0, -1])
def test_device_batch_rejects_invalid_generation_before_service_call(api_context, monkeypatch, value):
    client, _, _ = api_context
    calls = []
    monkeypatch.setattr(CustomerOperations, "revoke_devices", lambda *args, **kwargs: calls.append(True))
    client.cookies.set("sunrise_session", "synthetic-session")
    response = client.post(
        "/api/v1/subscriptions/synthetic-subscription/devices/revoke-bulk",
        headers={"X-CSRF-Token": "synthetic-csrf"},
        json={
            "device_ids": ["synthetic-device"],
            "expected_generations": {"synthetic-device": value},
            "idempotency_key": "synthetic-invalid-generation",
            "confirmed": True,
        },
    )
    assert response.status_code == 422
    assert not calls
