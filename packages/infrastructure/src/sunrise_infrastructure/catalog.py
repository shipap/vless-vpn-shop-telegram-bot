"""Проверка и публичное представление редактируемых данных каталога."""

from decimal import Decimal, InvalidOperation

from sunrise_core.content import validate_content
from sunrise_core.devices import device_surcharge, validate_device_rules
from sunrise_core.errors import DomainError
from sunrise_core.pricing import money

PRESENTATION_FIELDS = {
    "semantic_role",
    "visible_landing",
    "visible_bot",
    "sort_order",
    "highlighted",
    "icon",
    "base_period_days",
    "periods",
    "eligibility",
    "recurring_plan_mapping",
    "extra_device_rules",
}


def validate_presentation(values):
    try:
        for key, value in values.items():
            if key not in PRESENTATION_FIELDS:
                raise ValueError("Неизвестное поле витрины")
            if key == "extra_device_rules":
                validate_device_rules(value)
            if key in {"visible_landing", "visible_bot", "highlighted"} and type(value) is not bool:
                raise ValueError("Ожидается переключатель")
            if key == "semantic_role" and (
                not isinstance(value, str)
                or not value
                or len(value) > 64
                or not value.replace("_", "").isalnum()
            ):
                raise ValueError("Проверьте роль тарифа")
            if key == "sort_order" and (type(value) is not int or not -10000 <= value <= 10000):
                raise ValueError("Проверьте порядок")
            if key == "base_period_days" and (type(value) is not int or not 1 <= value <= 730):
                raise ValueError("Проверьте базовый срок")
            if key == "icon" and value is not None:
                if (
                    not isinstance(value, dict)
                    or set(value) - {"fallback", "custom_emoji_id"}
                    or not isinstance(value.get("fallback"), str)
                    or not 1 <= len(value["fallback"]) <= 16
                ):
                    raise ValueError("Укажите резервный значок")
                eid = value.get("custom_emoji_id")
                if eid is not None and (
                    not isinstance(eid, str) or not eid.isascii() or not eid.isdigit() or len(eid) > 32
                ):
                    raise ValueError("ID эмодзи должен быть строкой цифр")
            if key == "periods":
                if not isinstance(value, list) or len(value) > 20:
                    raise ValueError("Проверьте сроки")
                seen = set()
                for period in value:
                    if (
                        set(period) != {"days", "months", "discount_percent"}
                        or type(period["days"]) is not int
                        or type(period["months"]) is not int
                        or not 1 <= period["days"] <= 730
                        or not 1 <= period["months"] <= 24
                        or period["days"] in seen
                        or not Decimal(period["discount_percent"]).is_finite()
                        or not 0 <= Decimal(period["discount_percent"]) < 100
                    ):
                        raise ValueError("Проверьте срок и скидку")
                    seen.add(period["days"])
            if key == "eligibility" and (
                not isinstance(value, dict)
                or set(value) != {"allow_upgrade", "allow_downgrade"}
                or any(type(v) is not bool for v in value.values())
            ):
                raise ValueError("Проверьте правила перехода")
            if key == "recurring_plan_mapping" and (
                not isinstance(value, dict)
                or len(value) > 20
                or any(
                    not str(k).isdigit() or not isinstance(v, str) or not 1 <= len(v) <= 128
                    for k, v in value.items()
                )
            ):
                raise ValueError("Проверьте планы автопродления")
    except (ValueError, TypeError, InvalidOperation, KeyError):
        raise DomainError(
            "catalog_invalid", "Проверьте поля тарифа, сроки, значки и правила перехода.", 400
        ) from None
    return values


def billing_months_dto(value):
    """Keep fractional historical billing terms exact at the response boundary."""
    months = Decimal(str(value))
    return int(months) if months == months.to_integral_value() else format(months, "f")


def period_dto(period):
    """Normalize response numbers without rewriting historical JSON or paid snapshots."""
    return period | {
        "months": billing_months_dto(period["months"]),
        "discount_percent": str(Decimal(str(period["discount_percent"]))),
    }


def presentation_dto(product, periods=()):
    value = product.presentation or {}
    effective_periods = value.get("periods") or periods
    monthly_days = {period["days"] for period in effective_periods if Decimal(str(period.get("months"))) == 1}
    base_period = value.get("base_period_days")
    if base_period is None and len(monthly_days) == 1:
        base_period = next(iter(monthly_days))
    return {
        "semantic_role": value.get("semantic_role", product.code),
        "visible_landing": value.get("visible_landing", True),
        "visible_bot": value.get("visible_bot", True),
        "sort_order": value.get("sort_order", product.tier_level),
        "highlighted": value.get("highlighted", False),
        "icon": value.get("icon"),
        "base_period_days": base_period,
        "periods": [
            period_dto(p)
            | {
                "price": str(
                    money(
                        product.price
                        * Decimal(str(p["months"]))
                        * (1 - Decimal(str(p["discount_percent"])) / 100)
                    )
                )
            }
            for p in effective_periods
        ],
        "eligibility": value.get("eligibility", {"allow_upgrade": True, "allow_downgrade": True}),
        "extra_device_rules": [],
    }


def product_device_terms(product):
    return {
        "included": product.included_devices,
        "price": str(product.extra_device_price) if product.extra_device_price is not None else None,
        "rules": [],
        "source": "catalog_v9_fixed_monthly",
    }


def product_device_surcharge(product, devices):
    terms = product_device_terms(product)
    try:
        return device_surcharge(max(0, devices - terms["included"]), terms["rules"], terms["price"])
    except ValueError:
        raise DomainError(
            "device_price_unknown", "Стоимость дополнительных устройств требует настройки владельцем.", 409
        ) from None


def validate_public_content(value):
    if (
        not isinstance(value, dict)
        or not {"faq", "how_to_connect"} <= set(value)
        or set(value)
        - {"faq", "how_to_connect", "catalog_description", "catalog_description_content", "promo_channels"}
    ):
        raise DomainError("content_invalid", "Укажите FAQ и шаги подключения.", 400)
    try:
        if "promo_channels" in value:
            from .instance_config import public_url

            channels = value["promo_channels"]
            if not isinstance(channels, list) or len(channels) > 10:
                raise ValueError("Не более десяти каналов")
            for channel in channels:
                if (
                    not isinstance(channel, dict)
                    or set(channel) != {"title", "url"}
                    or not isinstance(channel["title"], str)
                    or not 1 <= len(channel["title"].strip()) <= 120
                    or any(ord(char) < 32 for char in channel["title"])
                    or not isinstance(channel["url"], str)
                    or not channel["url"].startswith("https://")
                ):
                    raise ValueError("Проверьте название и HTTPS-ссылку канала")
                public_url(channel["url"])
        if "catalog_description" in value and (
            not isinstance(value["catalog_description"], str) or len(value["catalog_description"]) > 4000
        ):
            raise ValueError("Описание каталога: до 4000 символов")
        if "catalog_description_content" in value:
            validate_content(value["catalog_description_content"])
        for key, title, body in [("faq", "question", "answer"), ("how_to_connect", "title", "body")]:
            if not isinstance(value[key], list) or len(value[key]) > 30:
                raise ValueError("Слишком много элементов")
            seen = set()
            for item in value[key]:
                if (
                    set(item) != {"id", title, body}
                    or not isinstance(item["id"], str)
                    or not 1 <= len(item["id"]) <= 64
                    or item["id"] in seen
                    or not isinstance(item[title], str)
                    or not 1 <= len(item[title]) <= 300
                ):
                    raise ValueError("Проверьте текст")
                seen.add(item["id"])
                validate_content(item[body])
    except (ValueError, TypeError, KeyError):
        raise DomainError(
            "content_invalid", "Проверьте структуру и безопасное форматирование контента.", 400
        ) from None
    return value
