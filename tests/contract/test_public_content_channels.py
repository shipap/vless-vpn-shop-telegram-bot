from sunrise_api.schemas import PublicContentUpdate


def test_public_content_schema_preserves_explicit_promo_channels_and_partial_updates():
    channels = [{"title": "Новости", "url": "https://example.test/news"}]
    common = {"reason": "Синтетический канал", "confirmed": True, "faq": [], "how_to_connect": []}
    supplied = PublicContentUpdate(**common, promo_channels=channels)
    assert supplied.model_dump(exclude_none=True)["promo_channels"] == channels
    assert "promo_channels" not in PublicContentUpdate(**common).model_dump(exclude_none=True)
