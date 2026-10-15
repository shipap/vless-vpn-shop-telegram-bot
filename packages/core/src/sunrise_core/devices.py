"""Точная накопительная стоимость дополнительных слотов; численные условия приходят из каталога."""

import re
from decimal import Decimal


def validate_device_rules(rules):
    if rules is None:
        return []
    if not isinstance(rules, list) or len(rules) > 20:
        raise ValueError("Некорректные ступени цены устройств")
    result, previous = [], 0
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict) or set(rule) != {"through_extra", "price"}:
            raise ValueError("Некорректные поля ступени")
        through, price = rule["through_extra"], rule["price"]
        if through is None:
            if index != len(rules) - 1:
                raise ValueError("Неограниченная ступень должна быть последней")
        elif type(through) is not int or not previous < through <= 100 or index == len(rules) - 1:
            raise ValueError("Границы должны возрастать и завершаться неограниченной ступенью")
        if (
            not isinstance(price, str)
            or not re.fullmatch(r"[0-9]{1,8}\.[0-9]{2}", price)
            or Decimal(price) <= 0
        ):
            raise ValueError("Цена должна быть положительной строкой с двумя знаками")
        result.append({"through_extra": through, "price": price})
        previous = through
    return result


def device_surcharge(extra_devices, rules=None, linear_price=None):
    if type(extra_devices) is not int or not 0 <= extra_devices <= 100:
        raise ValueError("Некорректное количество дополнительных устройств")
    rules = validate_device_rules(rules)
    if extra_devices == 0:
        return Decimal("0.00")
    if not rules:
        if linear_price is None:
            raise ValueError("Стоимость устройств неизвестна")
        price = Decimal(str(linear_price))
        if not price.is_finite() or price <= 0 or price != price.quantize(Decimal("0.01")):
            raise ValueError("Некорректная линейная цена")
        return price * extra_devices
    total, consumed = Decimal("0.00"), 0
    for rule in rules:
        boundary = rule["through_extra"] if rule["through_extra"] is not None else extra_devices
        count = max(0, min(extra_devices, boundary) - consumed)
        total += Decimal(rule["price"]) * count
        consumed += count
        if consumed == extra_devices:
            break
    return total
