"""Ограниченное восстановление счёта после потери ответа создания; только GET."""

from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sunrise_core.errors import DomainError

from .db import Inbox, Order, Payment, ProviderOperation, Setting, now, uid
from .panel_entitlements import owned_account
from .providers import RollyPay


def _binding(session, billing, order, operation):
    request = operation.request
    try:
        eligible = (
            order.environment == "live"
            and order.status == "awaiting_payment"
            and operation.terminal == billing.terminal
            and operation.order_id == order.id
            and operation.state == "outcome_unknown"
            and operation.provider_id is None
            and isinstance(request, dict)
            and request.get("scope_id") == billing.runtime_policy.scope.scope_id
            and request.get("environment") == "live"
            and request.get("provider") == "rollypay"
            and request.get("terminal") == billing.terminal
            and request.get("order_id") == order.id
            and request.get("currency") == order.currency
            and Decimal(request["amount"]) == order.amount
            and request.get("payment_method") in {"crypto", "xrocket", "sbp"}
            and owned_account(session, order, request.get("account_id"))
            and session.scalar(select(Payment.id).where(Payment.order_id == order.id).limit(1)) is None
        )
    except (KeyError, TypeError, ValueError, ArithmeticError):
        eligible = False
    if not eligible:
        return None
    return {
        "operation_id": operation.id,
        "order_id": order.id,
        "account_id": str(order.account_id),
        "amount": str(order.amount),
        "currency": order.currency,
        "request": dict(request),
    }


def _receipt_key(operation_id):
    return "unknown-invoice:" + operation_id


def _claim(billing, interval_seconds, limit):
    moment, token, key = now(), uid(), billing._unknown_invoice_recovery_key()
    with billing.db.transaction() as session:
        billing.runtime_policy.require_current(session, "payment_reconcile")
        session.execute(insert(Setting).values(key=key, value={}).on_conflict_do_nothing())
        marker = session.get(Setting, key, with_for_update=True)
        if (
            marker.value.get("next_check_at", "") > moment.isoformat()
            or marker.value.get("lease_until", "") > moment.isoformat()
        ):
            return None
        # Курсор даёт следующей ограниченной пачке шанс при отсутствии результата.
        cursor = marker.value.get("cursor", "")
        rows = session.execute(
            select(Order, ProviderOperation)
            .join(ProviderOperation, ProviderOperation.order_id == Order.id)
            .where(
                Order.environment == "live",
                Order.status == "awaiting_payment",
                ProviderOperation.terminal == billing.terminal,
                ProviderOperation.state == "outcome_unknown",
                ProviderOperation.provider_id.is_(None),
                ProviderOperation.request["scope_id"].as_string() == billing.runtime_policy.scope.scope_id,
                ~select(Inbox.id)
                .where(Inbox.event_key == func.concat("unknown-invoice:", ProviderOperation.id))
                .exists(),
            )
            .order_by(ProviderOperation.id <= cursor, ProviderOperation.id)
            .limit(limit)
        )
        bindings = []
        last_id = cursor
        for order, operation in rows:
            last_id = operation.id
            binding = _binding(session, billing, order, operation)
            if binding:
                bindings.append(binding)
        marker.value = {
            "claim": token if bindings else None,
            "lease_until": (moment + timedelta(minutes=5)).isoformat() if bindings else "",
            "next_check_at": (moment + timedelta(seconds=interval_seconds)).isoformat(),
            "cursor": last_id,
        }
        return (token, bindings) if bindings else None


def _queue_verified(billing, binding, token, payment_id):
    with billing.db.transaction() as session:
        billing.runtime_policy.require_current(session, "payment_reconcile")
        # Не меняем порядок блокировок callback: заказ, затем операция, затем маркер.
        order = session.get(Order, binding["order_id"], with_for_update=True)
        operation = session.get(ProviderOperation, binding["operation_id"], with_for_update=True)
        marker = session.get(Setting, billing._unknown_invoice_recovery_key(), with_for_update=True)
        if (
            not marker
            or marker.value.get("claim") != token
            or marker.value.get("lease_until", "") <= now().isoformat()
            or not order
            or not operation
            or _binding(session, billing, order, operation) != binding
            or session.scalar(
                select(ProviderOperation.id).where(
                    ProviderOperation.provider_id == payment_id, ProviderOperation.id != operation.id
                )
            )
            is not None
        ):
            return 0
        result = session.execute(
            insert(Inbox)
            .values(
                id=uid(),
                event_key=_receipt_key(operation.id),
                payload={
                    "payment_id": payment_id,
                    "order_id": order.id,
                    "source": "unknown_invoice_recovery",
                },
            )
            .on_conflict_do_nothing(index_elements=["event_key"])
            .returning(Inbox.id)
        )
        return int(result.scalar_one_or_none() is not None)


def _renew_claim(billing, token):
    with billing.db.transaction() as session:
        billing.runtime_policy.require_current(session, "payment_reconcile")
        marker = session.get(Setting, billing._unknown_invoice_recovery_key(), with_for_update=True)
        moment = now()
        if (
            not marker
            or marker.value.get("claim") != token
            or marker.value.get("lease_until", "") <= moment.isoformat()
        ):
            return False
        marker.value = marker.value | {"lease_until": (moment + timedelta(minutes=5)).isoformat()}
        return True


def recover_unknown_invoices(billing, provider, *, interval_seconds=300, limit=10):
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= 10
        or isinstance(interval_seconds, bool)
        or not isinstance(interval_seconds, int)
        or not 300 <= interval_seconds <= 86400
    ):
        raise ValueError("Некорректная периодичность или размер сверки неизвестных счетов.")
    if (
        billing.environment != "live"
        or not billing.runtime_policy.production
        or provider.terminal != billing.terminal
        or getattr(provider, "environment", None) != "live"
    ):
        return 0
    billing.runtime_policy.require("payment_reconcile")
    claim = _claim(billing, interval_seconds, limit)
    if claim is None:
        return 0
    token, bindings = claim
    queued = 0
    try:
        for binding in bindings:
            try:
                if not _renew_claim(billing, token):
                    break
                collection = provider.list_payments(binding["order_id"])
                billing.runtime_policy.require("payment_reconcile")
                items, total = collection.get("items"), collection.get("total")
                if (
                    not isinstance(items, list)
                    or type(total) is not int
                    or not 0 <= total <= 100
                    or total != len(items)
                    or not all(
                        isinstance(item, dict) and item.get("order_id") == binding["order_id"]
                        for item in items
                    )
                    or len(items) != 1
                ):
                    continue
                payment_id = items[0].get("payment_id")
                if not isinstance(payment_id, str):
                    continue
                RollyPay._safe_id(payment_id)
                if not _renew_claim(billing, token):
                    break
                data = provider.payment(payment_id)
                billing.runtime_policy.require("payment_reconcile")
                event = {"payment_id": payment_id, "order_id": binding["order_id"]}
                billing._verify_payment_response(event, binding["request"], data, strict=True)
                queued += _queue_verified(billing, binding, token, payment_id)
            except (DomainError, ValueError, KeyError, TypeError, ArithmeticError) as error:
                if isinstance(error, DomainError) and error.code.startswith("production_"):
                    raise
    finally:
        try:
            with billing.db.transaction() as session:
                billing.runtime_policy.require_current(session, "payment_reconcile")
                marker = session.get(Setting, billing._unknown_invoice_recovery_key(), with_for_update=True)
                if marker and marker.value.get("claim") == token:
                    marker.value = marker.value | {"claim": None, "lease_until": ""}
        except DomainError as error:
            # Отозванный допуск не разрешает даже снять старую аренду: она истечёт сама.
            if not error.code.startswith("production_"):
                raise
    return queued
