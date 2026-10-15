from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from sunrise_infrastructure.sale_notifications import staff_sale_content


def test_unknown_operation_and_missing_legacy_fields_are_reported_without_inventing_data():
    account = SimpleNamespace(
        id=9007199254740993, telegram_id=None, telegram_username="obsolete", email=None, email_verified=False
    )
    order = SimpleNamespace(
        id="order",
        subscription_id="subscription",
        amount=Decimal("0.00"),
        currency="EUR",
        source="recurring",
        fulfilled_at=None,
    )
    title, body = staff_sale_content(account, order, {"action": "future_operation"})
    assert title == "Операция с подпиской"
    assert "Тип операции: неизвестен" in body
    assert "Автопродление" not in body
    assert "Сумма заказа: 0.00 EUR" in body
    assert "Выполнено: неизвестно" in body
    assert "название отсутствует в снимке заказа" in body
    assert "Email:" not in body and "obsolete" not in body


def test_user_controlled_fields_cannot_create_fake_lines_and_moscow_date_rolls_over():
    account = SimpleNamespace(
        id=123,
        telegram_id=None,
        telegram_username=None,
        email="buyer@example.test\nЗаказ: fake",
        email_verified=False,
    )
    order = SimpleNamespace(
        id="real",
        subscription_id="sub",
        amount=Decimal("1.20"),
        currency="RUB",
        source="site",
        fulfilled_at=datetime(2026, 10, 8, 22, 15, tzinfo=UTC),
    )
    title, body = staff_sale_content(
        account,
        order,
        {"action": "purchase", "product_name": "A\r\nB", "devices_limit": 3, "basis": {"period_days": 90}},
    )
    assert title == "Покупка подписки"
    assert "\nТариф: A B\n" in body
    assert "\nЗаказ: fake" not in body
    assert "Email: buyer@example.test Заказ: fake (не подтверждён)" in body
    assert "Выполнено: 09.10.2026 01:15:00 МСК" in body
