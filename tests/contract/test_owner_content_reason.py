import pytest
from sunrise_api.schemas import ProductUpdate, PublicContentUpdate


def test_ordinary_content_uses_automatic_reason():
    body = ProductUpdate(confirmed=True, name="Новое название")
    assert len(body.reason) >= 5
    body = PublicContentUpdate(confirmed=True, faq=[], how_to_connect=[])
    assert len(body.reason) >= 5


@pytest.mark.parametrize(
    "values",
    [
        {"price": "10.00"},
        {"included_devices": 10},
        {"internal_squad_uuid": "11111111-1111-4111-8111-111111111111"},
    ],
)
def test_sensitive_product_edits_keep_automatic_audit_reason(values):
    assert len(ProductUpdate(confirmed=True, **values).reason) >= 5
