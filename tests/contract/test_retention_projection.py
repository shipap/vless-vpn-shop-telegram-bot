from sunrise_api.schemas import SubscriptionDto


def test_subscription_dto_preserves_shared_archive_and_discount_evidence():
    result = SubscriptionDto(
        id="synthetic",
        product_id="product",
        name="Подписка",
        status="expired",
        expires_at=None,
        devices_limit=4,
        version=1,
        connection_url=None,
        devices=[],
        segments=[],
        recurring=None,
        retention_state="archived",
        cleanup_at="2026-10-11T00:00:00+00:00",
        archived_at="2026-10-11T00:00:01+00:00",
        manual_renewal_discount_eligible=False,
        manual_renewal_discount_percent=None,
    ).model_dump()
    assert result["retention_state"] == "archived"
    assert result["archived_at"] == "2026-10-11T00:00:01+00:00"
    assert result["manual_renewal_discount_eligible"] is False
