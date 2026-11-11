"""Ступенчатая скидка на ручное продление перед окончанием срока и текст напоминаний.

Настройки хранятся в общей настройке ``discounts``:

* ``near_expiry_renewal`` — ``{"enabled": bool, "percent": str}`` (существующая кампания);
* ``near_expiry_tiers`` — необязательный список ``[{"hours": int, "percent": str}, ...]``.

Без ступеней сохраняется прежнее поведение: одна скидка в последние 24 часа и
напоминания за 3 дня, за 1 день и после окончания. Со ступенями скидка зависит от
оставшегося времени в момент расчёта цены и действует только до окончания срока.
"""

from datetime import timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

MOSCOW = ZoneInfo("Europe/Moscow")
LEGACY_WINDOW = timedelta(days=3)
MAX_TIER_HOURS = 168


def _percent(value, *, allow_zero=False):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    low_ok = result >= 0 if allow_zero else result > 0
    return result if result.is_finite() and low_ok and result <= 100 else None


def near_expiry_tiers(policy):
    """Validated tiers ordered from the shortest window, or None when tiers are not configured."""
    raw = (policy or {}).get("near_expiry_tiers")
    if not isinstance(raw, list) or not raw:
        return None
    tiers = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"hours", "percent"}:
            return None
        hours, percent = item["hours"], _percent(item["percent"])
        if type(hours) is not int or not 1 <= hours <= MAX_TIER_HOURS or percent is None:
            return None
        tiers.append((hours, percent))
    if len({hours for hours, _ in tiers}) != len(tiers):
        return None
    return sorted(tiers)


def campaign_enabled(policy):
    entry = (policy or {}).get("near_expiry_renewal") or {}
    return isinstance(entry, dict) and entry.get("enabled") is True


def tiers_configured(policy):
    return "near_expiry_tiers" in (policy or {})


def near_expiry_percent(policy, remaining):
    """Discount percent (Decimal) for a manual renewal with ``remaining`` time left, else None."""
    if not campaign_enabled(policy):
        return None
    tiers = near_expiry_tiers(policy)
    if tiers is None:
        if tiers_configured(policy):
            # Configured but invalid tiers fail closed: no discount instead of the legacy rule.
            return None
        # Unchanged single-step campaign: the last 24 hours, including after the end of the term.
        percent = _percent(policy["near_expiry_renewal"].get("percent"), allow_zero=True)
        return percent if percent is not None and remaining < timedelta(hours=24) else None
    if remaining <= timedelta(0):
        return None
    for hours, percent in tiers:
        if remaining <= timedelta(hours=hours):
            return percent
    return None


def reminder_window(policy):
    tiers = near_expiry_tiers(policy) if campaign_enabled(policy) else None
    return timedelta(hours=tiers[-1][0]) if tiers else LEGACY_WINDOW


def reminder_bucket(policy, seconds):
    """Which reminder is due for ``seconds`` left; None outside every reminder window."""
    if seconds <= 0:
        return "expired" if seconds >= -86400 else None
    tiers = near_expiry_tiers(policy) if campaign_enabled(policy) else None
    if tiers is None:
        return "1d" if seconds <= 86400 else "3d" if seconds <= 259200 else None
    for hours, _ in tiers:
        if seconds <= hours * 3600:
            return f"{hours}h"
    return None


def bucket_is_current(policy, bucket, seconds):
    """A queued reminder is still true. Reminders queued before a policy change keep their rule."""
    if bucket in {"1d", "3d"}:
        current = "expired" if seconds <= 0 else "1d" if seconds <= 86400 else "3d" if seconds <= 259200 else None
        return current == bucket and seconds >= -86400
    return reminder_bucket(policy, seconds) == bucket


def moscow(value):
    return value.astimezone(MOSCOW).strftime("%d.%m.%Y в %H:%M")


def _money(total, currency):
    return f"{total} ₽" if currency == "RUB" else f"{total} {currency}"


def _format_percent(value):
    text = format(Decimal(value).normalize(), "f")
    return text


def _hours(count):
    if count % 10 == 1 and count % 100 != 11:
        word = "час"
    elif count % 10 in {2, 3, 4} and count % 100 not in {12, 13, 14}:
        word = "часа"
    else:
        word = "часов"
    return f"{count} {word}"


def _days(count):
    if count % 10 == 1 and count % 100 != 11:
        return f"{count} день"
    if count % 10 in {2, 3, 4} and count % 100 not in {12, 13, 14}:
        return f"{count} дня"
    return f"{count} дней"


def _price_line(quote, discounted):
    return (
        f"Продление на {_days(int(quote['period_days']))}: {_money(quote['total'], quote['currency'])}"
        + (" со скидкой." if discounted else ".")
    )


POLICY_PERCENT = object()


def reminder_content(policy, *, product_name, expiry, moment, quote=None, applied_percent=POLICY_PERCENT):
    """Short title and plain body; paragraphs are separated by a blank line.

    ``applied_percent`` is the near-expiry percent the actual quote applied (None: not applied).
    The scheduler always passes it, so the text never promises a discount the price lacks.
    Telegram adds premium emoji to the known line prefixes, e-mail renders the same text.
    """
    seconds = (expiry - moment).total_seconds()
    if seconds <= 0:
        lines = [
            f"Тариф: {product_name}",
            "",
            f"Срок подписки закончился {moscow(expiry)} (МСК). Продлите подписку, чтобы снова подключиться к VPN.",
        ]
        if quote:
            lines.extend(["", _price_line(quote, False)])
        return "Подписка завершилась", "\n".join(lines)
    lines = [
        f"Тариф: {product_name}",
        "",
        f"Продлите подписку до {moscow(expiry)} (МСК), чтобы не потерять доступ к VPN.",
    ]
    tiers = near_expiry_tiers(policy) if campaign_enabled(policy) else None
    percent = near_expiry_percent(policy, expiry - moment)
    if applied_percent is not POLICY_PERCENT and (applied_percent is None or percent is None or Decimal(applied_percent) != percent):
        percent = None
    if percent is not None:
        line = f"Сейчас скидка на продление — {_format_percent(percent)}%."
        if tiers:
            current = next(hours for hours, _ in tiers if seconds <= hours * 3600)
            shorter = [item for item in tiers if item[0] < current]
            if shorter:
                next_hours, next_percent = shorter[-1]
                left = max(1, round(seconds / 3600 - next_hours))
                line += f" Через {_hours(left)} она уменьшится до {_format_percent(next_percent)}%."
            else:
                line += " Она действует до окончания подписки."
        lines.extend(["", line])
    if quote:
        lines.extend(([] if percent is not None else [""]) + [_price_line(quote, percent is not None)])
    return "Срок подписки заканчивается", "\n".join(lines)


TELEGRAM_LINE_ICONS = (
    ("Тариф:", "subscription", "📁"),
    ("Продлите", "quote_expiry", "🕒"),
    ("Срок подписки закончился", "error_heading", "⚠️"),
    ("Сейчас скидка", "quote_discount", "🏷"),
    ("Продление на", "quote_price", "💵"),
)
