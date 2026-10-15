"""Офлайн перенос в расширение схемы. Ни одного сетевого провайдера/worker здесь нет."""

import json
import re
from datetime import UTC
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import MetaData, Table, select, text

from .db import Account, LegacyIssue, LegacyPurchase, Product, Setting, Subscription


def legacy_device_limit(row, field):
    # Archived legacy items/subscriptions DDL lacks these columns. Preserve the
    # previous offline import interpretation; this is never a new runtime policy.
    if field not in row:
        return 4
    value = row[field]
    if type(value) is not int or value <= 0:
        raise ValueError("Некорректный исторический лимит устройств")
    return value


def legacy_currency(row):
    # Historical ruble contract only when the source schema had no currency.
    if "currency" not in row:
        return "RUB"
    value = row["currency"]
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z]{3}", value):
        raise ValueError("Некорректная историческая валюта")
    return value


def normalize_legacy_money(value):
    original = Decimal(str(value or 0))
    if not original.is_finite():
        raise ValueError("Некорректная историческая сумма")
    converted = original.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    return converted, converted - original


def resolve_squad(column_value, item_data):
    data = json.loads(item_data) if isinstance(item_data, str) and item_data else (item_data or {})
    embedded = data.get("squad_uuid") if isinstance(data, dict) else None
    if column_value and embedded and column_value != embedded:
        raise ValueError("Конфликт источников внутренней группы")
    return column_value or embedded


def legacy_account_values(row):
    bonus, bonus_delta = normalize_legacy_money(row["referral_balance"])
    credit, credit_delta = normalize_legacy_money(row["balance"])
    return (
        {
            "id": row["user_id"],
            "email": row["email"].strip().lower() if row["email"] else None,
            "email_verified": False,
            "telegram_id": row["telegram_id"],
            "password_hash": row["hashed_password"],
            "bonus_balance": bonus,
            "credit_balance": credit,
            "referrer_id": row["referrer_id"],
            "legacy_consent": {k: row[k] for k in ("tos", "tos_accepted") if k in row},
        },
        bonus_delta,
        credit_delta,
    )


def rehearse_import(db):
    """Только private-copy: исходные таблицы остаются единственным полным архивом фактов.

    Не конвертируем неоднозначные даты/историю оплат в выдуманные текущие права.
    Повтор запуска с уже заполненным расширением отклоняется.
    """
    if db.engine.url.host not in {"127.0.0.1", "localhost"} or db.engine.url.database != "sunrise_legacy":
        raise ValueError("Импорт разрешён только в изолированной sunrise_legacy.")
    metadata = MetaData()
    tables = {
        name: Table(name, metadata, autoload_with=db.engine)
        for name in ("users", "items", "subscriptions", "purchases")
    }
    totals = {
        "accounts": 0,
        "products": 0,
        "subscriptions": 0,
        "issues": 0,
        "rounded_fields": 0,
        "bonus_delta": "0",
        "credit_delta": "0",
        "legacy_tables_unchanged": True,
    }
    with db.transaction() as s:
        if s.scalar(select(Account.id).limit(1)):
            raise ValueError("Расширение уже заполнено; повторный импорт запрещён.")
        bonus_delta = Decimal(0)
        credit_delta = Decimal(0)
        user_ids = set()
        for row in s.execute(select(tables["users"])).mappings():
            values, bd, cd = legacy_account_values(row)
            bonus_delta += bd
            credit_delta += cd
            totals["rounded_fields"] += int(bd != 0) + int(cd != 0)
            s.add(Account(**values))
            user_ids.add(row["user_id"])
            totals["accounts"] += 1
        s.flush()
        # Sequence is advanced under the one-off import transaction; runtime never uses MAX+1.
        s.execute(
            text(
                "SELECT setval(pg_get_serial_sequence('v5_accounts','id'), GREATEST((SELECT max(id) FROM v5_accounts), 1), true)"
            )
        )
        product_ids = set()
        for index, row in enumerate(
            s.execute(select(tables["items"]).order_by(tables["items"].c.id)).mappings(), start=1
        ):
            try:
                squad = resolve_squad(row["squad_uuid"], row["item_data"])
            except (ValueError, TypeError):
                squad = row["squad_uuid"]
                s.add(LegacyIssue(source_table="items", source_id=str(row["id"]), reason="squad_conflict"))
                totals["issues"] += 1
            price, _ = normalize_legacy_money(row["price"])
            if price <= 0:
                s.add(
                    LegacyIssue(
                        source_table="items", source_id=str(row["id"]), reason="nonpositive_legacy_price"
                    )
                )
                totals["issues"] += 1
                continue
            pid = str(row["id"])
            product_ids.add(pid)
            s.add(
                Product(
                    id=pid,
                    code="legacy_" + pid,
                    name=row["name"],
                    description=row["description"] or "",
                    tier_level=index,
                    price=price,
                    currency=legacy_currency(row),
                    included_devices=legacy_device_limit(row, "included_devices"),
                    internal_squad_uuid=squad,
                    available=False,
                    content={"legacy_item_data_preserved_in": "public.items"},
                )
            )
            totals["products"] += 1
        s.flush()
        for row in s.execute(select(tables["subscriptions"])).mappings():
            sid = str(row["id"])
            if row["user_id"] not in user_ids:
                s.add(LegacyIssue(source_table="subscriptions", source_id=sid, reason="orphan_owner"))
                totals["issues"] += 1
                continue
            # Preserve original UUIDs and ID; raw expiry remains in untouched source row.
            s.add(
                Subscription(
                    id=sid,
                    account_id=row["user_id"],
                    product_id=str(row["item_id"]) if str(row["item_id"]) in product_ids else None,
                    name=row["item_name"] or "Историческая подписка",
                    status="legacy_review",
                    expires_at=None,
                    devices_limit=legacy_device_limit(row, "devices_limit"),
                    panel_uuid=row["remnawave_uuid"] or None,
                    short_uuid=row["short_uuid"] or None,
                    environment="private_copy",
                )
            )
            reason = "expiry_unknown" if row["expire_at"] is None else "expiry_timezone_and_value_review"
            s.add(LegacyIssue(source_table="subscriptions", source_id=sid, reason=reason))
            totals["issues"] += 1
            totals["subscriptions"] += 1
        for row in s.execute(select(tables["purchases"].c.id, tables["purchases"].c.user_id)).mappings():
            if row["user_id"] not in user_ids:
                s.add(LegacyIssue(source_table="purchases", source_id=str(row["id"]), reason="orphan_owner"))
                totals["issues"] += 1
        totals["bonus_delta"] = str(bonus_delta)
        totals["credit_delta"] = str(credit_delta)
        s.add(
            Setting(
                key="legacy_import",
                value={"mode": "offline_rehearsal", "runnable": False, "source_tables_retained": True},
            )
        )
    return totals


def finalize_verified_legacy_fields(db):
    """Поле expire_at подтверждено UTC setter-ами обоих приложений; остальные naive даты не угадываем."""
    if db.engine.url.host not in {"127.0.0.1", "localhost"} or db.engine.url.database != "sunrise_legacy":
        raise ValueError("Операция разрешена только в закрытой локальной копии.")
    metadata = MetaData()
    subscriptions = Table("subscriptions", metadata, autoload_with=db.engine)
    purchases = Table("purchases", metadata, autoload_with=db.engine)
    result = {
        "known_expiry_preserved": 0,
        "unknown_expiry_preserved": 0,
        "purchases_preserved": 0,
        "orphan_purchases_preserved": 0,
        "purchase_rounding_delta": "0",
    }
    with db.transaction() as s:
        user_ids = set(s.scalars(select(Account.id)))
        for row in s.execute(select(subscriptions)).mappings():
            target = s.get(Subscription, str(row["id"]))
            if not target:
                continue
            if row["expire_at"] is None:
                result["unknown_expiry_preserved"] += 1
                continue
            target.expires_at = row["expire_at"].replace(tzinfo=UTC)
            target.status = row["status"] or "legacy_preserved"
            result["known_expiry_preserved"] += 1
        delta = Decimal(0)
        for row in s.execute(select(purchases)).mappings():
            pid = str(row["id"])
            if s.get(LegacyPurchase, pid):
                continue
            amount, difference = normalize_legacy_money(row["price"])
            delta += difference
            owner = row["user_id"] if row["user_id"] in user_ids else None
            s.add(
                LegacyPurchase(
                    id=pid,
                    account_id=owner,
                    original_account_id=row["user_id"],
                    amount=amount,
                    status=row["status"] or "unknown",
                    item_name=row["item_name"] or "",
                    raw_date=row["date"].isoformat() if row["date"] else None,
                    provider=row["provider_id"] or row["payment_method"],
                    invoice_id=row["invoice_id"],
                )
            )
            result["purchases_preserved"] += 1
            result["orphan_purchases_preserved"] += owner is None
        result["purchase_rounding_delta"] = str(delta)
        s.add(
            Setting(
                key="legacy_expiry_provenance",
                value={
                    "field": "subscriptions.expire_at",
                    "timezone": "UTC",
                    "evidence": [
                        "site app/services/manual_operations.py utc(...).replace(tzinfo=None)",
                        "bot remnawave.py aware UTC expiry",
                    ],
                    "other_naive_dates": "preserved_without_timezone_assumption",
                    "legacy_payment_fees": "unknown",
                },
            )
        )
    return result
