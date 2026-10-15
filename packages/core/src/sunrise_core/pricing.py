from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
ZERO = Decimal(0)


def money(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("Некорректная сумма")
    return result.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Price:
    base: Decimal
    discounted: Decimal
    bonus: Decimal
    external: Decimal


def calculate(base, period_discount, secondary, cap, bonus_cap, requested_bonus, *, payment_discount=ZERO):
    values = [base, period_discount, cap, bonus_cap, requested_bonus, payment_discount, *secondary]
    if any(v < 0 or not v.is_finite() for v in values):
        raise ValueError("Некорректная сумма")
    if any(v > 100 for v in [period_discount, cap, bonus_cap, payment_discount, *secondary]):
        raise ValueError("Процент вне диапазона")
    discounted = max(
        base
        * (1 - period_discount / 100)
        * (1 - max(secondary, default=ZERO) / 100)
        * (1 - payment_discount / 100),
        base * (1 - cap / 100),
    )
    discounted = money(discounted)
    bonus = min(money(requested_bonus), (discounted * bonus_cap / 100).quantize(CENT, rounding=ROUND_FLOOR))
    return Price(money(base), discounted, bonus, money(discounted - bonus))


def period_price(monthly, months, discount):
    return money(monthly * months * (1 - discount / 100))


def upgrade(old_price, new_price, remaining_seconds, period_seconds):
    if old_price <= 0 or new_price < old_price or remaining_seconds < 0 or period_seconds <= 0:
        raise ValueError("Несопоставимые условия")
    return ((new_price - old_price) * Decimal(remaining_seconds) / Decimal(period_seconds)).quantize(
        CENT, rounding=ROUND_CEILING
    )


def downgrade_seconds(old_price, new_price, remaining_seconds):
    if new_price <= 0 or old_price <= new_price or remaining_seconds < 0:
        raise ValueError("Несопоставимые условия")
    return int((Decimal(remaining_seconds) * old_price / new_price).to_integral_value(rounding=ROUND_FLOOR))
