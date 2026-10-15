"""Signed scoped node changes produce only mapped in-app chronology."""

from datetime import datetime
from types import SimpleNamespace

from sunrise_api.schemas import PanelEventRoutesResponse
from sunrise_infrastructure import panel_news
from sunrise_infrastructure.db import Setting
from sunrise_infrastructure.panel_event_routes import PanelEventRoutes, registration_context

NODE = "32345678-1234-1234-1234-123456789abc"
STAMP = "2026-10-13T10:00:00+00:00"


class Rows:
    def __init__(self):
        self.rows = {}

    def get(self, model, key):
        return self.rows.get((model, key))

    def add(self, row):
        self.rows[(type(row), row.key)] = row


def test_scoped_registration_context_uses_real_scoped_environment():
    panel = SimpleNamespace(contract="3.4_id", api_host="panel.synthetic.invalid", client=SimpleNamespace(base_url="https://panel.synthetic.invalid/api/"))
    settings = SimpleNamespace(runtime_mode="scoped_test", local_test=True)
    assert registration_context(panel, settings)["environment"] == "scoped_live"


def test_scoped_registration_context_can_be_rendered_by_admin_api():
    context = {
        "version": 1,
        "environment": "scoped_live",
        "panel_origin": "https://panel.synthetic.invalid",
        "contract": "3.4_id",
    }
    response = PanelEventRoutesResponse.model_validate(
        {
            "configured": False,
            "policy": None,
            "registration_context": context,
            "approved_node_uuids": [],
            "approved_product_ids": [],
        }
    )
    assert response.registration_context.environment == "scoped_live"


def test_scoped_disabled_and_enabled_node_news_uses_signed_event_only(monkeypatch):
    route = PanelEventRoutes.model_validate(
        {
            "version": 1,
            "environment": "scoped_live",
            "panel_origin": "https://panel.synthetic.invalid",
            "contract": "3.4_id",
            "nodes": [{"node_uuid": NODE, "product_ids": ["plan"], "safe_label": "Узел 1"}],
            "infra": [],
        }
    )
    monkeypatch.setattr(panel_news, "policy_in", lambda *_: route)
    monkeypatch.setattr(panel_news, "now", lambda: datetime.fromisoformat(STAMP))
    rows = Rows()
    for kind, status in [("node.disabled", "DISABLED"), ("node.enabled", "CONNECTED")]:
        panel_news.record_node_news_in(
            rows, object(), object(), NODE, {"status": status}, [(kind, None, kind, {"timestamp": STAMP})]
        )
    values = [row.value for (model, _), row in rows.rows.items() if model is Setting]
    assert sorted(item["state"] for item in values) == ["outage", "recovered"]
    assert all(item["environment"] == "scoped_live" for item in values)
