"""Текст для сотрудников по неизменяемым условиям выполненного заказа."""

import re
from datetime import timedelta, timezone
from decimal import Decimal, InvalidOperation


def _field(value, fallback="не указано"):
    if value is None:
        return fallback
    return " ".join(str(value).split()) or fallback


def staff_account_identity(account):
    """Bound Telegram identity first; only verified email can identify an unlinked account."""
    telegram_id = getattr(account, "telegram_id", None)
    if telegram_id is not None:
        username = (getattr(account, "telegram_username", None) or "").removeprefix("@")
        return (
            f"@{username} (ID: {telegram_id})"
            if re.fullmatch(r"[A-Za-z0-9_]{5,32}", username)
            else f"ID: {telegram_id}"
        )
    email = getattr(account, "email", None)
    if email and getattr(account, "email_verified", False):
        return _field(email)
    return "Telegram не привязан"


def staff_sale_content(account, order, snapshot):
    action = snapshot.get("action")
    titles = {
        "purchase": "Покупка подписки",
        "renew": "Автопродление подписки" if order.source == "recurring" else "Продление подписки",
        "upgrade": "Повышение тарифа",
        "downgrade": "Понижение тарифа",
        "devices": "Покупка дополнительных устройств",
    }
    title = titles.get(action, "Операция с подпиской")
    if account.telegram_id is None:
        telegram = "не привязан"
    else:
        username = (account.telegram_username or "").removeprefix("@")
        telegram = (
            f"@{username} (ID {account.telegram_id})"
            if re.fullmatch(r"[A-Za-z0-9_]{5,32}", username)
            else f"ID {account.telegram_id} (имя не указано)"
        )
    basis = snapshot.get("basis") or {}
    days = snapshot.get("period_days") or basis.get("period_days")
    period = f"{days} дней" if type(days) is int and days > 0 else "не указан в снимке заказа"
    fulfilled = order.fulfilled_at
    timestamp = (
        fulfilled.astimezone(timezone(timedelta(hours=3))).strftime("%d.%m.%Y %H:%M:%S") + " МСК"
        if fulfilled is not None and fulfilled.tzinfo is not None
        else "неизвестно"
    )
    lines = [
        f"Аккаунт: {account.id}",
        f"Telegram: {telegram}",
    ]
    if account.email:
        verified = "" if account.email_verified else " (не подтверждён)"
        lines.append("Email: " + _field(account.email) + verified)
    lines.extend(
        [
            "Тариф: " + _field(snapshot.get("product_name"), "название отсутствует в снимке заказа"),
            "Период: " + period,
            "Устройств: " + _field(snapshot.get("devices_limit"), "не указано в снимке заказа"),
            f"Сумма заказа: {order.amount:.2f} {_field(order.currency)}",
            "Заказ: " + _field(order.id),
            "Подписка: " + _field(order.subscription_id),
            "Выполнено: " + timestamp,
        ]
    )
    if action not in titles:
        lines.append("Тип операции: неизвестен")
    # Поля остаются текстом: общий транспорт экранирует HTML для Telegram.
    return title, "\n".join(lines)


def paid_staff_sale_content(account, order, snapshot):
    """Краткая карточка покупки из неизменяемого снимка оплаченного заказа."""
    identity = staff_account_identity(account)
    action = {
        "purchase": "Подписка",
        "renew": "Автопродление" if getattr(order, "source", None) == "recurring" else "Продление",
        "upgrade": "Повышение тарифа",
        "downgrade": "Понижение тарифа",
        "devices": "Дополнительные устройства",
    }.get(snapshot.get("action"), "Заказ")
    name = _field(snapshot.get("product_name"), "название отсутствует в снимке заказа")
    days = snapshot.get("period_days") or (snapshot.get("basis") or {}).get("period_days")
    period = f" ({days} дн.)" if type(days) is int and days > 0 else ""
    method = (snapshot.get("payment_method") or {}).get("id")
    label = {"sbp": "СБП", "crypto": "Криптовалюта", "xrocket": "xRocket"}.get(
        method, "не указан в снимке заказа"
    )
    lines = [
        "Покупатель: " + identity,
        f"Товар: {action}: {name}{period}",
        "Метод: " + label,
        f"Сумма: {order.amount:.2f} {_field(order.currency)}",
    ]
    try:
        bonus = Decimal(str(snapshot.get("bonus_amount", "0")))
    except (InvalidOperation, TypeError, ValueError):
        bonus = Decimal(0)
    if bonus > 0:
        lines.append(f"Бонусы: {bonus:.2f} RUB")
    try:
        promo_discount = Decimal(str(snapshot.get("promo_discount_amount", "0")))
        if (snapshot.get("selected_secondary") or {}).get("source") == "promo":
            promo_discount += Decimal(str(snapshot.get("secondary_discount_amount", "0")))
    except (InvalidOperation, TypeError, ValueError):
        promo_discount = Decimal(0)
    if promo_discount.is_finite() and promo_discount > 0:
        lines.append(f"Промокод {_field(snapshot.get('promo_code'), 'применён')}: {promo_discount:.2f} RUB")
    return "Новая покупка!", "\n".join(lines)
