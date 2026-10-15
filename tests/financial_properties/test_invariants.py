"""Independent generated checks of value conservation and discount semantics."""

from decimal import Decimal as D

from hypothesis import given, settings
from hypothesis import strategies as st
from sunrise_core.pricing import calculate, downgrade_seconds, upgrade


@settings(max_examples=250, deadline=None)
@given(
    cents=st.integers(1, 100_000_000),
    period=st.integers(0, 100),
    secondary=st.lists(st.integers(0, 100), max_size=6),
    cap=st.integers(0, 100),
    bonus_cap=st.integers(0, 100),
    requested=st.integers(0, 100_000_000),
)
def test_quotes_conserve_money_and_never_exceed_bonus_budget(
    cents, period, secondary, cap, bonus_cap, requested
):
    base, budget = D(cents) / 100, D(requested) / 100
    result = calculate(base, D(period), list(map(D, secondary)), D(cap), D(bonus_cap), budget)
    assert result.bonus + result.external == result.discounted
    assert D(0) <= result.bonus <= budget
    assert result.bonus <= result.discounted * D(bonus_cap) / 100
    assert D(0) <= result.external <= result.discounted <= base
    assert result.discounted >= (base * (1 - D(cap) / 100)).quantize(D(".01"))


@settings(max_examples=200, deadline=None)
@given(seconds=st.integers(0, 10**9), low=st.integers(1, 1_000_000), delta=st.integers(1, 1_000_000))
def test_downgrade_preserves_value_without_creating_fractional_second_credit(seconds, low, delta):
    old, new = D(low + delta), D(low)
    converted = downgrade_seconds(old, new, seconds)
    assert D(converted) * new <= D(seconds) * old
    assert (D(converted) + 1) * new > D(seconds) * old


@settings(max_examples=200, deadline=None)
@given(seconds=st.integers(0, 31_536_000), period=st.integers(1, 31_536_000))
def test_upgrade_rounding_never_undercharges_and_is_less_than_one_cent(seconds, period):
    exact = D(150) * D(seconds) / D(period)
    charged = upgrade(D(99), D(249), seconds, period)
    assert exact <= charged < exact + D(".01")


@given(extra=st.integers(0, 20))
def test_weaker_secondary_promos_do_not_stack(extra):
    strongest = calculate(D(1000), D(20), [D(20)], D(40), D(50), D(0))
    attempted = calculate(D(1000), D(20), [D(20), D(extra)], D(40), D(50), D(0))
    assert attempted == strongest
