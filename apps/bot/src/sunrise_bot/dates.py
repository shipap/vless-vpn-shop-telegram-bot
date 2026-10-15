"""Russian presentation of verified timezone-aware timestamps."""

from datetime import UTC, datetime, timedelta, timezone

MOSCOW = timezone(timedelta(hours=3), "MSK")


def remaining_days(value, *, instant=None):
    try:
        expiry = (
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        )
        if expiry.tzinfo is None or expiry.utcoffset() is None:
            return "срок уточняется"
        seconds = (expiry - (instant or datetime.now(UTC))).total_seconds()
        if seconds <= 0:
            return "истекла"
        if seconds < 86400:
            return "меньше суток"
        days = int(seconds // 86400)
        singular = days % 10 == 1 and days % 100 != 11
        unit = "день" if singular else "дня" if 2 <= days % 10 <= 4 and not 12 <= days % 100 <= 14 else "дней"
        return f"{'остался' if singular else 'осталось'} {days} {unit}"
    except (ValueError, TypeError, OverflowError):
        return "срок уточняется"


def moscow_datetime(value):
    if not value:
        return "Уточняется"
    try:
        parsed = (
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        )
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return "Уточняется"
        return parsed.astimezone(MOSCOW).strftime("%d.%m.%Y %H:%M МСК")
    except (ValueError, TypeError, OverflowError):
        return "Уточняется"
