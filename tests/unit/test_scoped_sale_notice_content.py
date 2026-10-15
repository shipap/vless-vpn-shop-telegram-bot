"""Scoped paid notices use the immutable quote, not current catalog data."""

from decimal import Decimal
from types import SimpleNamespace

from sunrise_infrastructure.sale_notifications import paid_staff_sale_content


def test_scoped_staff_sale_content_displays_actual_method_and_bonus():
    account = SimpleNamespace(telegram_id=202, telegram_username="test_buyer")
    order = SimpleNamespace(amount=Decimal("1.00"), currency="RUB")
    quote = {
        "action": "purchase",
        "product_name": "Старое имя тарифа",
        "period_days": 30,
        "payment_method": {"id": "sbp", "label": "СБП"},
        "bonus_amount": "2.00",
    }

    title, body = paid_staff_sale_content(account, order, quote)

    assert title == "Новая покупка!"
    assert "Покупатель: @test_buyer (ID: 202)" in body
    assert "Товар: Подписка: Старое имя тарифа (30 дн.)" in body
    assert "Метод: СБП" in body
    assert "Сумма: 1.00 RUB" in body
    assert "Бонусы: 2.00 RUB" in body
