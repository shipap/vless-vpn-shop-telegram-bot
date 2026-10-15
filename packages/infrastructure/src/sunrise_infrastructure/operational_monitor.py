"""Очередь проблемных операций и ограниченные по частоте предупреждения владельцу."""

import hashlib
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import String, and_, cast, func, or_, select, text

from .db import (
    Account,
    Inbox,
    Job,
    NotificationDelivery,
    Order,
    Payment,
    ProviderOperation,
    Setting,
    Subscription,
    now,
)
from .notifications import NotificationService
from .staff_permissions import StaffPermissions, telegram_admin_ids


def provider_review_binding(operation, order):
    return {
        "operation_id": operation.id,
        "order_id": order.id,
        "operation_state": operation.state,
        "provider_id": operation.provider_id or "",
        "terminal": operation.terminal,
        "order_status": order.status,
        "environment": order.environment,
        "account_id": str(order.account_id),
        "amount": str(order.amount),
        "currency": order.currency,
    }


def provider_review_token(operation, order):
    return hashlib.sha256(
        json.dumps(provider_review_binding(operation, order), sort_keys=True).encode()
    ).hexdigest()


def provider_acknowledgeable_clause():
    return and_(
        ProviderOperation.state == "outcome_unknown",
        ProviderOperation.provider_id.is_(None),
        Order.status == "awaiting_payment",
        ~select(Payment.id).where(Payment.order_id == Order.id).correlate(Order).exists(),
    )


def provider_reviewed_clause():
    binding = {
        "operation_id": ProviderOperation.id,
        "order_id": Order.id,
        "operation_state": ProviderOperation.state,
        "provider_id": func.coalesce(ProviderOperation.provider_id, ""),
        "terminal": ProviderOperation.terminal,
        "order_status": Order.status,
        "environment": Order.environment,
        "account_id": cast(Order.account_id, String),
        "amount": cast(Order.amount, String),
        "currency": Order.currency,
    }
    return (
        select(Setting.key)
        .where(
            Setting.key == "op-review:" + ProviderOperation.id,
            Setting.value["version"].as_integer() == 1,
            provider_acknowledgeable_clause(),
            *(Setting.value["binding"][key].as_string() == value for key, value in binding.items()),
        )
        .correlate(ProviderOperation, Order)
        .exists()
    )


def operational_alert_body(snapshot, *, local_test=False):
    counts = snapshot["counts"]
    prefix = "Тестовый стенд.\n\n" if local_test is True else ""
    return (
        prefix
        + f"Записей, требующих внимания: {snapshot['signal_count']}.\n"
        + f"Фоновые задачи: {counts['job']}\n"
        + f"Заказы: {counts['order']}\n"
        + f"Платёжные события: {counts['payment_event']}\n"
        + f"Операции провайдера: {counts['provider_operation']}\n"
        + f"Доставка уведомлений: {counts['delivery']}\n\n"
        + "Счётчики показывают количество записей. Один заказ может учитываться в нескольких категориях.\n\n"
        + "Откройте очередь в админке. Повтор выдачи доступа не выполняет повторное списание денег."
    )


class OperationalMonitor:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings

    def snapshot(self, limit=50):
        with self.db.transaction() as session:
            return self.snapshot_in(session, limit)

    def snapshot_in(self, session, limit=50):
        limit = max(1, min(int(limit), 200))
        moment, cutoff = now(), now() - timedelta(minutes=15)
        rows, counts = [], {}
        job_query = select(Job).where(
            Job.kind.in_(
                ["provision", "credential_rotate", "cancel_agreement", "notification", "daily_backup"]
            ),
            or_(
                Job.state.in_(["review", "blocked", "failed"]),
                (Job.state == "pending") & (Job.available_at <= cutoff),
            ),
        )
        order_query = (
            select(Order)
            .outerjoin(Subscription, Subscription.id == Order.subscription_id)
            .where(
                or_(
                    Order.status == "reconciliation_required",
                    (Order.status == "paid")
                    & Subscription.status.in_(["provisioning_pending", "paid_pending_provisioning"])
                    & ~select(Job.id).where(Job.key == "provision:" + Order.id, Job.state == "done").exists()
                    & (func.coalesce(Order.fulfilled_at, Order.created_at) <= cutoff),
                )
            )
        )
        inbox_query = select(Inbox).where(
            or_(Inbox.state == "review", (Inbox.state == "pending") & (Inbox.available_at <= cutoff))
        )
        provider_query = (
            select(ProviderOperation, Order)
            .join(Order, Order.id == ProviderOperation.order_id)
            .where(
                ProviderOperation.state.in_(["outcome_unknown", "review"]),
                ~provider_reviewed_clause(),
            )
        )
        delivery_query = select(NotificationDelivery).where(
            or_(
                NotificationDelivery.state == "review",
                (NotificationDelivery.state == "pending") & (NotificationDelivery.available_at <= cutoff),
            )
        )
        for kind, query in [
            ("job", job_query),
            ("order", order_query),
            ("payment_event", inbox_query),
            ("provider_operation", provider_query),
            ("delivery", delivery_query),
        ]:
            counts[kind] = session.scalar(select(func.count()).select_from(query.subquery()))
            ordering = {
                "job": Job.available_at,
                "order": Order.created_at,
                "payment_event": Inbox.created_at,
                "provider_operation": Order.created_at,
                "delivery": NotificationDelivery.available_at,
            }[kind]
            for record in session.execute(query.order_by(ordering).limit(limit)):
                item = record[0]
                state = item.status if kind == "order" else item.state
                since = (
                    (item.fulfilled_at or item.created_at)
                    if kind == "order"
                    else (
                        record[1].created_at
                        if kind == "provider_operation"
                        else item.created_at
                        if kind == "payment_event"
                        else item.available_at
                    )
                )
                order_id = (
                    item.id
                    if kind == "order"
                    else item.order_id
                    if kind == "provider_operation"
                    else (item.payload.get("order_id") if kind == "job" else None)
                )
                if (
                    kind == "payment_event"
                    and item.payload.get("source") == "settlement_refresh"
                    and all(
                        isinstance(item.payload.get(key), str) and 1 <= len(item.payload[key]) <= 64
                        for key in ("payment_id", "order_id")
                    )
                ):
                    payment = session.get(Payment, item.payload.get("payment_id", ""))
                    candidate = session.get(Order, item.payload.get("order_id", ""))
                    operation = (
                        session.scalar(
                            select(ProviderOperation).where(ProviderOperation.order_id == candidate.id)
                        )
                        if candidate
                        else None
                    )
                    if (
                        payment
                        and candidate
                        and operation
                        and payment.provider == "rollypay"
                        and payment.order_id == candidate.id
                        and payment.account_id == candidate.account_id
                        and payment.environment == candidate.environment == "sandbox"
                        and payment.amount == candidate.amount
                        and payment.currency == candidate.currency
                        and payment.provider_id == operation.provider_id
                        and payment.terminal == operation.terminal
                    ):
                        order_id = candidate.id
                if (
                    kind == "job"
                    and item.kind == "provision"
                    and not order_id
                    and item.key.startswith("provision:")
                ):
                    candidate = session.get(Order, item.key[len("provision:") :])
                    if (
                        candidate
                        and candidate.account_id == item.account_id
                        and candidate.subscription_id == item.payload.get("subscription_id")
                    ):
                        order_id = candidate.id
                retry_kind = (
                    "provisioning" if kind == "job" and item.kind == "provision" and order_id else None
                )
                can_acknowledge = kind == "provider_operation" and bool(
                    session.scalar(
                        select(ProviderOperation.id)
                        .join(Order, Order.id == ProviderOperation.order_id)
                        .where(ProviderOperation.id == item.id, provider_acknowledgeable_clause())
                    )
                )
                rows.append(
                    {
                        "key": kind + ":" + item.id,
                        "kind": kind,
                        "reference": item.id,
                        "order_id": order_id,
                        "state": state,
                        "age_seconds": max(0, int((moment - since).total_seconds())),
                        "reason": (
                            "Результат создания счёта неизвестен. Автоматическая сверка продолжается; повторный счёт не создаётся."
                            if can_acknowledge
                            else "Неизвестен результат платежа: требуется сверка, новое списание запрещено."
                            if kind == "provider_operation"
                            else "Платёжное событие ожидает обработки или проверки."
                            if kind == "payment_event"
                            else "Уведомление ожидает доставки или проверки неизвестного результата."
                            if kind == "delivery"
                            else "Заказ требует сверки оплаты и оплаченных прав."
                            if kind == "order" and state == "reconciliation_required"
                            else "Оплата получена, выдача доступа задерживается."
                            if kind == "order"
                            else "Выдача доступа ожидает восстановления."
                            if item.kind == "provision"
                            else "Задача ожидает восстановления или ручной проверки."
                        ),
                        "retry_kind": retry_kind,
                        "can_acknowledge": can_acknowledge,
                        "ack_token": provider_review_token(item, record[1]) if can_acknowledge else None,
                        "target": "/admin#orders" if order_id else "/admin#operations",
                    }
                )
        rows.sort(key=lambda item: (-item["age_seconds"], item["key"]))
        return {
            "items": rows[:limit],
            "signal_count": sum(counts.values()),
            "counts": counts,
            "truncated": sum(counts.values()) > limit,
            "stalled_after_seconds": 900,
            "checked_at": moment.isoformat(),
        }

    def notify_owner(self):
        with self.db.transaction() as session:
            session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('operational-alert',0))"))
            snapshot = self.snapshot_in(session)
            if not snapshot["signal_count"]:
                return 0
            permissions = StaffPermissions(SimpleNamespace(db=self.db, settings=self.settings))
            candidates = session.scalars(
                select(Account).where(
                    Account.telegram_id.in_(telegram_admin_ids(self.settings)),
                )
            )
            signature = hashlib.sha256(json.dumps(snapshot["counts"], sort_keys=True).encode()).hexdigest()
            emitted, moment = 0, now()
            for owner in candidates:
                if permissions.role_in(session, owner) != "owner":
                    continue
                marker_key = "operational-alert:" + str(owner.id)
                marker = session.get(Setting, marker_key)
                if marker:
                    previous = datetime.fromisoformat(marker.value["notified_at"])
                    if moment - previous < timedelta(hours=1):
                        continue
                bucket = int(moment.timestamp() // 3600)
                NotificationService(self.db, self.settings).emit_in(
                    session,
                    owner.id,
                    f"operational-alert:{bucket}:{signature[:24]}",
                    "operational_alert",
                    "Операции требуют внимания",
                    operational_alert_body(snapshot, local_test=getattr(self.settings, "local_test", False)),
                    target="/admin#operations",
                    owner_only=True,
                    actions=[{"label": "Открыть очередь", "target": "/admin#operations"}],
                )
                value = {"signature": signature, "notified_at": moment.isoformat()}
                if marker:
                    marker.value = value
                else:
                    session.add(Setting(key=marker_key, value=value))
                emitted += 1
            return emitted
