from sunrise_api.schemas import PaymentDto


def test_actual_fee_currency_survives_customer_api_serialization():
    payment = PaymentDto(
        id="synthetic",
        order_id="synthetic-order",
        amount="100.00",
        currency="RUB",
        status="paid",
        paid_at="2026-10-09T12:00:00Z",
        fee="0.48",
        fee_currency="USDT",
        environment="local_test",
    )
    assert payment.model_dump()["fee_currency"] == "USDT"
