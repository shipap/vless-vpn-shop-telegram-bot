"""HTTP отмены использует подтверждённого владельца текущей сессии."""

from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sunrise_api.v11_routes import install_v11_routes
from sunrise_infrastructure import admin_operations, campaigns, customer_operations, panel_events


def test_cancel_route_requires_auth_confirmation_shape_and_forwards_only_session_owner(monkeypatch):
    calls = []

    def cancel(*args, **kwargs):
        calls.append((args, kwargs))
        return {"status": "rotate_canceled", "job_id": args[2]}

    monkeypatch.setattr(admin_operations, "AdminOperations", lambda *args: SimpleNamespace())
    monkeypatch.setattr(campaigns, "Campaigns", lambda *args: SimpleNamespace())
    monkeypatch.setattr(panel_events, "PanelEvents", lambda *args, **kwargs: SimpleNamespace())
    monkeypatch.setattr(
        customer_operations, "CustomerOperations", lambda *args: SimpleNamespace(cancel_rotation=cancel)
    )
    allowed = [False]

    def authenticated():
        if not allowed[0]:
            raise HTTPException(401)
        return SimpleNamespace(id=9007199254740993), SimpleNamespace()

    app = FastAPI()
    install_v11_routes(
        app,
        authenticated,
        authenticated,
        None,
        None,
        SimpleNamespace(db=None, panel_provider=None),
        SimpleNamespace(),
    )
    client = TestClient(app)
    path = "/api/v1/subscriptions/synthetic/credentials/rotation/cancel"
    body = {"job_id": "synthetic-job", "idempotency_key": "synthetic-key", "confirmed": True}
    assert client.post(path, json=body).status_code == 401 and calls == []
    allowed[0] = True
    assert client.post(path, json=body | {"account_id": "999"}).status_code == 422 and calls == []
    assert (
        client.post(path, json={key: value for key, value in body.items() if key != "confirmed"}).status_code
        == 422
    )
    assert client.post(path, json=body).json() == {"status": "rotate_canceled", "job_id": "synthetic-job"}
    assert calls == [((9007199254740993, "synthetic", "synthetic-job", "synthetic-key"), {"confirmed": True})]
