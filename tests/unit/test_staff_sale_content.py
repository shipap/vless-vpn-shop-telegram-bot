from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sunrise_infrastructure.sale_notifications import paid_staff_sale_content, staff_sale_content


@pytest.mark.parametrize("formatter", [staff_sale_content, paid_staff_sale_content])
@pytest.mark.parametrize(
    ("action", "notice"),
    [
        ("upgrade", "Срок подписки сохранён."),
        ("devices", "Срок подписки сохранён."),
        ("downgrade", "Срок подписки пересчитан."),
    ],
)
def test_term_changes_do_not_present_catalog_month_as_new_paid_time(formatter, action, notice):
    account = SimpleNamespace(
        id=123, telegram_id=101001, telegram_username="test_buyer", email=None, email_verified=False
    )
    order = SimpleNamespace(
        id="order",
        subscription_id="sub",
        amount=Decimal("3.11"),
        currency="RUB",
        source="bot",
        fulfilled_at=datetime(2026, 10, 28, 6, tzinfo=UTC),
    )
    _, body = formatter(
        account,
        order,
        {
            "action": action,
            "product_name": "Старший тариф",
            "basis": {"period_days": 30},
            "starts_at": "2026-10-28T06:00:00+00:00",
            "resulting_expires_at": "2026-10-28T21:00:00+00:00",
            "payment_method": {"id": "sbp"},
        },
    )
    assert notice in body
    assert "30 дней" not in body and "(30 дн.)" not in body
    if action == "downgrade":
        assert "Срок подписки сохранён." not in body
    assert "3.11 RUB" in body


@pytest.mark.parametrize("formatter", [staff_sale_content, paid_staff_sale_content])
@pytest.mark.parametrize("action", ["purchase", "renew"])
@pytest.mark.parametrize(
    ("snapshot_period", "detailed_period", "compact_period"),
    [(None, "90 дней", "(90 дн.)"), (30, "30 дней", "(30 дн.)")],
)
def test_purchase_and_renewal_still_show_the_purchased_duration(
    formatter, action, snapshot_period, detailed_period, compact_period
):
    account = SimpleNamespace(
        id=123, telegram_id=101001, telegram_username="test_buyer", email=None, email_verified=False
    )
    order = SimpleNamespace(
        id="order",
        subscription_id="sub",
        amount=Decimal("123.45"),
        currency="RUB",
        source="site",
        fulfilled_at=None,
    )
    _, body = formatter(
        account,
        order,
        {
            "action": action,
            "product_name": "Тариф",
            "period_days": snapshot_period,
            "basis": {"period_days": 90},
        },
    )
    assert detailed_period in body if formatter is staff_sale_content else compact_period in body
    assert "Срок подписки сохранён." not in body
    assert "Срок подписки пересчитан." not in body


@pytest.mark.parametrize("formatter", [staff_sale_content, paid_staff_sale_content])
def test_unknown_action_does_not_claim_catalog_duration_or_a_preserved_term(formatter):
    account = SimpleNamespace(
        id=123, telegram_id=None, telegram_username=None, email=None, email_verified=False
    )
    order = SimpleNamespace(
        id="order",
        subscription_id="sub",
        amount=Decimal("0.00"),
        currency="RUB",
        source="site",
        fulfilled_at=None,
    )
    _, body = formatter(account, order, {"action": "future", "basis": {"period_days": 30}})
    assert "30 дней" not in body and "(30 дн.)" not in body
    assert "Срок подписки сохранён." not in body
    assert "Срок подписки пересчитан." not in body


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
