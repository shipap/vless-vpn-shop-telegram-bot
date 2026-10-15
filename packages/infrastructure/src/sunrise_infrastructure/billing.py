from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import ValidationError
from sqlalchemy import func, or_, select
from sunrise_core.errors import DomainError, missing
from sunrise_core.pricing import money
from sunrise_core.refunds import VerifiedEntitlementUsage, decide_refund, paid_entitlement_matches

from . import panel_entitlements
from .db import (
    Alias,
    Audit,
    Inbox,
    Job,
    Ledger,
    Order,
    Payment,
    PromoUse,
    ProviderOperation,
    Quote,
    Refund,
    RefundReview,
    Segment,
    Setting,
    Settlement,
    Subscription,
    now,
    uid,
)
from .instance_config import public_url
from .payment_description import payment_description
from .providers import checkout_url, sandbox_checkout_url


@dataclass(frozen=True)
class VerifiedPayment:
    provider: str
    provider_id: str
    terminal: str
    environment: str
    order_id: str
    amount: Decimal
    currency: str
    paid_at: datetime
    fee: Decimal | None = None
    fee_currency: str | None = None
    net_usdt: Decimal | None = None
    settled_at: datetime | None = None


class Billing:
    def __init__(self, commerce, terminal, environment):
        self.commerce, self.db = commerce, commerce.db
        self.terminal, self.environment = terminal, environment
        self.runtime_policy = getattr(commerce, "runtime_policy", None)
        if environment in {"live", "scoped_live"}:
            if not self.runtime_policy or not (
                self.runtime_policy.production
                or environment == "scoped_live"
                and self.runtime_policy.scoped_payments
            ):
                raise DomainError(
                    "production_admission_denied", "Допуск платежного окружения не подтверждён.", 503
                )
            self.runtime_policy.require()

    @property
    def real_payments(self):
        return self.environment in {"live", "scoped_live"}

    def accept(self, fact):
        if self.real_payments:
            self.runtime_policy.require("financial_apply")
        if fact.terminal != self.terminal or fact.environment != self.environment:
            raise DomainError("payment_context_mismatch", "Параметры платежа не совпадают.", 400)
        if not fact.amount.is_finite() or fact.amount <= 0:
            raise DomainError("payment_amount_invalid", "Некорректная сумма платежа.", 400)
        with self.db.transaction() as s:
            for _ in range(3):
                with s.begin_nested() as attempt:
                    initial = s.get(Order, fact.order_id, populate_existing=True)
                    if not initial:
                        raise missing()
                    owner = initial.account_id
                    referrer = self.commerce._account(s, owner, customer=False).referrer_id
                    locked = self.commerce._lock_accounts_in(s, [owner, referrer])
                    a = self.commerce._account(s, locked[owner], customer=False)
                    if a.referrer_id == locked.get(referrer):
                        break
                    if locked[owner] == owner:
                        raise missing()
                    # The verified merge may select a canonical owner with its own referrer.
                    # Release this account set and lock the complete new set in sorted order.
                    attempt.rollback()
            else:
                raise missing()
            o = s.scalar(
                select(Order)
                .where(Order.id == fact.order_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if not o or o.account_id != a.id or a.referrer_id != locked.get(referrer):
                raise missing()
            if self.environment == "scoped_live":
                self.runtime_policy.require_order_in(s, o, provider_id=fact.provider_id)
            if self.real_payments:
                self.runtime_policy.require_current(s, "financial_apply")
            if o.amount != fact.amount or o.currency != fact.currency or o.environment != fact.environment:
                raise DomainError(
                    "payment_amount_mismatch", "Сумма или валюта платежа не совпадает с заказом.", 400
                )
            prior = s.scalar(
                select(Payment).where(
                    Payment.provider == fact.provider,
                    Payment.terminal == fact.terminal,
                    Payment.environment == fact.environment,
                    Payment.provider_id == fact.provider_id,
                )
            )
            if prior:
                if prior.order_id != o.id:
                    raise DomainError("payment_association_mismatch", "Платёж связан с другим заказом.", 400)
                self._settlement_snapshot(s, prior, fact)
                if self._legacy_recovery_in(s, o, prior):
                    self.commerce._fulfill(s, a, o, s.get(Quote, o.quote_id), prior.id)
                return self.commerce._order_dto(o)
            duplicate_order_payment = s.scalar(select(Payment).where(Payment.order_id == o.id))
            pid = uid()
            payment = Payment(
                id=pid,
                provider=fact.provider,
                provider_id=fact.provider_id,
                terminal=fact.terminal,
                environment=fact.environment,
                order_id=o.id,
                account_id=a.id,
                original_account_id=a.id,
                amount=fact.amount,
                currency=fact.currency,
                paid_at=fact.paid_at,
            )
            s.add(payment)
            s.flush()
            self._settlement_snapshot(s, payment, fact)
            if duplicate_order_payment:
                o.status = "reconciliation_required"
            else:
                self.commerce._fulfill(s, a, o, s.get(Quote, o.quote_id), pid)
            return self.commerce._order_dto(o)

    def _legacy_recovery_in(self, session, order, payment=None):
        """Only a still-reserved own migration grant may recover an accepted payment."""
        if (
            self.environment != "live"
            or order is None
            or order.environment != "live"
            or order.status != "reconciliation_required"
            or order.fulfilled_at is not None
        ):
            return False
        quote = session.get(Quote, order.quote_id, populate_existing=True)
        use = session.get(PromoUse, order.id, populate_existing=True)
        if (
            quote is None
            or quote.account_id != order.account_id
            or use is None
            or use.state != "reserved"
            or use.account_id != order.account_id
        ):
            return False
        from .promos import LegacyPromoGrantContext

        try:
            context = LegacyPromoGrantContext.model_validate(quote.snapshot.get("legacy_promo_grant"))
        except (ValidationError, ValueError, TypeError):
            return False
        if (
            context.scope_id != self.runtime_policy.scope.scope_id
            or context.code != use.code
            or context.code != quote.snapshot.get("promo_code")
            or quote.snapshot.get("promo_activation_id") != context.grant_id
        ):
            return False
        source = int(context.source_account_id)
        alias = session.get(Alias, source, populate_existing=True) if source != order.account_id else None
        if source != order.account_id and (alias is None or alias.canonical_id != order.account_id):
            return False
        payments = list(session.scalars(select(Payment).where(Payment.order_id == order.id)))
        if len(payments) != 1 or payment is not None and payments[0].id != payment.id:
            return False
        payment = payments[0]
        return bool(
            payment.status == "paid"
            and payment.account_id == order.account_id
            and payment.provider == "rollypay"
            and payment.terminal == self.terminal
            and payment.environment == "live"
            and payment.amount == order.amount
            and payment.currency == order.currency
            and session.scalar(select(Refund.id).where(Refund.payment_id == payment.id)) is None
            and self.commerce._paid_order_timing_in(session, order, quote, payment.id)
        )

    @staticmethod
    def _merchant_window(data, order):
        """Dates come only from an authenticated own merchant response."""
        if "created_at" not in data and "expires_at" not in data:
            return {}
        from .panel import PanelReview
        from .panel_v3 import instant

        try:
            created, expiry = instant(data["created_at"]), instant(data["expires_at"])
            if not order.created_at.replace(microsecond=0) <= created <= now() or expiry <= created:
                raise ValueError()
        except (KeyError, TypeError, ValueError, PanelReview):
            raise DomainError("payment_deadline_review", "Срок оплаты требует сверки.", 409) from None
        return {"created_at": created.isoformat(), "expires_at": expiry.isoformat()}

    def _settlement_snapshot(self, s, p, fact):
        if self.real_payments:
            self.runtime_policy.require_current(s, "financial_apply")
        import hashlib

        fee_changed = fact.fee is not None and (p.fee != fact.fee or p.fee_currency != fact.fee_currency)
        net_changed = fact.net_usdt is not None and p.net_usdt != fact.net_usdt
        if not fee_changed and not net_changed:
            return
        if fact.fee is not None and (not fact.fee.is_finite() or not fact.fee_currency):
            raise DomainError("settlement_invalid", "Комиссия требует проверки.", 400)
        if fact.net_usdt is not None and not fact.net_usdt.is_finite():
            raise DomainError("settlement_invalid", "Зачисление требует проверки.", 400)
        if (
            fee_changed
            and p.fee is not None
            and p.fee_currency is not None
            and p.fee_currency != fact.fee_currency
        ):
            raise DomainError(
                "settlement_currency_changed", "Изменение валюты комиссии требует отдельной сверки."
            )
        key = (
            "snapshot:"
            + hashlib.sha256(f"{p.id}|{fact.fee}|{fact.fee_currency}|{fact.net_usdt}".encode()).hexdigest()
        )
        if s.scalar(select(Settlement).where(Settlement.operation_id == key)):
            # Replayed old snapshots cannot roll the current cumulative values backwards.
            return
        if fee_changed and p.fee is not None and p.fee_currency is None:
            s.add(
                Audit(
                    action="settlement_currency_verified",
                    target=p.id,
                    reason="Валюта комиссии подтверждена провайдером; прежняя сумма без валюты не вычитается.",
                    details={
                        "before": {"fee": str(p.fee), "fee_currency": None},
                        "after": {"fee": str(fact.fee), "fee_currency": fact.fee_currency},
                    },
                )
            )
        verified_fee_basis = p.fee if p.fee_currency is not None else None
        s.add(
            Settlement(
                payment_id=p.id,
                operation_id=key,
                fee=fact.fee - (verified_fee_basis or Decimal(0)) if fee_changed else None,
                fee_currency=fact.fee_currency if fee_changed else None,
                net_usdt=fact.net_usdt - (p.net_usdt or Decimal(0)) if net_changed else None,
                occurred_at=fact.settled_at or now(),
                time_source="provider_confirmed" if fact.settled_at else "received_at",
            )
        )
        if fee_changed:
            p.fee = fact.fee
            p.fee_currency = fact.fee_currency
        if net_changed:
            p.net_usdt = fact.net_usdt

    def record_settlement(self, provider_id, operation_id, fee, fee_currency, net_usdt, occurred_at):
        if (
            occurred_at.tzinfo is None
            or any(v is not None and not v.is_finite() for v in (fee, net_usdt))
            or (fee is not None and not fee_currency)
        ):
            raise DomainError("settlement_invalid", "Проверьте сумму, валюту и дату зачисления.", 400)
        with self.db.transaction() as s:
            p = s.scalar(
                select(Payment)
                .where(
                    Payment.provider_id == provider_id,
                    Payment.terminal == self.terminal,
                    Payment.environment == self.environment,
                )
                .with_for_update()
            )
            if not p:
                raise missing()
            key = "settlement:" + p.id + ":" + operation_id
            existing = s.scalar(select(Settlement).where(Settlement.operation_id == key))
            if existing:
                if (existing.fee, existing.fee_currency, existing.net_usdt, existing.occurred_at) != (
                    fee,
                    fee_currency,
                    net_usdt,
                    occurred_at,
                ):
                    raise DomainError("settlement_conflict", "Зачисление требует сверки.")
                return {"status": "recorded"}
            if p.fee_currency and fee is not None and p.fee_currency != fee_currency:
                raise DomainError("settlement_currency_changed", "Валюта комиссии требует сверки.")
            if self.real_payments:
                self.runtime_policy.require_current(s, "financial_apply")
            s.add(
                Settlement(
                    payment_id=p.id,
                    operation_id=key,
                    fee=fee,
                    fee_currency=fee_currency,
                    net_usdt=net_usdt,
                    occurred_at=occurred_at,
                )
            )
            if fee is not None:
                p.fee = (p.fee or Decimal(0)) + fee
                p.fee_currency = fee_currency
            if net_usdt is not None:
                p.net_usdt = (p.net_usdt or Decimal(0)) + net_usdt
            return {"status": "recorded"}

    def refund(self, provider_id, operation_id, amount, occurred_at, *, usage=None, kind="refund"):
        if kind not in {"refund", "chargeback"} or occurred_at.tzinfo is None:
            raise DomainError("refund_context_invalid", "Проверьте источник и дату возврата.", 400)
        with self.db.transaction() as s:
            hint = s.scalar(
                select(Payment).where(
                    Payment.provider_id == provider_id,
                    Payment.terminal == self.terminal,
                    Payment.environment == self.environment,
                )
            )
            if not hint:
                raise missing()
            owner = hint.account_id
            reward_hint = s.scalar(select(Ledger).where(Ledger.operation == "referral_payment:" + hint.id))
            referrer = reward_hint.account_id if reward_hint else None
            locked = self.commerce._lock_accounts_in(s, [owner, referrer])
            p = s.scalar(
                select(Payment)
                .where(
                    Payment.provider_id == provider_id,
                    Payment.terminal == self.terminal,
                    Payment.environment == self.environment,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if not p or p.account_id != locked[owner]:
                raise missing()
            reward = s.scalar(select(Ledger).where(Ledger.operation == "referral_payment:" + p.id))
            if reward and (referrer is None or reward.account_id != locked[referrer]):
                raise missing()
            key = "refund:" + p.id + ":" + operation_id
            existing = s.scalar(select(Refund).where(Refund.operation_id == key))
            if existing:
                if existing.amount != amount:
                    raise DomainError("refund_conflict", "Возврат требует сверки.")
                return {"status": "recorded"}
            total = s.scalar(
                select(func.coalesce(func.sum(Refund.amount), 0)).where(Refund.payment_id == p.id)
            )
            if not amount.is_finite() or amount <= 0 or total + amount > p.amount:
                raise DomainError("refund_amount_invalid", "Сумма возврата требует проверки.", 400)
            if self.real_payments:
                self.runtime_policy.require_current(s, "financial_apply")
            refund = Refund(payment_id=p.id, operation_id=key, amount=amount, occurred_at=occurred_at)
            s.add(refund)
            s.flush()
            p.status = "refunded" if total + amount == p.amount else "partially_refunded"
            o = s.get(Order, p.order_id)
            o.status = "refund_review"
            segment = s.scalar(select(Segment).where(Segment.order_id == o.id).with_for_update())
            decision = decide_refund(
                full=total == 0 and amount == p.amount,
                disputed=kind == "chargeback",
                evidence=usage,
                instant=now(),
                order_id=o.id,
                segment_id=segment.id if segment else None,
            )
            s.execute(select(func.pg_advisory_xact_lock(617045503)))
            policy = self.commerce._setting(
                s, "refund_policy", {"max_age_hours": 24, "max_traffic_bytes": 100_000_000}
            )
            if decision["action"] == "automatic_revocation" and (
                (now() - usage.started_at).total_seconds() > policy.get("max_age_hours", 24) * 3600
                or usage.traffic_bytes > policy.get("max_traffic_bytes", 100_000_000)
            ):
                decision = {"action": "manual_review", "reason": "configured_refund_threshold"}
            quote = s.get(Quote, o.quote_id)
            if decision["action"] == "automatic_revocation" and (
                not segment
                or segment.subscription_id != o.subscription_id
                or usage.started_at < segment.starts_at
                or not paid_entitlement_matches(segment, quote.snapshot)
            ):
                decision = {"action": "manual_review", "reason": "paid_entitlement_mapping_unverified"}
            sub = (
                s.scalar(select(Subscription).where(Subscription.id == o.subscription_id).with_for_update())
                if o.subscription_id
                else None
            )
            if self.real_payments:
                self.runtime_policy.require_current(s, "financial_apply")
            if sub and decision["action"] == "automatic_revocation":
                from .retention import fenced_in

                if fenced_in(s, sub):
                    decision = {"action": "manual_review", "reason": "retention_target_fenced"}
            if decision["action"] == "dispute_freeze" and sub:
                flag = s.get(Setting, "dispute:" + sub.id)
                value = {"enabled": True, "payment_id": p.id, "reason": "provider_chargeback"}
                if flag:
                    flag.value = value
                else:
                    s.add(Setting(key="dispute:" + sub.id, value=value))
            before_rights = {
                "expires_at": sub.expires_at.isoformat() if sub and sub.expires_at else None,
                "segment_ends_at": segment.ends_at.isoformat() if segment else None,
            }
            if decision["action"] == "automatic_revocation" and sub:
                prior_status = panel_entitlements.local_status(s, sub)
                segment.ends_at = max(segment.starts_at, now())
                segment.kind = "refunded"
                moment = now()
                remaining = panel_entitlements.remaining_segments(s, sub, moment)
                sub.expires_at = max(
                    (panel_entitlements.effective_end(s, sub, item) for item in remaining), default=moment
                )
                active = panel_entitlements.current_segment(s, sub, moment)
                if active:
                    sub.product_id, sub.devices_limit = active.product_id, active.devices
                sub.version += 1
                sub.status = "provisioning_pending" if active else "revocation_pending"
                payload = self.commerce._provision_payload(
                    s,
                    sub,
                    active.price_basis.get("panel_mappings", {}) if active else None,
                    prior_status=prior_status,
                    origin="refund",
                )
                payload["refunded_segment_id"] = segment.id
                if not active:
                    payload |= {
                        "panel_status": "DISABLED",
                        "desired_subscription_status": "revoked"
                        if prior_status in {"active", "expired"}
                        else prior_status,
                    }
                s.add(
                    Job(
                        key="refund-rights:" + key,
                        kind="provision",
                        account_id=sub.account_id,
                        payload=payload,
                    )
                )
            from .refund_reviews import segment_state

            usage_snapshot = (
                {
                    "source": usage.source,
                    "usable_at": usage.started_at.isoformat(),
                    "observed_at": usage.observed_at.isoformat(),
                    "traffic_bytes": usage.traffic_bytes,
                    "order_id": usage.order_id,
                    "segment_id": usage.segment_id,
                }
                if isinstance(usage, VerifiedEntitlementUsage)
                else None
            )
            if decision["action"] != "automatic_revocation":
                s.add(
                    RefundReview(
                        refund_id=refund.id,
                        payment_id=p.id,
                        segment_id=segment.id if segment else None,
                        state="dispute" if kind == "chargeback" else "pending",
                        evidence={
                            "reason": decision["reason"],
                            "kind": kind,
                            "operation_id": key,
                            "full_refund": total + amount == p.amount,
                            "usage": usage_snapshot,
                            "segment": segment_state(segment),
                            "thresholds": policy,
                        },
                    )
                )
            s.add(
                Audit(
                    action="refund_entitlement_decision",
                    target=p.id,
                    reason=decision["reason"],
                    details={
                        "operation_id": key,
                        "kind": kind,
                        "action": decision["action"],
                        "full_refund": total + amount == p.amount,
                        "subscription_id": sub.id if sub else None,
                        "before": before_rights,
                        "remaining_expires_at": sub.expires_at.isoformat()
                        if sub and sub.expires_at
                        else None,
                        "usage_source": usage.source if isinstance(usage, VerifiedEntitlementUsage) else None,
                        "automatic_recalculation": False,
                        "usage_snapshot": usage_snapshot,
                        "thresholds": policy,
                    },
                )
            )
            if reward:
                ref = self.commerce._account(s, reward.account_id, True, customer=False)
                if self.real_payments:
                    self.runtime_policy.require_current(s, "financial_apply")
                cumulative = money(reward.amount * (total + amount) / p.amount)
                reversed_before = s.scalar(
                    select(func.coalesce(func.sum(Ledger.amount), 0)).where(
                        Ledger.operation.like("reversal:" + p.id + ":%")
                    )
                )
                reverse = cumulative + reversed_before
                ref.bonus_balance -= reverse
                s.add(
                    Ledger(
                        account_id=ref.id, operation="reversal:" + p.id + ":" + operation_id, amount=-reverse
                    )
                )
            return {
                "status": "recorded",
                "entitlement_action": decision["action"],
                "reason": decision["reason"],
            }

    def recover_unknown_invoices(self, provider, *, interval_seconds=300, limit=10):
        from .unknown_invoices import recover_unknown_invoices

        return recover_unknown_invoices(self, provider, interval_seconds=interval_seconds, limit=limit)

    def _unknown_invoice_recovery_key(self):
        import hashlib

        context = f"{self.environment}|{self.runtime_policy.scope.scope_id}|{self.terminal}"
        return "invoice-recover:" + hashlib.sha256(context.encode()).hexdigest()

    def poll_known_payments(self, *, limit=10):
        from sqlalchemy.dialects.postgresql import insert

        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ValueError("Нужен ограниченный размер платёжной сверки от 1 до 50.")
        if self.environment not in {"sandbox", "live", "scoped_live"}:
            return 0
        with self.db.transaction() as session:
            operations = list(
                session.scalars(
                    select(ProviderOperation)
                    .join(Order, Order.id == ProviderOperation.order_id)
                    .where(
                        Order.environment == self.environment,
                        Order.status == "awaiting_payment",
                        ProviderOperation.terminal == self.terminal,
                        ProviderOperation.provider_id.is_not(None),
                        ProviderOperation.state.in_(["created", "reconciled"]),
                        ~select(Inbox.id)
                        .where(Inbox.event_key == self.environment + "-poll:" + ProviderOperation.id)
                        .exists(),
                    )
                    .order_by(ProviderOperation.id)
                    .limit(limit)
                )
            )
            queued = 0
            for operation in operations:
                if self.environment == "scoped_live":
                    self.runtime_policy.require_order_in(
                        session,
                        session.get(Order, operation.order_id),
                        operation=operation,
                        provider_id=operation.provider_id,
                    )
                if self.real_payments:
                    self.runtime_policy.require_current(session)
                result = session.execute(
                    insert(Inbox)
                    .values(
                        id=uid(),
                        event_key=self.environment + "-poll:" + operation.id,
                        payload={
                            "payment_id": operation.provider_id,
                            "order_id": operation.order_id,
                            "test": not self.real_payments,
                            "environment": "prod" if self.real_payments else "sandbox",
                        },
                    )
                    .on_conflict_do_nothing(index_elements=["event_key"])
                    .returning(Inbox.id)
                )
                queued += int(result.scalar_one_or_none() is not None)
            return queued

    def refresh_missing_settlements(self, provider, *, interval_seconds=300, limit=10):
        """Локальная политика сверки: только GET известных платежей, без повторной выдачи прав."""
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 50
            or isinstance(interval_seconds, bool)
            or not isinstance(interval_seconds, int)
            or not 60 <= interval_seconds <= 86400
        ):
            raise ValueError("Некорректная периодичность или размер сверки комиссии.")
        if self.environment not in {"sandbox", "live", "scoped_live"} or provider.terminal != self.terminal:
            return 0
        claims = self._claim_settlement_refreshes(interval_seconds, limit)
        for binding, token in claims:
            try:
                data = provider.payment(binding["provider_id"])
                self._verify_payment_response(
                    {"payment_id": binding["provider_id"], "order_id": binding["order_id"]},
                    binding["request"],
                    data,
                    strict=True,
                )
                if data.get("status") != "paid":
                    raise DomainError(
                        "settlement_payment_status_review",
                        "Статус подтверждённого платежа требует сверки.",
                        400,
                    )
                for field in ("platform_fee_usdt", "amount_usdt_net"):
                    raw = data.get(field)
                    if isinstance(raw, bool):
                        raise ValueError("invalid_provider_response")
                    if raw is not None:
                        cumulative = Decimal(raw)
                        if not cumulative.is_finite() or cumulative < 0:
                            raise DomainError(
                                "settlement_invalid", "Некорректные итоговые данные комиссии.", 400
                            )
                self._apply_settlement_refresh(binding, token, data)
            except (DomainError, ValueError, KeyError, TypeError, ArithmeticError) as exc:
                self._settlement_refresh_error(binding, token, exc)
        return len(claims)

    @staticmethod
    def _settlement_refresh_key(payment_id):
        import hashlib

        return "settlement:" + hashlib.sha256(payment_id.encode("utf-8")).hexdigest()

    @staticmethod
    def _settlement_binding(payment, order, operation):
        return {
            "payment_id": payment.id,
            "provider": payment.provider,
            "provider_id": payment.provider_id,
            "terminal": payment.terminal,
            "environment": payment.environment,
            "order_id": payment.order_id,
            "account_id": str(payment.account_id),
            "amount": str(payment.amount),
            "currency": payment.currency,
            "payment_status": payment.status,
            "fee": payment.fee,
            "fee_currency": payment.fee_currency,
            "net_usdt": payment.net_usdt,
            "order_account_id": str(order.account_id),
            "order_amount": str(order.amount),
            "order_currency": order.currency,
            "order_environment": order.environment,
            "operation_id": operation.id,
            "operation_order_id": operation.order_id,
            "operation_provider_id": operation.provider_id,
            "operation_terminal": operation.terminal,
            "operation_state": operation.state,
            "request": dict(operation.request),
        }

    def _claim_settlement_refreshes(self, interval_seconds, limit):
        moment, claims = now(), []
        with self.db.transaction() as session:
            due = func.coalesce(Setting.value["next_check_at"].as_string(), "")
            rows = session.execute(
                select(Payment, Order, ProviderOperation)
                .join(Order, Order.id == Payment.order_id)
                .join(
                    ProviderOperation,
                    (ProviderOperation.order_id == Order.id)
                    & (ProviderOperation.provider_id == Payment.provider_id),
                )
                .outerjoin(
                    Setting,
                    Setting.key
                    == func.concat(
                        "settlement:", func.encode(func.sha256(func.convert_to(Payment.id, "UTF8")), "hex")
                    ),
                )
                .where(
                    Payment.provider == "rollypay",
                    Payment.environment == self.environment,
                    Payment.terminal == self.terminal,
                    Payment.status == "paid",
                    Payment.provider_id.is_not(None),
                    or_(Payment.fee.is_(None), Payment.fee_currency.is_(None), Payment.net_usdt.is_(None)),
                    Order.environment == self.environment,
                    ProviderOperation.terminal == self.terminal,
                    ProviderOperation.state.in_(["created", "reconciled"]),
                    or_(
                        Setting.key.is_(None),
                        (Setting.value["state"].as_string() == "pending") & (due <= moment.isoformat()),
                    ),
                )
                .order_by(due, Payment.paid_at, Payment.id)
                .limit(limit)
                .with_for_update(of=Payment, skip_locked=True)
            )
            for payment, order, operation in rows:
                key, token = self._settlement_refresh_key(payment.id), uid()
                marker = session.get(Setting, key, with_for_update=True)
                if marker and (
                    marker.value.get("state") != "pending"
                    or marker.value.get("next_check_at", "") > moment.isoformat()
                ):
                    continue
                if self.real_payments:
                    self.runtime_policy.require_current(session, "payment_reconcile")
                value = {
                    "state": "pending",
                    "next_check_at": (moment + timedelta(seconds=interval_seconds)).isoformat(),
                    "claim": token,
                    "last_error": None,
                }
                if marker:
                    marker.value = value
                else:
                    session.add(Setting(key=key, value=value))
                claims.append((self._settlement_binding(payment, order, operation), token))
        return claims

    def _apply_settlement_refresh(self, binding, token, data):
        with self.db.transaction() as session:
            self.commerce._account(session, int(binding["account_id"]), True, customer=False)
            order = session.get(Order, binding["order_id"], with_for_update=True)
            payment = session.get(Payment, binding["payment_id"], with_for_update=True)
            operation = session.get(ProviderOperation, binding["operation_id"], with_for_update=True)
            if (
                not order
                or not payment
                or not operation
                or self._settlement_binding(payment, order, operation) != binding
                or payment.account_id != order.account_id
                or payment.amount != order.amount
                or payment.currency != order.currency
                or order.environment != payment.environment
                or operation.request.get("order_id") != order.id
                or Decimal(operation.request["amount"]) != payment.amount
                or operation.request.get("currency") != payment.currency
            ):
                raise DomainError("payment_association_mismatch", "Связь платежа изменилась при сверке.", 400)
            marker = session.get(Setting, self._settlement_refresh_key(payment.id), with_for_update=True)
            if not marker or marker.value.get("claim") != token or marker.value.get("state") != "pending":
                return
            self._settlement_snapshot(
                session,
                payment,
                VerifiedPayment(
                    payment.provider,
                    payment.provider_id,
                    payment.terminal,
                    payment.environment,
                    payment.order_id,
                    payment.amount,
                    payment.currency,
                    payment.paid_at,
                    Decimal(data["platform_fee_usdt"]) if data.get("platform_fee_usdt") is not None else None,
                    "USDT" if data.get("platform_fee_usdt") is not None else None,
                    Decimal(data["amount_usdt_net"]) if data.get("amount_usdt_net") is not None else None,
                ),
            )
            marker.value = marker.value | {
                "state": "completed"
                if payment.fee is not None
                and payment.fee_currency is not None
                and payment.net_usdt is not None
                else "pending",
                "claim": None,
                "last_error": None,
            }

    def _settlement_refresh_error(self, binding, token, error):
        import hashlib

        from sqlalchemy.dialects.postgresql import insert

        retry = isinstance(error, DomainError) and error.status >= 500
        allowed = {
            "payment_context_mismatch",
            "payment_association_mismatch",
            "payment_amount_mismatch",
            "payment_owner_mismatch",
            "settlement_invalid",
            "settlement_currency_changed",
            "settlement_payment_status_review",
        }
        reason = (
            "provider_unavailable"
            if retry
            else error.code
            if isinstance(error, DomainError) and error.code in allowed
            else "invalid_provider_response"
        )
        with self.db.transaction() as session:
            marker = session.get(
                Setting, self._settlement_refresh_key(binding["payment_id"]), with_for_update=True
            )
            if not marker or marker.value.get("claim") != token:
                return
            marker.value = marker.value | {
                "state": "pending" if retry else "review",
                "claim": None,
                "last_error": reason,
            }
            if not retry:
                key = (
                    "settlement-review:"
                    + hashlib.sha256(
                        f"{binding['payment_id']}|{binding['operation_id']}|{reason}".encode()
                    ).hexdigest()[:60]
                )
                session.execute(
                    insert(Inbox)
                    .values(
                        id=uid(),
                        event_key=key,
                        state="review",
                        last_error=reason,
                        payload={
                            "source": "settlement_refresh",
                            "reason": reason,
                            "order_id": binding["order_id"],
                            "payment_id": binding["payment_id"],
                        },
                    )
                    .on_conflict_do_nothing(index_elements=["event_key"])
                )

    def reconcile_inbox(self, provider, limit=50):
        from datetime import timedelta

        processed = 0
        with self.db.transaction() as s:
            ids = list(
                s.scalars(
                    select(Inbox.id)
                    .where(Inbox.state == "pending", Inbox.available_at <= now())
                    .order_by(Inbox.created_at)
                    .limit(limit)
                )
            )
        for iid in ids:
            try:
                if self._reconcile_one(iid, provider):
                    processed += 1
            except (DomainError, ValueError, KeyError, TypeError) as exc:
                with self.db.transaction() as s:
                    row = s.get(Inbox, iid)
                    row.attempts += 1
                    row.last_error = exc.code if isinstance(exc, DomainError) else "invalid_provider_response"
                    row.state = "pending" if isinstance(exc, DomainError) and exc.status >= 500 else "review"
                    row.available_at = now() + timedelta(seconds=min(3600, 2 ** min(row.attempts, 11)))
        return processed

    def _live_response_matches_request(self, data, saved_request):
        """An authenticated response must match the immutable merchant request."""
        from decimal import InvalidOperation

        amount = data.get("amount")
        if isinstance(amount, bool) or not isinstance(amount, (str, int, Decimal)):
            return False
        try:
            amount = Decimal(str(amount))
            expected = Decimal(str(saved_request["amount"]))
        except (InvalidOperation, KeyError, ValueError):
            return False
        return (
            amount.is_finite()
            and expected.is_finite()
            and amount > 0
            and amount == expected
            and data.get("order_id") == saved_request.get("order_id")
            and data.get("payment_currency") == saved_request.get("currency")
            and ("currency" not in data or data["currency"] == saved_request.get("currency"))
            and (
                "customer_id" not in data
                or type(data["customer_id"]) in {str, int}
                and str(data["customer_id"]) == saved_request.get("account_id")
            )
            and ("terminal_id" not in data or data["terminal_id"] == self.terminal)
        )

    def _verify_payment_response(self, event, saved_request, data, *, strict=False):
        from .settings import payment_context_matches

        if (strict or self.real_payments) and isinstance(data.get("amount"), bool):
            raise DomainError("payment_amount_mismatch", "Некорректная сумма платежа.", 400)
        proof = saved_request.get("sandbox_creation")
        sandbox_verified = data.get("test") is True
        if self.real_payments:
            sandbox_verified = (
                payment_context_matches(data, self.environment)
                and saved_request.get("environment") == self.environment
                and saved_request.get("provider") == "rollypay"
                and saved_request.get("terminal") == self.terminal
                and saved_request.get("scope_id") == self.runtime_policy.scope.scope_id
                and saved_request.get("payment_method") in {"crypto", "xrocket", "sbp"}
                and data.get("payment_method") == saved_request["payment_method"]
                and self._live_response_matches_request(data, saved_request)
            )
        elif "test" not in data:
            sandbox_verified = (
                data.get("environment") == "sandbox"
                and proof
                == {
                    "test": True,
                    "environment": "sandbox",
                    "payment_id": event["payment_id"],
                    "order_id": event["order_id"],
                    "terminal_id": self.terminal,
                }
                and str(data.get("customer_id")) == saved_request["account_id"]
            )
        terminal_verified = data.get("terminal_id") == self.terminal
        if self.real_payments and "terminal_id" not in data:
            terminal_verified = (
                saved_request.get("terminal") == self.terminal
                and saved_request.get("scope_id") == self.runtime_policy.scope.scope_id
            )
        if (
            not terminal_verified
            or not sandbox_verified
            or (
                "environment" in data and data["environment"] != ("prod" if self.real_payments else "sandbox")
            )
        ):
            raise DomainError("payment_context_mismatch", "Платёж требует проверки окружения.", 400)
        if data.get("order_id") != event["order_id"] or data.get("payment_id") != event["payment_id"]:
            raise DomainError("payment_association_mismatch", "Платёж требует проверки заказа.", 400)
        if (
            Decimal(data["amount"]) != Decimal(saved_request["amount"])
            or data["payment_currency"] != saved_request["currency"]
        ):
            raise DomainError("payment_amount_mismatch", "Сумма не совпадает с сохранённым запросом.", 400)
        if data.get("customer_id") is not None and str(data["customer_id"]) != saved_request["account_id"]:
            raise DomainError("payment_owner_mismatch", "Владелец платежа не совпадает.", 400)
        if strict and (
            data.get("environment") != ("prod" if self.real_payments else "sandbox")
            or not self.real_payments
            and str(data.get("customer_id")) != saved_request["account_id"]
        ):
            raise DomainError("payment_context_mismatch", "Отсутствует подтверждённый контекст платежа.", 400)

    def _reconcile_one(self, iid, provider):
        with self.db.transaction() as s:
            if self.real_payments:
                self.runtime_policy.require_current(s, "payment_reconcile")
            event = s.get(Inbox, iid).payload
            operation = s.scalar(
                select(ProviderOperation).where(ProviderOperation.provider_id == event["payment_id"])
            )
            if operation is None:
                operation = s.scalar(
                    select(ProviderOperation).where(
                        ProviderOperation.order_id == event["order_id"],
                        ProviderOperation.provider_id.is_(None),
                        ProviderOperation.state.in_(["in_flight", "outcome_unknown"]),
                    )
                )
            if (
                not operation
                or operation.order_id != event["order_id"]
                or operation.terminal != self.terminal
            ):
                s.get(Inbox, iid).state = "review"
                return False
            operation_id = operation.id
            saved_request = operation.request
            recovery_receipt = (
                self.environment == "live" and event.get("source") == "unknown_invoice_recovery"
            )
            if self.environment == "scoped_live":
                self.runtime_policy.require_event_order_in(
                    s, s.get(Order, operation.order_id), operation=operation, provider_id=event["payment_id"]
                )
            if self.real_payments:
                order = s.get(Order, operation.order_id)
                from .panel_entitlements import owned_account

                try:
                    scoped = (
                        order is not None
                        and order.environment == self.environment
                        and isinstance(saved_request, dict)
                        and saved_request.get("scope_id") == self.runtime_policy.scope.scope_id
                        and saved_request.get("environment") == self.environment
                        and saved_request.get("provider") == "rollypay"
                        and saved_request.get("terminal") == self.terminal
                        and saved_request.get("order_id") == order.id
                        and saved_request.get("currency") == order.currency
                        and Decimal(saved_request["amount"]) == order.amount
                        and saved_request.get("payment_method") in {"crypto", "xrocket", "sbp"}
                        and owned_account(s, order, saved_request.get("account_id"))
                    )
                except (KeyError, ValueError, TypeError, ArithmeticError):
                    scoped = False
                if not scoped:
                    s.get(Inbox, iid).state = "review"
                    return False
                if recovery_receipt:
                    recovery_order = (order.account_id, order.amount, order.currency, order.environment)
        data = provider.payment(event["payment_id"])
        self._verify_payment_response(
            event, saved_request, data, strict=event.get("source") == "unknown_invoice_recovery"
        )
        if self.real_payments:
            self.runtime_policy.require("payment_reconcile")
        with self.db.transaction() as s:
            if recovery_receipt:
                self.runtime_policy.require_current(s, "payment_reconcile")
                current_order = s.get(Order, event["order_id"], with_for_update=True)
                if (
                    not current_order
                    or (
                        current_order.account_id,
                        current_order.amount,
                        current_order.currency,
                        current_order.environment,
                    )
                    != recovery_order
                ):
                    raise DomainError(
                        "payment_association_mismatch", "Связь платежа изменилась при сверке.", 400
                    )
            operation = s.scalar(
                select(ProviderOperation).where(ProviderOperation.id == operation_id).with_for_update()
            )
            if recovery_receipt:
                if (
                    not operation
                    or operation.order_id != event["order_id"]
                    or operation.terminal != self.terminal
                    or operation.state not in {"outcome_unknown", "created", "reconciled"}
                ):
                    raise DomainError(
                        "payment_association_mismatch", "Связь платежа изменилась при сверке.", 400
                    )
                if operation.request != saved_request:
                    # Callback мог добавить подтверждённый срок: новый GET проверит свежий снимок.
                    raise DomainError(
                        "payment_recovery_changed", "Снимок счёта изменился; повторяем сверку.", 503
                    )
            if operation.provider_id not in {None, data["payment_id"]}:
                raise DomainError("payment_association_mismatch", "У заказа уже есть другой платёж.", 400)
            operation.provider_id = data["payment_id"]
            operation.state = "reconciled"
            if self.real_payments and ("created_at" in data or "expires_at" in data):
                order = s.get(Order, operation.order_id)
                window = self._merchant_window(data, order)
                prior_creation = operation.request.get("live_creation", {})
                if any(
                    key in prior_creation and prior_creation[key] != value for key, value in window.items()
                ):
                    raise DomainError("payment_deadline_review", "Срок оплаты требует сверки.", 409)
                operation.request = operation.request | {
                    "live_creation": {
                        "test": False,
                        "environment": "prod",
                        "payment_id": data["payment_id"],
                        "order_id": order.id,
                        "terminal_id": self.terminal,
                        "provider": "rollypay",
                        "scope_id": self.runtime_policy.scope.scope_id,
                        **window,
                    }
                }
        if data.get("status") == "paid":
            if self.real_payments and not isinstance(data.get("paid_at"), str):
                raise DomainError("payment_time_invalid", "Не подтверждено время оплаты.", 409)
            paid_at = datetime.fromisoformat(data["paid_at"].replace("Z", "+00:00"))
            if paid_at.tzinfo is None:
                raise ValueError("Missing payment timezone")
            self.accept(
                VerifiedPayment(
                    "rollypay",
                    data["payment_id"],
                    self.terminal,
                    self.environment,
                    data["order_id"],
                    Decimal(data["amount"]),
                    data["payment_currency"],
                    paid_at,
                    Decimal(data["platform_fee_usdt"]) if data.get("platform_fee_usdt") is not None else None,
                    "USDT" if data.get("platform_fee_usdt") is not None else None,
                    Decimal(data["amount_usdt_net"]) if data.get("amount_usdt_net") is not None else None,
                )
            )
        elif data.get("status") in {"refunded", "chargeback"}:
            self.refund(
                data["payment_id"],
                "full",
                Decimal(data["amount"]),
                now(),
                kind="chargeback" if data.get("status") == "chargeback" else "refund",
            )
        elif data.get("status") in {"canceled", "expired"}:
            with self.db.transaction() as s:
                initial = s.get(Order, event["order_id"])
                a = self.commerce._account(s, initial.account_id, True, customer=False)
                o = s.scalar(select(Order).where(Order.id == initial.id).with_for_update())
                if self.real_payments:
                    self.runtime_policy.require_current(s, "financial_apply")
                if o.amount != Decimal(data["amount"]) or o.currency != data["payment_currency"]:
                    raise DomainError("payment_amount_mismatch", "Сумма не совпадает.", 400)
                if o.status == "awaiting_payment":
                    q = s.get(Quote, o.quote_id)
                    a.bonus_reserved -= Decimal(q.snapshot["bonus_amount"])
                    a.credit_reserved -= Decimal(q.snapshot.get("credit_amount", "0"))
                    o.status = data["status"]
                    promo = s.get(PromoUse, o.id)
                    if promo:
                        promo.state = "released"
        with self.db.transaction() as s:
            row = s.get(Inbox, iid)
            retry_legacy = data.get("status") == "paid" and self._legacy_recovery_in(
                s, s.get(Order, event["order_id"])
            )
            row.state = (
                "processed"
                if data.get("status") in {"paid", "refunded", "chargeback", "canceled", "expired"}
                and not retry_legacy
                else "pending"
            )
            row.attempts += 1
            if row.state == "pending":
                from datetime import timedelta

                row.available_at = now() + timedelta(seconds=30)
        return True

    def prepare_payment(self, order_id, provider):
        if self.environment == "scoped_live" and getattr(provider, "environment", None) != "scoped_live":
            raise DomainError("scoped_test_transport_denied", "Платёжное окружение не подтверждено.", 403)
        if self.real_payments:
            self.runtime_policy.require("invoice_create")
        if provider.terminal != self.terminal:
            raise DomainError("payment_context_mismatch", "Касса не совпадает с сохранённой операцией.", 400)
        with self.db.transaction() as s:
            hint = s.get(Order, order_id)
            if hint:
                self.commerce._account(s, hint.account_id, True)
            o = s.scalar(select(Order).where(Order.id == order_id).with_for_update())
            if (
                not o
                or o.environment != self.environment
                or self.environment not in {"sandbox", "live", "scoped_live"}
            ):
                raise DomainError("environment_mismatch", "Оплата недоступна для этого окружения.", 403)
            if self.real_payments:
                self.runtime_policy.require_current(s, "invoice_create")
            if self.environment == "scoped_live":
                self.runtime_policy.require_account_in(
                    s, self.commerce._account(s, o.account_id, customer=False)
                )
            if o.subscription_id:
                sub = self.commerce._sub(s, o.account_id, o.subscription_id, True)
                self.commerce._assert_subscription_operations(s, sub)
            prior = s.scalar(select(ProviderOperation).where(ProviderOperation.order_id == o.id))
            if prior:
                return {"state": prior.state, "checkout_url": self.commerce._order_dto(o)["checkout_url"]}
            quote = s.get(Quote, o.quote_id)
            if not quote or quote.expires_at <= now():
                raise DomainError("quote_expired", "Срок расчёта истёк. Создайте новый заказ.", 409)
            if self.real_payments:
                self.commerce._account(s, o.account_id)
                self.runtime_policy.require_current(s, "invoice_create")
            operation = ProviderOperation(
                order_id=o.id,
                terminal=self.terminal,
                state="in_flight",
                request={
                    "order_id": o.id,
                    "amount": str(o.amount),
                    "currency": o.currency,
                    "account_id": str(o.account_id),
                    "description": payment_description(
                        self.commerce._account(s, o.account_id, customer=False),
                        o, quote.snapshot, self.commerce.public_settings,
                    ),
                }
                | self._payment_return_urls(o),
            )
            s.add(operation)
            if quote.snapshot.get("payment_method"):
                operation.request = operation.request | {
                    "payment_method": quote.snapshot["payment_method_policy"]["provider_method"]
                }
            if self.real_payments:
                operation.request = operation.request | {
                    "environment": self.environment,
                    "provider": "rollypay",
                    "terminal": self.terminal,
                    "scope_id": self.runtime_policy.scope.scope_id,
                }
            s.flush()
            oid = operation.id
            body = operation.request
        try:
            result = provider.create_payment(
                **{
                    key: value
                    for key, value in body.items()
                    if key
                    in {
                        "order_id",
                        "amount",
                        "currency",
                        "account_id",
                        "description",
                        "payment_method",
                        "success_redirect_url",
                        "fail_redirect_url",
                    }
                }
            )
        except DomainError:
            with self.db.transaction() as s:
                s.get(ProviderOperation, oid).state = "outcome_unknown"
            raise
        with self.db.transaction() as s:
            operation = s.get(ProviderOperation, oid)
            payment_id = result.get("payment_id")
            link = self._checkout_url(result.get("pay_url"))
            from .settings import payment_context_matches

            if (
                result.get("order_id") != order_id
                or not isinstance(payment_id, str)
                or not 1 <= len(payment_id) <= 128
                or any(not (char.isalnum() or char in "-_") for char in payment_id)
                or not payment_context_matches(result, self.environment)
                or self.real_payments
                and result.get("payment_method") != body.get("payment_method")
                or self.real_payments
                and not self._live_response_matches_request(result, body)
                or link is None
            ):
                operation.state = "review"
                return {"state": "review", "checkout_url": None}
            operation.provider_id = payment_id
            operation.pay_url = link
            window = {}
            if self.real_payments:
                try:
                    window = self._merchant_window(result, s.get(Order, order_id))
                except DomainError:
                    operation.state = "review"
                    return {"state": "review", "checkout_url": None}
            operation.request = operation.request | {
                "live_creation" if self.real_payments else "sandbox_creation": {
                    "test": not self.real_payments,
                    "environment": "prod" if self.real_payments else "sandbox",
                    "payment_id": payment_id,
                    "order_id": order_id,
                    "terminal_id": self.terminal,
                    **(
                        {"provider": "rollypay", "scope_id": self.runtime_policy.scope.scope_id}
                        if self.real_payments
                        else {}
                    ),
                    **window,
                }
            }
            operation.state = "created"
            return {
                "state": "created",
                "checkout_url": self.commerce._order_dto(s.get(Order, order_id))["checkout_url"],
            }

    def _payment_return_urls(self, order):
        config = self.commerce.public_settings
        if config is None:
            return {}
        destination = getattr(config, "bot_url" if order.source == "bot" else "dashboard_url", None)
        try:
            if order.source not in {"site", "bot"} or not public_url(destination):
                raise ValueError("payment_return_invalid")
        except ValueError:
            raise DomainError("payment_return_unavailable", "Возврат после оплаты недоступен.", 409) from None
        if order.source == "site":
            parsed = urlsplit(destination)
            query = [(key, value) for key, value in parse_qsl(parsed.query) if key != "payment_order"]
            query.append(("payment_order", order.id))
            destination = urlunsplit(parsed._replace(query=urlencode(query), fragment="payments"))
        return {"success_redirect_url": destination, "fail_redirect_url": destination}

    def _checkout_url(self, value):
        return (
            checkout_url(value, self.runtime_policy.scope.rollypay.checkout_hosts)
            if self.real_payments
            else sandbox_checkout_url(value)
        )
