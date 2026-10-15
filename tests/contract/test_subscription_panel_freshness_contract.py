from sunrise_api.schemas import SubscriptionDto


def test_profile_serialization_preserves_actual_panel_and_future_paid_horizon_separately():
    sub = SubscriptionDto(
        id="synthetic",
        product_id="product",
        name="Подписка",
        status="active",
        expires_at="2026-11-01T00:00:00Z",
        devices_limit=5,
        version=1,
        connection_url=None,
        devices=[],
        segments=[],
        recurring=None,
        paid_horizon="2027-01-01T00:00:00Z",
        panel_sync_state="fresh",
        panel_sync_error=None,
        panel_device_limit=0,
        renewal_pricing_state="blocked",
        panel_product_state="matched",
        panel_product_id="current-product",
    )
    value = sub.model_dump()
    assert value["paid_horizon"] == "2027-01-01T00:00:00Z"
    assert value["panel_device_limit"] == 0
    assert value["renewal_pricing_state"] == "blocked"
    assert value["panel_product_state"] == "matched"
    assert value["panel_product_id"] == "current-product"
