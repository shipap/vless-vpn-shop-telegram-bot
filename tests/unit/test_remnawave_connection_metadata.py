"""Pinned 3.4.5 nullable connection fields are not inferred from missing/reset traffic."""

import pytest
from sunrise_infrastructure import panel_v3


def user():
    return {
        "createdAt": "2026-10-10T00:00:00Z",
        "password": "must-not-copy",
        "userTraffic": {
            "firstConnectedAt": None,
            "onlineAt": None,
            "lifetimeUsedTrafficBytes": 0,
            "usedTrafficBytes": 0,
        },
    }


def test_explicit_null_connection_fields_are_preserved_without_secrets():
    result = panel_v3.connection_metadata(user())
    assert result == {
        "created_at": "2026-10-10T00:00:00+00:00",
        "first_connected_at": None,
        "online_at": None,
        "lifetime_traffic_bytes": "0",
    }


@pytest.mark.parametrize("field", ["firstConnectedAt", "onlineAt", "lifetimeUsedTrafficBytes"])
def test_missing_connection_field_is_not_converted_to_null_or_zero(field):
    data = user()
    del data["userTraffic"][field]
    result = panel_v3.connection_metadata(data)
    snake = {
        "firstConnectedAt": "first_connected_at",
        "onlineAt": "online_at",
        "lifetimeUsedTrafficBytes": "lifetime_traffic_bytes",
    }[field]
    assert snake not in result


@pytest.mark.parametrize("bad", [True, -1, "0", 9007199254740992])
def test_invalid_lifetime_traffic_does_not_prove_no_connection(bad):
    data = user()
    data["userTraffic"]["lifetimeUsedTrafficBytes"] = bad
    assert panel_v3.connection_metadata(data) == {}


def test_positive_lifetime_survives_current_traffic_reset():
    data = user()
    data["userTraffic"]["lifetimeUsedTrafficBytes"] = 4096
    assert panel_v3.connection_metadata(data)["lifetime_traffic_bytes"] == "4096"


@pytest.mark.parametrize("field", ["firstConnectedAt", "onlineAt"])
def test_malformed_connection_timestamp_is_unknown(field):
    data = user()
    data["userTraffic"][field] = "not-a-date"
    assert panel_v3.connection_metadata(data) == {}
