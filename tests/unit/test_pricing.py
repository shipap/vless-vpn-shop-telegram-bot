from decimal import Decimal as D

import pytest
from sunrise_core import pricing


def test_upgrade_half_month_costs_75_without_extending_time():
    assert pricing.upgrade(D(99), D(249), 1296000, 2592000) == D("75.00")


def test_downgrade_converts_from_change_time_and_floors_seconds():
    assert pricing.downgrade_seconds(D(249), D(99), 1296000) == 3259636


def test_sequential_discounts_and_bonus_are_separate():
    result = pricing.calculate(D(100), D(20), [D(20), D(10)], D(40), D(50), D(100))
    assert result.discounted == D("64.00")
    assert result.bonus == D("32.00")
    assert result.external == D("32.00")


def test_bonus_cannot_be_negative():
    with pytest.raises(ValueError):
        pricing.calculate(D(100), D(0), [], D(40), D(50), D(-1))


def test_roundtrip_tier_changes_cannot_grow_service_value():
    for seconds in [1, 17, 86400, 1296000, 31535999]:
        converted = pricing.downgrade_seconds(D(249), D(99), seconds)
        assert D(converted) * D(99) <= D(seconds) * D(249)


def test_period_365_days_is_not_360_days():
    assert pricing.period_price(D(99), 12, D(20)) == D("950.40")
