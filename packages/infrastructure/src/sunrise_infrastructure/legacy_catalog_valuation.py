"""Явная оценка перенесённого доступа; она не является прежней оплатой."""

import hashlib
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import or_, select
from sunrise_core.errors import DomainError

from .catalog import presentation_dto, product_device_surcharge, product_device_terms
from .db import Order, Payment, Product, Quote, Segment, Setting
from .panel import PanelReview
from .panel_v3 import instant


def catalog_snapshot(session, currency):
    """Pin both versions and relevant values, including non-versioned period settings."""
    periods = session.get(Setting, "periods")
    mapping = session.get(Setting, "panel_mapping")
    return {
        "periods": deepcopy(periods.value if periods else []),
        "panel_mapping": deepcopy(mapping.value if mapping else {}),
        "products": {
            item.id: {
                "version": item.version,
                "price": str(item.price),
                "currency": item.currency,
                "available": item.available,
                "tier_level": item.tier_level,
                "device_terms": product_device_terms(item),
                "presentation": deepcopy(item.presentation),
                "internal_squad_uuid": item.internal_squad_uuid,
            }
            for item in session.scalars(select(Product).where(Product.currency == currency))
        },
    }


def catalog_projection(session, sub, moment):
    """Only a positively proven import with no past financial/gift segment may bootstrap."""
    from . import panel_entitlements as entitlements

    marker = entitlements.imported_baseline(session, sub, moment)
    if not marker or session.scalar(
        select(Segment.id).where(Segment.subscription_id == sub.id, Segment.starts_at <= moment).limit(1)
    ):
        return None
    paid_fact = (
        select(Payment.id)
        .where(
            Payment.order_id == Order.id,
            Payment.environment == sub.environment,
            Payment.status.in_(["paid", "partially_refunded", "refunded"]),
        )
        .exists()
    )
    for order in session.scalars(
        select(Order).where(
            Order.subscription_id == sub.id,
            Order.environment == sub.environment,
            or_(Order.fulfilled_at.is_not(None), paid_fact),
        )
    ):
        quote = session.get(Quote, order.quote_id)
        snapshot = quote.snapshot if quote and isinstance(quote.snapshot, dict) else {}
        future = session.scalar(select(Segment).where(Segment.order_id == order.id))
        try:
            intact_future = (
                order.status == "paid"
                and order.fulfilled_at is not None
                and order.account_id == sub.account_id
                and quote is not None
                and quote.subscription_id == sub.id
                and quote.account_id == order.account_id
                and snapshot.get("action") in {"purchase", "renew"}
                and future is not None
                and future.subscription_id == sub.id
                and future.kind == "paid"
                and future.starts_at > moment
                and future.starts_at == instant(snapshot.get("starts_at"))
                and future.ends_at == instant(snapshot.get("resulting_expires_at"))
                and future.price_basis.get("currency") == order.currency
                and future.price_basis.get("products")
            )
        except (PanelReview, TypeError, AttributeError):
            intact_future = False
        if not intact_future:
            raise DomainError(
                "valuation_unknown", "Оплаченные условия новой операции требуют восстановления.", 409
            )
    expiry = instant(marker["expires_at"])
    if expiry <= moment:
        raise DomainError("no_paid_segment", "Нет действующего периода доступа.", 409)
    source = session.get(Product, marker.get("panel_product_id"))
    devices = marker.get("devices_limit")
    if not source or type(devices) is not int or not 1 <= devices <= 100:
        return None
    snapshot = catalog_snapshot(session, source.currency)
    source_days = presentation_dto(source, snapshot["periods"])["base_period_days"]
    if type(source_days) is not int or source_days <= 0:
        raise DomainError("valuation_unknown", "Настройте базовый срок тарифа в каталоге.", 409)
    products, terms, mappings, device_factors = {}, {}, {}, {}
    for item in session.scalars(
        select(Product).where(Product.currency == source.currency, Product.available.is_(True))
    ):
        days = presentation_dto(item, snapshot["periods"])["base_period_days"]
        if type(days) is not int or days <= 0:
            continue
        try:
            value = (
                (item.price + product_device_surcharge(item, devices)) * Decimal(source_days) / Decimal(days)
            )
        except DomainError:
            continue
        if value.is_finite() and value > 0:
            products[item.id] = str(value)
            terms[item.id] = product_device_terms(item)
            device_factors[item.id] = str(Decimal(source_days) / Decimal(days))
            mappings[item.id] = item.internal_squad_uuid
    if source.id not in products:
        raise DomainError(
            "valuation_unknown", "Стоимость перенесённого тарифа требует настройки каталога.", 409
        )
    future = [
        item for item in entitlements.remaining_segments(session, sub, moment) if item.starts_at > moment
    ]
    if any(item.kind != "paid" for item in future):
        raise DomainError(
            "valuation_unknown", "Подарочные периоды требуют отдельного расчёта владельцем.", 409
        )
    if future and expiry > future[0].starts_at:
        # An applied own renewal may publish the aggregate expiry. Value the legacy slice only once.
        applied = entitlements.observed_target(session, sub)
        horizon = max(item.ends_at for item in future)
        if (
            not applied
            or applied.get("target_origin") != "paid"
            or applied.get("version") not in {sub.version, sub.version - 1}
            or abs((expiry - horizon).total_seconds()) > 0.001
            or instant(applied["expires_at"]) != horizon
            or applied.get("panel_binding") != entitlements.registered_binding(session, sub)
        ):
            raise DomainError("valuation_unknown", "Срок панели пересекает будущий оплаченный период.", 409)
        expiry = future[0].starts_at
    segment_id = "legacy_catalog:" + hashlib.sha256(sub.id.encode()).hexdigest()[:48]
    return SimpleNamespace(
        id=segment_id,
        order_id=None,
        kind="legacy_catalog",
        starts_at=moment,
        ends_at=expiry,
        devices=devices,
        product_id=source.id,
        price_basis={
            "currency": source.currency,
            "period_days": source_days,
            "months": 1,
            "device_price_factor": "1",
            "device_pricing_included": True,
            "products": products,
            "device_terms": terms,
            "device_period_factors": device_factors,
            "panel_mappings": mappings,
            "panel_configuration": snapshot["panel_mapping"],
            "legacy_catalog": {
                "origin": "legacy_catalog",
                "pending": True,
                "subscription_id": sub.id,
                "environment": sub.environment,
                "binding": entitlements.registered_binding(session, sub),
                "starts_at": moment.isoformat(),
                "expires_at": expiry.isoformat(),
                "product_id": source.id,
                "devices": devices,
                "catalog": snapshot,
            },
        },
    )


def supported(segment):
    return segment.kind == "paid" or (
        segment.kind == "legacy_catalog"
        and isinstance(segment.price_basis, dict)
        and isinstance(segment.price_basis.get("legacy_catalog"), dict)
        and segment.price_basis["legacy_catalog"].get("origin") == "legacy_catalog"
        and segment.order_id is None
    )


def require_current_catalog(session, basis):
    provenance = basis.get("legacy_catalog") or {}
    if provenance.get("pending") and provenance.get("catalog") != catalog_snapshot(
        session, basis["currency"]
    ):
        raise DomainError("quote_stale", "Каталог изменился. Рассчитайте цену ещё раз.", 409)


def require_accepted_import(session, sub, basis):
    """A paid conflict must be retained for reconciliation before consuming reservations."""
    from . import panel_entitlements as entitlements

    provenance = basis.get("legacy_catalog") or {}
    if not provenance.get("pending"):
        return
    if (
        provenance.get("origin") != "legacy_catalog"
        or provenance.get("subscription_id") != sub.id
        or provenance.get("environment") != sub.environment
        or provenance.get("binding") != entitlements.registered_binding(session, sub)
        or entitlements.imported_baseline(session, sub) is None
        or session.get(Segment, basis["segment_id"]) is not None
    ):
        raise DomainError("valuation_unknown", "Основание перенесённой подписки изменилось.", 409)


def materialize(session, sub, basis):
    """Caller owns account/subscription locks and checked the accepted order version."""
    provenance = basis.get("legacy_catalog") or {}
    if not provenance.get("pending"):
        return session.get(Segment, basis["segment_id"])
    require_accepted_import(session, sub, basis)
    segment = Segment(
        id=basis["segment_id"],
        subscription_id=sub.id,
        order_id=None,
        product_id=provenance["product_id"],
        starts_at=instant(provenance["starts_at"]),
        ends_at=instant(provenance["expires_at"]),
        devices=provenance["devices"],
        kind="legacy_catalog",
        paid_value=Decimal(0),
        price_basis={},
    )
    session.add(segment)
    session.flush()
    return segment


def accepted_basis(basis):
    provenance = basis.get("legacy_catalog")
    return basis | {"legacy_catalog": provenance | {"pending": False}} if provenance else basis
