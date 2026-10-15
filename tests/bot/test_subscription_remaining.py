from datetime import UTC, datetime, timedelta

import pytest
from sunrise_bot.dates import remaining_days


@pytest.mark.parametrize(
    "hours,label",
    [
        (48, "осталось 2 дня"),
        (120, "осталось 5 дней"),
        (504, "остался 21 день"),
        (3, "меньше суток"),
        (0, "истекла"),
        (-1, "истекла"),
    ],
)
def test_remaining_days_preserves_less_than_day_and_expired(hours, label):
    instant = datetime(2026, 10, 13, tzinfo=UTC)
    assert remaining_days((instant + timedelta(hours=hours)).isoformat(), instant=instant) == label


def test_unknown_remaining_is_not_guessed():
    assert remaining_days(None) == "срок уточняется"
    assert remaining_days("2026-10-13T00:00:00") == "срок уточняется"
