"""Краткое описание нового счёта из сохранённых условий заказа."""

import unicodedata
from html.parser import HTMLParser
from urllib.parse import urlsplit

from sunrise_core.telegram_content import render_plain

from .sale_notifications import staff_account_identity


def _single_line(value):
    class Text(HTMLParser):
        def handle_data(self, data):
            parts.append(data)

    parts = []
    parser = Text(convert_charrefs=True)
    parser.feed(str(value or ""))
    text = "".join(parts)
    return " ".join("".join(
        " " if unicodedata.category(char) in {"Cc", "Cf"} and char != "\u200d" else char
        for char in text
    ).split())


def payment_description(account, order, snapshot, config):
    action = {
        "purchase": "New", "renew": "Renew", "upgrade": "Upgrade",
        "downgrade": "Downgrade", "devices": "Devices",
    }.get(snapshot.get("action"), "Order")
    content = snapshot.get("product_content_name")
    name = _single_line(render_plain(content) if content else snapshot.get("product_name")) or "Тариф"
    days = snapshot.get("period_days") or (snapshot.get("basis") or {}).get("period_days")
    period = f" ({days}d)" if type(days) is int and days > 0 else ""
    instance = getattr(config, "instance", config)
    bot = _single_line(getattr(instance, "telegram_bot_username", None)) or "не настроен"
    source = ""
    if order.source == "site":
        host = urlsplit(getattr(instance, "dashboard_url", None) or "").hostname
        source = " Site: " + (_single_line(host)[:96] or "сайт")
    buyer = _single_line(staff_account_identity(account)).removeprefix("@")
    suffix = f"{period} Bot: {bot}{source} Buyer: {buyer}"
    # Локальный бюджет читаемости; RollyPay не документирует предел description.
    budget = max(1, 512 - len(action) - 2 - len(suffix))
    if len(name) > budget:
        name = name[:budget - 1].rstrip() + "…"
    return f"{action}: {name}{suffix}"
