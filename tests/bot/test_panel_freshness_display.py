"""Real bot subscription rendering distinguishes verified limits from cached data."""

import asyncio
from types import SimpleNamespace

import pytest
from test_instance_terminology import private_app


@pytest.mark.parametrize(
    "facts, expected",
    [
        ({"panel_sync_state": "fresh", "panel_device_limit": 0}, "Устройств: Без ограничения"),
        ({"panel_sync_state": "fresh", "panel_device_limit": 12}, "Устройств: 12"),
        ({"panel_sync_state": "fresh", "panel_device_limit": None}, "Лимит панели не определён"),
        ({"panel_sync_state": "stale"}, "Показаны сохранённые данные"),
        ({"panel_sync_state": "denied"}, "Показаны сохранённые данные"),
        ({"panel_sync_state": "pending"}, "Изменения подключения выполняются"),
        ({"panel_sync_state": "fresh", "panel_product_state": "ambiguous"}, "Тариф панели требует проверки"),
    ],
)
def test_subscription_screen_explains_panel_facts(facts, expected):
    async def run():
        app, service, session = private_app()
        sub = service.subscriptions[0]
        sub.update(facts)
        await app.execute("101", "9223372036854775700", "subscription", {"sid": sub["id"]}, None, "")
        assert expected in session.calls[-1].text

    asyncio.run(run())


def test_expired_invoice_calculation_is_not_described_as_provider_downtime(monkeypatch):
    from sunrise_core.errors import DomainError
    from sunrise_infrastructure.billing import Billing

    def expired(*args, **kwargs):
        raise DomainError("quote_expired", "Срок расчёта истёк. Создайте новый заказ.")

    monkeypatch.setattr(Billing, "prepare_payment", expired)

    async def run():
        app, service, session = private_app()
        service.db = object()
        app.payment_provider = SimpleNamespace(terminal="synthetic")
        order = service.create_order("9223372036854775700", "q1", "synthetic", "bot")
        order.update(status="awaiting_payment", environment="sandbox")
        await app.execute("101", "9223372036854775700", "order", {"order_id": order["id"]}, None, "")
        assert "Расчёт истёк" in session.calls[-1].text
        assert "временно недоступен" not in session.calls[-1].text

    asyncio.run(run())


@pytest.mark.parametrize("provisioning", ["pending", "review", None])
def test_active_subscription_does_not_prove_own_order_is_ready(provisioning):
    async def run():
        app, service, session = private_app()
        order = service.create_order("9223372036854775700", "q1", "synthetic", "bot")
        order.update(
            status="paid",
            environment="sandbox",
            subscription_id=service.subscriptions[0]["id"],
            provisioning_status=provisioning,
        )
        await app.execute("101", "9223372036854775700", "order", {"order_id": order["id"]}, None, "")
        assert "Доступ выдан" not in session.calls[-1].text
        assert "Оплата получена" in session.calls[-1].text
        if provisioning != "pending":
            assert "требует проверки" in session.calls[-1].text

    asyncio.run(run())
