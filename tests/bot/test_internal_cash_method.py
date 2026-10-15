import asyncio

import pytest
from test_dispatcher import setup


@pytest.mark.parametrize(
    "total,credit,minimum,confirm",
    [
        ("0.00", "75.00", False, True),
        ("1.00", "74.00", False, False),
        ("0.00", "0.00", False, False),
        ("0.00", "75.00", True, False),
    ],
)
def test_empty_gateway_catalog_explicit_cash_uses_only_authoritative_internal_quote(
    total, credit, minimum, confirm
):
    async def run():
        app, service, session = setup()
        catalog, dashboard, quote = service.catalog, service.dashboard, service.quote
        service.catalog = lambda: catalog() | {"payment_methods": []}
        service.dashboard = lambda aid: dashboard(aid) | {"credit_available": "100.00"}
        service.quote = lambda aid, **kwargs: (
            quote(aid, **kwargs)
            | {
                "total": total,
                "credit_amount": credit,
                "minimum_external_required": minimum,
                "payment_method": None,
            }
        )
        await app.execute(
            "101",
            "9223372036854775700",
            "cash_max",
            {"product_id": "standard", "period_days": 30, "intent": "purchase"},
            None,
            "",
        )
        requests = [call for call in service.calls if call[0] == "quote"]
        assert len(requests) == 1
        assert requests[0][2]["credit_amount"] == "100.00"
        assert requests[0][2].get("payment_method") is None
        assert ("Создать заказ" in str(session.calls[-1].reply_markup)) is confirm
        assert not [call for call in service.calls if call[0] == "create"]

    asyncio.run(run())
