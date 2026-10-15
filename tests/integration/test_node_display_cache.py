"""Server screens use current owned caption cache without panel requests."""

import httpx
import pyotp
import pytest
from sqlalchemy import select
from sunrise_infrastructure.db import PanelObservation, Product, Setting
from sunrise_infrastructure.panel_v3 import RemnawaveV3

from tests.integration.test_v10_admin_mfa import enroll, mfa_context  # noqa: F401

NODE = "11111111-1111-4111-8111-111111111111"


@pytest.mark.parametrize("basis", ["current", "missing", "wrong_origin", "revoked"])
def test_admin_nodes_use_safe_current_caption_manual_priority_and_no_panel_get(mfa_context, basis):  # noqa: F811
    db, app, client, _, _, _, _ = mfa_context
    _, headers, pending = enroll(mfa_context)
    assert (
        client.post(
            "/api/v1/admin/mfa/enroll/confirm",
            headers=headers,
            json={"code": pyotp.TOTP(pending["secret"]).now()},
        ).status_code
        == 200
    )
    config = app.state.settings
    config.panel_test_node_uuids = [NODE]
    requests = []

    def no_http(request):
        requests.append(request.url.path)
        raise AssertionError("Cached server screens must not query the panel")

    panel = RemnawaveV3(
        base_url="https://panel.example.test",
        token="synthetic-token",  # noqa: S106 -- MockTransport only
        authorized=True,
        allowed_hosts={"panel.example.test"},
        allowed_nodes={NODE},
        auto_test_telegram_ids={"70000000000000001"},
        allowed_squads={"22222222-2222-4222-8222-222222222222"},
        transport=httpx.MockTransport(no_http),
    )
    app.state.commerce.panel_provider = panel
    with db.transaction() as session:
        product = session.scalar(select(Product).where(Product.available))
        if product is None:
            product = Product(
                id="synthetic",
                code="synthetic",
                name="Синтетический тариф",
                tier_level=1,
                price=10,
                currency="RUB",
                included_devices=1,
            )
            session.add(product)
            session.flush()
        if basis != "missing":
            session.add(
                Setting(
                    key="panel_event_routes",
                    value={
                        "version": 1,
                        "environment": "local_test",
                        "contract": "3.4_id",
                        "panel_origin": "https://foreign.example.test"
                        if basis == "wrong_origin"
                        else "https://panel.example.test",
                        "nodes": [
                            {"node_uuid": NODE, "product_ids": [product.id], "safe_label": "🇫🇮 Финляндия #1"}
                        ],
                        "infra": [],
                    },
                )
            )
        session.add(
            PanelObservation(
                kind="node", resource_id=NODE, status="online", payload={"name": "PRIVATE-TECHNICAL-01"}
            )
        )
    if basis == "revoked":
        panel.nodes.clear()
    response = client.get("/api/v1/admin/nodes")
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["name"] == ("🇫🇮 Финляндия #1" if basis == "current" else "Название не определено")
    assert item["status"] == ("online" if basis == "current" else None)
    with db.transaction() as session:
        session.add(
            Setting(key="node_visibility", value={NODE: {"name": "Ручное название", "visible": True}})
        )
    assert client.get("/api/v1/admin/nodes").json()["items"][0]["name"] == "Ручное название"
    public = client.get("/api/v1/nodes").json()["items"]
    assert (public[0]["name"] if public else None) == ("Ручное название" if basis == "current" else None)
    assert requests == []
    panel.client.close()
