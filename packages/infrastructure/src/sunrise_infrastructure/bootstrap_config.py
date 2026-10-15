"""Проверяемые исходные данные для явной инициализации нового каталога."""

import json
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SeedProduct(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_-]+$")
    code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(max_length=5000)
    tier_level: int = Field(ge=0, le=1000)
    price: Decimal = Field(gt=0, max_digits=18, decimal_places=2)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    included_devices: int = Field(ge=1, le=10000)
    extra_device_price: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=2)
    presentation: dict = Field(default_factory=dict)
    content: dict = Field(default_factory=dict)
    available: bool = True

    @field_validator("presentation")
    @classmethod
    def valid_presentation(cls, value):
        from .catalog import validate_presentation

        return validate_presentation(value)


class SeedSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    recurring: dict
    periods: list[dict] = Field(min_length=1, max_length=100)
    discounts: dict

    @field_validator("recurring")
    @classmethod
    def recurring_off(cls, value):
        if value.get("enabled") is not False:
            raise ValueError("Начальная конфигурация не разрешает новые банковские согласия.")
        return value

    @field_validator("periods")
    @classmethod
    def valid_periods(cls, value):
        seen = set()
        normalized = []
        for row in value:
            days = row.get("days")
            if type(days) is not int or not 1 <= days <= 3660 or days in seen:
                raise ValueError("Нужны уникальные положительные сроки.")
            try:
                months = Decimal(str(row.get("months")))
                discount = Decimal(str(row.get("discount_percent")))
            except ArithmeticError:
                raise ValueError("Проверьте множитель цены и скидку периода.") from None
            if not months.is_finite() or months <= 0 or not discount.is_finite() or not 0 <= discount < 100:
                raise ValueError("Проверьте множитель цены и скидку периода.")
            seen.add(days)
            normalized.append(dict(row, months=str(months), discount_percent=str(discount)))
        return normalized

    @field_validator("discounts")
    @classmethod
    def valid_discounts(cls, value):
        value = value.copy()

        def percent(raw):
            try:
                amount = Decimal(str(raw))
            except ArithmeticError:
                raise ValueError("Нужен конечный процент от 0 до 100.") from None
            if not amount.is_finite() or not 0 <= amount <= 100:
                raise ValueError("Нужен конечный процент от 0 до 100.")
            return amount

        for key in ("cap", "bonus_cap"):
            value[key] = str(percent(value.get(key)))
        if "referral_percent" in value:
            value["referral_percent"] = str(percent(value["referral_percent"]))
        if not isinstance(value.get("secondary"), list) or len(value["secondary"]) > 100:
            raise ValueError("Нужен ограниченный список дополнительных скидок.")
        value["secondary"] = [str(percent(item)) for item in value["secondary"]]
        if "loyalty_period_days" in value or "loyalty_milestones" in value:
            period = value.get("loyalty_period_days")
            milestones = value.get("loyalty_milestones")
            if (
                type(period) is not int
                or not 1 <= period <= 3660
                or not isinstance(milestones, list)
                or len(milestones) > 100
            ):
                raise ValueError("Проверьте период и ступени лояльности.")
            seen = set()
            for row in milestones:
                elapsed = row.get("elapsed_periods") if isinstance(row, dict) else None
                if type(elapsed) is not int or not 1 <= elapsed <= 10000 or elapsed in seen:
                    raise ValueError("Нужны уникальные положительные ступени лояльности.")
                seen.add(elapsed)
                percent(row.get("percent"))
            ordered = sorted(milestones, key=lambda row: row["elapsed_periods"])
            if any(
                percent(a["percent"]) > percent(b["percent"])
                for a, b in zip(ordered, ordered[1:], strict=False)
            ):
                raise ValueError("Ступени лояльности не должны уменьшаться.")
            value["loyalty_milestones"] = [
                dict(row, percent=str(percent(row["percent"]))) for row in milestones
            ]
        return value


class BootstrapSeed(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    products: list[SeedProduct] = Field(min_length=1, max_length=1000)
    settings: SeedSettings

    @field_validator("products")
    @classmethod
    def unique_products(cls, value):
        if len({p.id for p in value}) != len(value) or len({p.code for p in value}) != len(value):
            raise ValueError("Ключи товаров должны быть уникальны.")
        return value


def load_bootstrap_seed(value=None):
    if value is None:
        source = Path(__file__).resolve().parents[4] / "config" / "bootstrap.example.json"
        value = json.loads(source.read_text(encoding="utf-8"))
    return BootstrapSeed.model_validate(value)


def initial_legal_documents(settings):
    """Пустой комплект остаётся OFF; частично заполненный комплект не применяется."""
    from .legal import validate_documents

    fields = (settings.legal_offer_url, settings.legal_offer_version,
              settings.legal_privacy_url, settings.legal_privacy_version)
    if not any(fields):
        return None
    return validate_documents([
        {"document_type": "offer", "url": fields[0], "version": fields[1]},
        {"document_type": "privacy", "url": fields[2], "version": fields[3]},
    ])


def bootstrap_instance(db, settings, seed=None):
    """Только явный bootstrap; каталог, документы и согласия владельца не перезаписываются."""
    from .commerce import Commerce, bootstrap

    documents = initial_legal_documents(settings)
    configured = load_bootstrap_seed(seed)
    bootstrap(db, seed=configured.model_dump(mode="json"))
    if documents:
        Commerce(db).legal.configure(
            None, documents, "Явная первичная настройка документов из конфигурации экземпляра.",
            only_if_missing=True,
        )


def apply_missing_policy_fields(db, seed=None):
    """Только явное обновление настроек: существующие решения владельца сохраняются."""
    from sqlalchemy import select

    from .db import Audit, Setting

    configured = load_bootstrap_seed(seed).settings.discounts
    with db.transaction() as session:
        row = session.scalar(select(Setting).where(Setting.key == "discounts").with_for_update())
        if row is None or not isinstance(row.value, dict):
            raise ValueError("Сначала настройте исходную политику оплаты.")
        added = {key: value for key, value in configured.items() if key not in row.value}
        if not added:
            return []
        row.value = row.value | added
        session.add(
            Audit(
                action="policy_seed_fields",
                target="discounts",
                reason="Явное заполнение отсутствующих полей из проверенного seed; существующие значения сохранены.",
                details={"added_keys": sorted(added)},
            )
        )
        return sorted(added)
