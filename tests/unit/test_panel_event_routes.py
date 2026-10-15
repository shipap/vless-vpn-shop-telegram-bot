"""Explicit owner mappings are strict, bounded and never inferred from webhook names."""

import importlib

import pytest
from pydantic import ValidationError


def policy():
    return {
        "version": 1,
        "environment": "local_test",
        "panel_origin": "https://panel.example.test",
        "contract": "3.4_id",
        "nodes": [
            {
                "node_uuid": "30000000-0000-4000-8000-000000000003",
                "product_ids": ["standard"],
                "safe_label": "Тестовый сервер",
            }
        ],
        "infra": [
            {
                "provider_name": "synthetic-provider",
                "node_name": "synthetic-infra",
                "safe_label": "Тестовый сервер",
            }
        ],
    }


@pytest.mark.parametrize(
    "wrong",
    [
        "version",
        "scheme",
        "path",
        "port",
        "field",
        "duplicate_node",
        "duplicate_pair",
        "label_url",
        "label_ip",
        "label_uuid",
        "empty_products",
        "duplicate_product",
    ],
)
def test_owner_event_map_rejects_unscoped_or_ambiguous_data(wrong):
    cls = importlib.import_module("sunrise_infrastructure.panel_event_routes").PanelEventRoutes
    value = policy()
    if wrong == "version":
        value["version"] = True
    if wrong == "scheme":
        value["panel_origin"] = "http://panel.example.test"
    if wrong == "path":
        value["panel_origin"] += "/other"
    if wrong == "port":
        value["panel_origin"] += ":9443"
    if wrong == "field":
        value["login_url"] = "https://private.example.test/"
    if wrong == "duplicate_node":
        value["nodes"] *= 2
    if wrong == "duplicate_pair":
        value["infra"] *= 2
    if wrong == "label_url":
        value["nodes"][0]["safe_label"] = "https://private.example.test"
    if wrong == "label_ip":
        value["nodes"][0]["safe_label"] = "192.0.2.1"
    if wrong == "label_uuid":
        value["nodes"][0]["safe_label"] = value["nodes"][0]["node_uuid"]
    if wrong == "empty_products":
        value["nodes"][0]["product_ids"] = []
    if wrong == "duplicate_product":
        value["nodes"][0]["product_ids"] *= 2
    with pytest.raises(ValidationError):
        cls.model_validate(value)


def test_owner_event_map_accepts_explicit_exact_pair_without_cost_or_url():
    cls = importlib.import_module("sunrise_infrastructure.panel_event_routes").PanelEventRoutes
    assert cls.model_validate(policy()).model_dump() == policy()
