import asyncio

import pytest
from test_dispatcher import setup


@pytest.mark.parametrize(
    "fee_currency,expected", [("USDT", "0,48 USDT"), (None, "Валюта комиссии неизвестна")]
)
def test_payment_history_never_infers_fee_currency_from_customer_payment(fee_currency, expected):
    async def run():
        app, service, session = setup()
        original = service.dashboard

        def dashboard(account_id, **kwargs):
            result = original(account_id)
            result["payments"] = [
                {
                    "id": "synthetic-payment",
                    "order_id": "synthetic-order",
                    "amount": "100.00",
                    "currency": "RUB",
                    "status": "paid",
                    "paid_at": "2026-10-09T12:00:00Z",
                    "fee": "0.48",
                    "fee_currency": fee_currency,
                }
            ]
            return result

        service.dashboard = dashboard
        await app.execute("101", "9223372036854775700", "history", {}, None, "")
        assert expected in session.calls[-1].text
        assert "0.48 ₽" not in session.calls[-1].text

    asyncio.run(run())
