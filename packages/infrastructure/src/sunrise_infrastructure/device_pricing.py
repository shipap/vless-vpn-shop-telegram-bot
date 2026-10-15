"""Фиксированная покупка устройств с сохранением стоимости оплаченных сегментов."""

from decimal import ROUND_FLOOR
from decimal import Decimal as D

from sunrise_core.devices import device_surcharge
from sunrise_core.errors import DomainError
from sunrise_core.pricing import money


def _saved_device_delta(basis, product_id, devices, extra_devices):
    terms = basis.get("device_terms", {}).get(product_id)
    if not terms or type(terms.get("included")) is not int:
        raise ValueError("unknown device terms")
    before = max(0, devices - terms["included"])
    after = max(0, devices + extra_devices - terms["included"])
    return device_surcharge(after, terms.get("rules"), terms.get("price")) - device_surcharge(
        before, terms.get("rules"), terms.get("price")
    )


def fixed_remaining_terms(segments, moment, product, extra_devices):
    price = product.extra_device_price
    if price is None or product.currency != "RUB" or price <= 0:
        raise DomainError("device_price_unknown", "Настройте рублёвую цену дополнительного устройства.", 409)
    charge = money(price * extra_devices)
    durations = [
        max(D(0), D(str((seg.ends_at - max(moment, seg.starts_at)).total_seconds()))) for seg in segments
    ]
    total = sum(durations)
    if not total:
        raise DomainError("no_paid_segment", "Нет действующего оплаченного периода.", 409)
    adjustments, allocated = [], D(0)
    for index, (segment, duration) in enumerate(zip(segments, durations, strict=True)):
        old = {k: v for k, v in segment.price_basis.items() if k != "device_segment_adjustments"}
        if (
            segment.kind != "paid"
            or old.get("currency") != product.currency
            or not old.get("products")
            or not old.get("period_days")
            or segment.devices + extra_devices > 100
        ):
            raise DomainError(
                "valuation_unknown",
                "Стоимость оставшихся оплаченных периодов требует проверки владельцем.",
                409,
            )
        portion = (
            charge - allocated
            if index == len(segments) - 1
            else (charge * duration / total).quantize(D(".01"), rounding=ROUND_FLOOR)
        )
        allocated += portion
        # Full-period price basis represents only remaining value. Normalize the fixed
        # contribution by its actual paid time instead of treating 50 as a monthly price.
        increment = portion * D(old["period_days"] * 86400) / duration if duration else D(0)

        try:
            source_delta = _saved_device_delta(old, segment.product_id, segment.devices, extra_devices)
            if source_delta <= 0:
                raise ValueError("unknown source device value")
        except (ValueError, TypeError, KeyError):
            raise DomainError(
                "valuation_unknown",
                "Сопоставимая стоимость ранее оплаченных устройств требует проверки владельцем.",
                409,
            ) from None
        products = {}
        for pid, value in old["products"].items():
            try:
                target_delta = _saved_device_delta(old, pid, segment.devices, extra_devices)
            except (ValueError, TypeError, KeyError):
                # Unknown comparable tiers remain unavailable for conversion; they
                # cannot manufacture free device value or block the known source tier.
                continue
            products[pid] = str(D(value) + increment * target_delta / source_delta)
        basis = old | {
            "products": products,
            "device_pricing_included": True,
            "device_adjustments": list(old.get("device_adjustments", []))
            + [
                {
                    "pricing": "fixed_remaining_term_v9",
                    "before_devices": segment.devices,
                    "after_devices": segment.devices + extra_devices,
                    "charge": str(portion),
                    "period_charge": str(charge),
                    "unit_price": str(price),
                    "remaining_seconds": str(duration),
                }
            ],
        }
        adjustments.append(
            {
                "segment_id": segment.id,
                "devices": segment.devices + extra_devices,
                "charge": str(portion),
                "basis": basis,
            }
        )
    return charge, adjustments
