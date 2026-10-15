import pytest
from sunrise_core.errors import DomainError
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import Subscription

from tests.integration.test_v10_admin_mfa import login
from tests.isolated_database import isolated_database

pytest_plugins = ["tests.integration.test_v10_admin_mfa"]


class Loader:
    def __init__(self):
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        return [
            {
                "id": "android",
                "name": "Android",
                "apps": [
                    {
                        "name": "Happ",
                        "steps": [
                            {"title": "Настройка", "description": "Импортируйте подписку.", "links": []}
                        ],
                    }
                ],
            }
        ]


def test_only_owner_can_fetch_instructions_and_unavailable_page_preserves_fallback():
    with isolated_database("connection_guide") as db:
        loader = Loader()
        commerce = Commerce(db, local_test=True, connection_provider=loader)
        owner = commerce.resolve_telegram(70000000000000001)
        stranger = commerce.resolve_telegram(70000000000000002)
        with db.transaction() as session:
            sub = Subscription(
                devices_limit=4,
                account_id=int(owner["id"]),
                connection_url="https://subscription.example.test/synthetic",
            )
            session.add(sub)
            session.flush()
            sid = sub.id
        with pytest.raises(DomainError) as error:
            commerce.connection_instructions(stranger["id"], sid)
        assert error.value.status == 404
        assert loader.calls == []
        guide = commerce.connection_instructions(owner["id"], sid)
        assert guide["source"] == "remnawave"
        assert guide["platforms"][0]["apps"][0]["name"] == "Happ"
        assert len(loader.calls) == 1
        commerce.connection_provider = None
        fallback = commerce.connection_instructions(owner["id"], sid)
        assert fallback["source"] == "fallback"
        assert fallback["platforms"] == []
        assert "оферт" in fallback["notice"]


def test_authenticated_api_returns_only_normalized_own_guide(mfa_context):
    db, app, client, *_ = mfa_context
    auth = login(mfa_context)
    loader = Loader()
    commerce = app.state.commerce
    commerce.connection_provider = loader
    stranger = commerce.resolve_telegram(70000000000000001)
    with db.transaction() as session:
        own = Subscription(
            devices_limit=4,
            account_id=int(auth["account"]["id"]),
            connection_url="https://subscription.example.test/own",
        )
        other = Subscription(
            devices_limit=4,
            account_id=int(stranger["id"]),
            connection_url="https://subscription.example.test/other",
        )
        session.add_all([own, other])
        session.flush()
        own_id, other_id = own.id, other.id
    denied = client.get(f"/api/v1/subscriptions/{other_id}/connection-instructions")
    assert denied.status_code == 404
    assert loader.calls == []
    response = client.get(f"/api/v1/subscriptions/{own_id}/connection-instructions")
    assert response.status_code == 200
    assert response.json()["platforms"][0]["apps"][0]["name"] == "Happ"
    assert "connection_url" not in response.json()
    client.cookies.clear()
    denied = client.get(f"/api/v1/subscriptions/{own_id}/connection-instructions")
    assert denied.status_code == 401
    assert loader.calls == ["https://subscription.example.test/own"]
