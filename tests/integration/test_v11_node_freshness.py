from datetime import datetime, timedelta
from uuid import uuid4

import fakeredis
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sunrise_api.main import create_app
from sunrise_infrastructure.db import Account, PanelObservation, Product, Session, Setting, now
from sunrise_infrastructure.panel_v3 import RemnawaveV3
from sunrise_infrastructure.security import digest

from tests.e2e.local_auth_server import SyntheticSettings
from tests.isolated_database import isolated_database


@pytest.fixture
def node_api():
    with isolated_database("v11_nodes_test") as db:
        node_id, hidden_id = str(uuid4()), str(uuid4())
        config = SyntheticSettings(
            _env_file=None,
            database_url=db.engine.url.render_as_string(hide_password=False),
            secret_key="synthetic-node-status-secret-" * 2,
            local_test=True,
            allowed_hosts=["testserver"],
            panel_test_node_uuids=[node_id, hidden_id],
        )
        cookie = "synthetic-node-session"
        with db.transaction() as session:
            account = Account(email="nodes@example.test", email_verified=True)
            session.add(account)
            session.flush()
            session.add(
                Session(
                    id=digest(cookie),
                    account_id=account.id,
                    csrf="synthetic-node-csrf",
                    expires_at=now() + timedelta(days=1),
                )
            )
            session.add(
                Setting(
                    key="node_visibility",
                    value={
                        node_id: {"visible": True, "name": "Тестовый узел"},
                        hidden_id: {"visible": False, "name": "Скрытый узел"},
                    },
                )
            )
        app = create_app(config, redis_client=fakeredis.FakeRedis())
        requests = []

        def no_http(request):
            requests.append(request.url.path)
            raise AssertionError("Server screen uses the current admitted caption cache")

        panel = RemnawaveV3(
            base_url="https://panel.example.test",
            token="synthetic-token",  # noqa: S106 -- MockTransport only
            authorized=True,
            allowed_hosts={"panel.example.test"},
            allowed_squads={str(uuid4())},
            auto_test_telegram_ids={"730000000000001"},
            allowed_nodes={node_id, hidden_id},
            transport=httpx.MockTransport(no_http),
        )
        app.state.commerce.panel_provider = panel
        with db.transaction() as session:
            product = session.scalar(select(Product).where(Product.available))
            session.add(
                Setting(
                    key="panel_event_routes",
                    value={
                        "version": 1,
                        "environment": "local_test",
                        "contract": "3.4_id",
                        "panel_origin": "https://panel.example.test",
                        "nodes": [
                            {"node_uuid": identifier, "product_ids": [product.id], "safe_label": label}
                            for identifier, label in (
                                (node_id, "Публичный сервер"),
                                (hidden_id, "Скрытый сервер"),
                            )
                        ],
                        "infra": [],
                    },
                )
            )
        try:
            with TestClient(app, client=("127.0.0.1", 50000)) as client:
                client.cookies.set("sunrise_session", cookie)
                yield db, client, node_id, hidden_id, requests
        finally:
            panel.client.close()
            app.state.commerce.db.engine.dispose()


@pytest.mark.parametrize(
    "age,last_error,expected_state",
    [
        (1, "panel_unavailable", "stale"),
        (1, None, "fresh"),
        (11, None, "stale"),
        (11, "panel_unavailable", "stale"),
    ],
)
def test_node_state_preserves_observation_and_requires_recent_success(
    node_api, age, last_error, expected_state
):
    db, client, node_id, _, requests = node_api
    observed_at = now() - timedelta(minutes=age)
    with db.transaction() as session:
        session.add(
            PanelObservation(
                kind="node",
                resource_id=node_id,
                status="CONNECTED",
                observed_at=observed_at,
                last_error=last_error,
            )
        )

    response = client.get("/api/v1/nodes")

    assert response.status_code == 200
    assert requests == []
    data = response.json()
    assert data["state"] == expected_state
    assert data["source"] == "panel"
    assert len(data["items"]) == 1
    assert {key: value for key, value in data["items"][0].items() if key != "observed_at"} == {
        "id": node_id,
        "name": "Тестовый узел",
        "status": "CONNECTED",
    }
    assert datetime.fromisoformat(data["items"][0]["observed_at"]) == observed_at
    with db.transaction() as session:
        stored = session.scalar(select(PanelObservation).where(PanelObservation.resource_id == node_id))
        assert stored.status == "CONNECTED"
        assert stored.observed_at == observed_at
        assert stored.last_error == last_error


def test_hidden_failed_node_does_not_make_visible_success_stale(node_api):
    db, client, node_id, hidden_id, requests = node_api
    observed_at = now() - timedelta(minutes=1)
    with db.transaction() as session:
        session.add_all(
            [
                PanelObservation(
                    kind="node",
                    resource_id=node_id,
                    status="DISCONNECTED",
                    observed_at=observed_at,
                ),
                PanelObservation(
                    kind="node",
                    resource_id=hidden_id,
                    status="CONNECTED",
                    observed_at=observed_at,
                    last_error="panel_unavailable",
                ),
            ]
        )

    response = client.get("/api/v1/nodes")

    assert response.status_code == 200
    assert requests == []
    assert response.json()["state"] == "fresh"
    assert [row["id"] for row in response.json()["items"]] == [node_id]
    assert response.json()["items"][0]["status"] == "DISCONNECTED"
    assert datetime.fromisoformat(response.json()["items"][0]["observed_at"]) == observed_at


def test_no_visible_observation_remains_unknown(node_api):
    _, client, _, _, requests = node_api

    response = client.get("/api/v1/nodes")

    assert response.status_code == 200
    assert requests == []
    assert response.json() == {"items": [], "source": "panel", "state": "unknown"}
