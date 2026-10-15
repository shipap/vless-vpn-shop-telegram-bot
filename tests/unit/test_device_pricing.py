from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st


def test_progressive_slot_boundaries_and_explicit_linear_fallback():
    from sunrise_core.devices import device_surcharge, validate_device_rules

    rules = validate_device_rules(
        [
            {"through_extra": 5, "price": "50.00"},
            {"through_extra": 9, "price": "100.00"},
            {"through_extra": None, "price": "150.00"},
        ]
    )
    assert [device_surcharge(n, rules) for n in (0, 5, 6, 9, 10)] == [
        Decimal(x) for x in (0, 250, 350, 650, 800)
    ]
    assert device_surcharge(10, [], "50.00") == Decimal("500.00")
    with pytest.raises(ValueError):
        device_surcharge(1, [], None)


@given(st.integers(min_value=0, max_value=80), st.integers(min_value=1, max_value=20))
def test_batch_slot_charge_equals_individual_slot_differences(before, count):
    from sunrise_core.devices import device_surcharge

    rules = [
        {"through_extra": 5, "price": "50.00"},
        {"through_extra": 9, "price": "100.00"},
        {"through_extra": None, "price": "150.00"},
    ]
    total = device_surcharge(before + count, rules) - device_surcharge(before, rules)
    assert total == sum(
        device_surcharge(n + 1, rules) - device_surcharge(n, rules) for n in range(before, before + count)
    )


@pytest.mark.parametrize(
    "rules",
    [
        [{"through_extra": 5, "price": "50.00"}],
        [{"through_extra": None, "price": "NaN"}],
        [
            {"through_extra": 5, "price": "50.00"},
            {"through_extra": 4, "price": "100.00"},
            {"through_extra": None, "price": "150.00"},
        ],
    ],
)
def test_invalid_device_policy_fails_closed(rules):
    from sunrise_core.devices import validate_device_rules

    with pytest.raises(ValueError):
        validate_device_rules(rules)
