from datetime import datetime, timedelta
from decimal import Decimal as D

from sqlalchemy import func, select
from sunrise_core.errors import DomainError, missing
from sunrise_core.pricing import money

from .billing import Billing, VerifiedPayment
from .commerce import iso
from .db import (
    Agreement,
    Notification,
    NotificationDelivery,
    Order,
    Product,
    Quote,
    RecurringAttempt,
    RecurringCycle,
    Segment,
    Setting,
    now,
    uid,
)


class Recurring:
    def __init__(self, commerce, provider):
        self.commerce, self.db, self.provider = commerce, commerce.db, provider

    def enroll(self, account_id, sid, plan_id, confirmed, idempotency_key):
        if getattr(self.provider, "environment", None) == "live":
            raise DomainError("live_enrollment_disabled", "Новые подключения автопродления отключены.", 403)
        if not confirmed:
            raise DomainError("consent_required", "Подтвердите сумму и условия автопродления.", 400)
        if not self.provider or not self.provider.recurring_authorized:
            raise DomainError(
                "provider_capability_unavailable", "Автопродление ожидает подтверждения провайдера.", 503
            )
        with self.db.transaction() as s:
            self.commerce._sub(s, account_id, sid)
            prior = next(
                (
                    x
                    for x in s.scalars(select(Agreement).where(Agreement.subscription_id == sid))
                    if x.consent.get("idempotency_key") == idempotency_key
                ),
                None,
            )
            if prior and prior.provider_id:
                return self._dto(prior)
            enabled = self.commerce._setting(s, "recurring", {}).get("enabled")
            prior_id = prior.id if prior else None
            maintenance = self.commerce._setting(s, "maintenance", {}).get("enabled")
        if maintenance:
            if prior_id:
                return self.recover_enrollment(prior_id)
            raise DomainError(
                "maintenance", "Новые подключения автопродления приостановлены на обслуживание.", 503, True
            )
        if not enabled:
            if prior_id:
                return self.recover_enrollment(prior_id)
            raise DomainError("recurring_disabled", "Новые подключения автопродления отключены.")
        plans = self.provider.plans().get("items", [])
        plan = next((x for x in plans if x["id"] == plan_id), None)
        if not plan or plan.get("cap_amount_rub"):
            raise DomainError("plan_unavailable", "Выберите подтверждённый фиксированный план кассы.", 400)
        periods = {"month": 30, "quarter": 90, "half_year": 180, "year": 365}
        days = periods.get(plan.get("interval"))
        if not days:
            raise DomainError("plan_period_unknown", "Период плана требует проверки.", 400)
        with self.db.transaction() as s:
            a = self.commerce._account(s, account_id, True)
            sub = self.commerce._sub(s, a.id, sid, True)
            self.commerce._assert_new_operations(s)
            self.commerce._assert_subscription_operations(s, sub)
            toggle = s.scalar(select(Setting).where(Setting.key == "recurring").with_for_update())
            if not toggle or not toggle.value.get("enabled"):
                raise DomainError("recurring_disabled", "Новые подключения автопродления отключены.")
            existing = s.scalar(
                select(Agreement).where(Agreement.subscription_id == sid, Agreement.state != "stopped")
            )
            if existing:
                if existing.consent.get("idempotency_key") != idempotency_key:
                    raise DomainError("agreement_exists", "Автопродление для этой подписки уже подключается.")
                agreement_id = existing.id
                body = existing.consent["request"]
                if existing.provider_id:
                    return self._dto(existing)
            else:
                self.commerce.legal.require_current(s, a)
                product = s.get(Product, sub.product_id)
                product_periods = (product.presentation or {}).get("periods") or self.commerce._setting(
                    s, "periods", []
                )
                period = next((x for x in product_periods if x["days"] == days), None)
                plan_mapping = (product.presentation or {}).get("recurring_plan_mapping", {})
                if plan_mapping and plan_mapping.get(str(days)) != plan_id:
                    raise DomainError(
                        "plan_terms_mismatch", "План не привязан к выбранному тарифу и сроку.", 409
                    )
                if not period or product.currency != "RUB":
                    raise DomainError(
                        "plan_terms_mismatch", "План не соответствует выбранному сроку и устройствам."
                    )
                from .catalog import product_device_surcharge, product_device_terms

                amount = money(
                    (
                        product.price * (1 - D(period["discount_percent"]) / 100)
                        + product_device_surcharge(product, sub.devices_limit)
                    )
                    * D(period["months"])
                )
                if D(plan["merchant_amount_rub"]) != amount or D(plan["payer_amount_rub"]) != amount:
                    raise DomainError(
                        "plan_terms_mismatch",
                        "Цена плана требует подтверждения. Локальная цена не меняет банковское согласие.",
                    )
                new_agreement_id = uid()
                body = {
                    "plan_id": plan_id,
                    "payer_id": "a" + str(a.id),
                    "merchant_subscription_ref": new_agreement_id,
                }
                products, device_terms = {}, {}
                for p in s.scalars(select(Product).where(Product.currency == product.currency)):
                    try:
                        surcharge = product_device_surcharge(p, sub.devices_limit)
                    except DomainError:
                        continue
                    products[p.id] = str(
                        money(
                            (p.price * (1 - D(period["discount_percent"]) / 100) + surcharge)
                            * D(period["months"])
                        )
                    )
                    device_terms[p.id] = product_device_terms(p)
                terms = {
                    "idempotency_key": idempotency_key,
                    "request": body,
                    "plan": plan,
                    "period_days": days,
                    "months": period["months"],
                    "product_id": product.id,
                    "product_name": product.name,
                    "product_version": product.version,
                    "devices": sub.devices_limit,
                    "basis": {
                        "currency": product.currency,
                        "period_days": days,
                        "months": period["months"],
                        "products": products,
                        "device_pricing_included": True,
                        "device_terms": device_terms,
                    },
                    "panel_mappings": {p.id: p.internal_squad_uuid for p in s.scalars(select(Product))},
                    "panel_configuration": self.commerce._setting(s, "panel_mapping", {}),
                    "environment": sub.environment,
                    "enrollment_started_at": iso(now()),
                }
                ag = Agreement(
                    id=new_agreement_id,
                    account_id=a.id,
                    subscription_id=sid,
                    consent_amount=amount,
                    currency="RUB",
                    state="create_in_flight",
                    consent=terms,
                )
                s.add(ag)
                s.flush()
                agreement_id = ag.id
        try:
            result = self.provider.create_agreement(body, idempotency_key)
        except DomainError:
            with self.db.transaction() as s:
                s.get(Agreement, agreement_id).state = "outcome_unknown"
            raise
        with self.db.transaction() as s:
            ag = s.get(Agreement, agreement_id)
            if result.get("plan_id") != plan_id or not result.get("id"):
                ag.state = "review"
                return self._dto(ag)
            ag.provider_id = result["id"]
            ag.state = result.get("billing_status", "consent_pending")
            ag.consent = ag.consent | {"pay_url": result.get("pay_url")}
            return self._dto(ag)

    def _dto(self, ag):
        return {
            "id": ag.id,
            "provider_id": ag.provider_id,
            "state": ag.state,
            "amount": str(ag.consent_amount),
            "currency": ag.currency,
            "checkout_url": ag.consent.get("pay_url"),
            "next_charge_at": iso(ag.next_charge_at),
        }

    def recover_enrollment(self, agreement_id):
        with self.db.transaction() as s:
            ag = s.get(Agreement, agreement_id)
            terms = ag.consent
            if ag.provider_id:
                return self._dto(ag)
        if not hasattr(self.provider, "list_agreements"):
            return self._dto(ag)
        for page in range(1, 21):
            response = self.provider.list_agreements(page)
            match = next(
                (
                    x
                    for x in response.get("items", [])
                    if x.get("merchant_subscription_ref") == terms["request"]["merchant_subscription_ref"]
                    and x.get("plan_id") == terms["request"]["plan_id"]
                ),
                None,
            )
            if match:
                with self.db.transaction() as s:
                    ag = s.scalar(select(Agreement).where(Agreement.id == agreement_id).with_for_update())
                    ag.provider_id = match["id"]
                    ag.state = match.get("billing_status", "review")
                    return self._dto(ag)
            if len(response.get("items", [])) < 50:
                break
        return self._dto(ag)

    def service_cycle(self, payment, charge):
        if (
            payment.get("status") != "paid"
            or charge.get("status") != "payed"
            or charge.get("validation_status") != "accepted"
            or charge.get("payment_id") != payment.get("payment_id")
        ):
            raise DomainError("cycle_unconfirmed", "Оплата цикла ещё не подтверждена.", 409)
        if payment.get(
            "terminal_id", self.provider.terminal
        ) != self.provider.terminal or not self._payment_environment(payment):
            raise DomainError(
                "payment_context_mismatch", "Для этой проверки разрешены только синтетические циклы.", 403
            )
        with self.db.transaction() as s:
            initial = s.scalar(select(Agreement).where(Agreement.provider_id == payment["subscription_id"]))
            if not initial:
                raise missing()
            a = self.commerce._account(s, initial.account_id, True, customer=False)
            ag = s.scalar(select(Agreement).where(Agreement.id == initial.id).with_for_update())
            sub = self.commerce._sub(s, a.id, ag.subscription_id, True)
            if D(payment["amount"]) != ag.consent_amount or payment["payment_currency"] != ag.currency:
                raise DomainError("cycle_amount_mismatch", "Сумма цикла не соответствует согласию.", 400)
            existing = s.scalar(
                select(RecurringCycle).where(
                    RecurringCycle.agreement_id == ag.id,
                    RecurringCycle.cycle_number == charge["cycle_number"],
                )
            )
            if existing:
                if existing.provider_payment_id != payment["payment_id"]:
                    raise DomainError("cycle_duplicate_payment", "Повторный платёж за цикл требует сверки.")
                oid = existing.order_id
            else:
                self._require_existing(ag, session=s)
                if getattr(self.provider, "environment", None) == "live":
                    self.commerce.runtime_policy.require_current(s, "financial_apply")
                terms = ag.consent
                start = max(now(), sub.expires_at or now())
                end = start + timedelta(days=terms["period_days"])
                snapshot = {
                    "action": "renew",
                    "product_id": terms["product_id"],
                    "product_name": terms["product_name"],
                    "product_version": terms["product_version"],
                    "subscription_id": sub.id,
                    "base_amount": str(ag.consent_amount),
                    "discount_amount": "0.00",
                    "bonus_amount": "0.00",
                    "total": str(ag.consent_amount),
                    "currency": ag.currency,
                    "resulting_expires_at": iso(end),
                    "starts_at": iso(start),
                    "devices_limit": terms["devices"],
                    "version": sub.version,
                    "basis": terms["basis"],
                    "panel_mappings": terms.get("panel_mappings", {}),
                    "panel_configuration": terms.get("panel_configuration", {}),
                    "breakdown": [],
                }
                quote = Quote(
                    account_id=a.id,
                    subscription_id=sub.id,
                    snapshot=snapshot,
                    expires_at=now() + timedelta(days=1),
                )
                s.add(quote)
                s.flush()
                order = Order(
                    account_id=a.id,
                    quote_id=quote.id,
                    idempotency_key="cycle:" + ag.id + ":" + str(charge["cycle_number"]),
                    amount=ag.consent_amount,
                    currency=ag.currency,
                    subscription_id=sub.id,
                    source="recurring",
                    environment=terms["environment"],
                )
                s.add(order)
                s.flush()
                oid = order.id
                s.add(
                    RecurringCycle(
                        agreement_id=ag.id,
                        cycle_number=charge["cycle_number"],
                        order_id=oid,
                        provider_payment_id=payment["payment_id"],
                    )
                )
            environment = ag.consent["environment"]
        paid_at = datetime.fromisoformat(payment["paid_at"].replace("Z", "+00:00"))
        if paid_at.tzinfo is None:
            raise DomainError("payment_time_invalid", "Не подтверждено время платежа.", 400)
        result = Billing(self.commerce, self.provider.terminal, environment).accept(
            VerifiedPayment(
                "rollypay",
                payment["payment_id"],
                self.provider.terminal,
                environment,
                oid,
                D(payment["amount"]),
                payment["payment_currency"],
                paid_at,
            )
        )
        with self.db.transaction() as s:
            cycle = s.scalar(select(RecurringCycle).where(RecurringCycle.order_id == oid))
            cycle.state = "paid" if result["status"] == "paid" else "review"
            ag = s.get(Agreement, initial.id)
            if result["status"] == "paid" and not ag.consent.get("continuous_paid_since"):
                ag.consent = ag.consent | {"continuous_paid_since": iso(paid_at)}
            if result["status"] == "paid":
                decline = s.scalar(
                    select(Notification).where(
                        Notification.account_id == ag.account_id,
                        Notification.event_key == f"recurring_failed:{ag.id}:{charge['cycle_number']}",
                    )
                )
                if decline:
                    for delivery in s.scalars(
                        select(NotificationDelivery)
                        .where(
                            NotificationDelivery.notification_id == decline.id,
                            NotificationDelivery.state == "pending",
                        )
                        .with_for_update()
                    ):
                        delivery.state, delivery.last_error = "cancelled", "recurring_cycle_paid"
        return result

    def cancel(self, account_id, sid):
        if getattr(self.provider, "environment", None) == "live":
            with self.db.transaction() as session:
                sub = self.commerce._sub(session, account_id, sid)
                agreement = session.scalar(
                    select(Agreement).where(Agreement.subscription_id == sub.id, Agreement.state != "stopped")
                )
                self._require_existing(agreement, capability="existing_recurring_cancel", session=session)
        result = self.commerce.cancel_agreement(account_id, sid)
        with self.db.transaction() as s:
            ag = s.get(Agreement, result["id"])
            provider_id = ag.provider_id
        if not provider_id:
            return result
        verified = self.provider.stop_agreement(provider_id)
        with self.db.transaction() as s:
            ag = s.get(Agreement, result["id"])
            self._require_existing(ag, capability="existing_recurring_cancel", session=s)
            if verified.get("state") == "stop" and verified.get("billing_status") == "stopped":
                ag.state = "stopped"
            return self._dto(ag)

    def _payment_environment(self, data):
        if getattr(self.provider, "environment", None) == "live":
            from .settings import payment_context_matches

            return payment_context_matches(data, "live")
        return data.get("test") is True

    def _require_existing(self, agreement, *, capability="existing_recurring_status", session=None):
        if getattr(self.provider, "environment", None) != "live":
            return
        policy = self.commerce.runtime_policy
        terms = agreement.consent if agreement and isinstance(agreement.consent, dict) else {}
        if (
            terms.get("environment") != "live"
            or terms.get("provider") != "rollypay"
            or terms.get("terminal") != self.provider.terminal
            or terms.get("scope_id") != policy.scope.scope_id
        ):
            raise DomainError(
                "agreement_scope_unverified", "Существующее согласие требует проверки провайдера.", 409
            )
        if session is not None:
            policy.require_current(session, capability)
        else:
            policy.require(capability)

    def loyalty(self, sid):
        with self.db.transaction() as s:
            ag = s.scalar(
                select(Agreement)
                .where(Agreement.subscription_id == sid)
                .order_by((Agreement.state == "stopped").asc(), Agreement.created_at.desc())
                .limit(1)
            )
            start = ag.consent.get("continuous_paid_since") if ag else None
            if not start:
                return {"percent": "0", "next_milestone_at": None, "applied_to_external_amount": False}
            first = datetime.fromisoformat(start)
            cursor = first
            moment = now()
            segments = list(
                s.scalars(
                    select(Segment)
                    .where(Segment.subscription_id == sid, Segment.kind == "paid", Segment.ends_at > first)
                    .order_by(Segment.starts_at)
                )
            )
            for segment in segments:
                if segment.starts_at > moment:
                    break
                if segment.starts_at > cursor:
                    first = segment.starts_at
                cursor = max(cursor, segment.ends_at)
            if cursor < moment:
                return {"percent": "0", "next_milestone_at": None, "applied_to_external_amount": False}
            elapsed = max(0, (moment - first).total_seconds())
            policy = self.commerce._setting(s, "discounts", {})
            period_days = policy.get("loyalty_period_days")
            configured = policy.get("loyalty_milestones")
            if not period_days or configured is None:
                return {
                    "percent": "0",
                    "next_milestone_at": None,
                    "applied_to_external_amount": False,
                    "policy_configured": False,
                }
            milestones = sorted((x["elapsed_periods"] * period_days, x["percent"]) for x in configured)
            percent = "0"
            next_date = None
            for days, value in milestones:
                if elapsed >= days * 86400:
                    percent = value
                elif next_date is None:
                    next_date = iso(first + timedelta(days=days))
            return {
                "percent": percent,
                "policy_configured": True,
                "period_days": period_days,
                "period_definition": "fixed_elapsed_paid_days",
                "elapsed_paid_days": str(D(str(elapsed)) / D(86400)),
                "next_milestone_at": next_date,
                "applied_to_external_amount": False,
                "supported_path": "manual_renewal_after_verified_stop",
            }

    def reconcile(self, limit=50):
        import hashlib
        import json

        with self.db.transaction() as s:
            query = select(Agreement).where(Agreement.state != "stopped")
            if getattr(self.provider, "environment", None) == "live":
                for field, expected in {
                    "environment": "live",
                    "provider": "rollypay",
                    "terminal": self.provider.terminal,
                    "scope_id": self.commerce.runtime_policy.scope.scope_id,
                }.items():
                    query = query.where(
                        func.json_typeof(Agreement.consent[field]) == "string",
                        Agreement.consent[field].as_string() == expected,
                    )
            agreements = list(
                s.scalars(
                    query.order_by(
                        Agreement.consent["last_checked_at"].as_string().asc().nulls_first(),
                        Agreement.created_at.asc(),
                    )
                    .limit(limit)
                )
            )
        for ag in agreements:
            try:
                capability = (
                    "existing_recurring_cancel" if ag.state == "stop_pending" else "existing_recurring_status"
                )
                self._require_existing(ag, capability=capability)
                with self.db.transaction() as s:
                    row = s.get(Agreement, ag.id)
                    row.consent = row.consent | {"last_checked_at": iso(now())}
                if not ag.provider_id:
                    self.recover_enrollment(ag.id)
                    continue
                remote = (
                    self.provider.stop_agreement(ag.provider_id)
                    if ag.state == "stop_pending"
                    else self.provider.agreement(ag.provider_id)
                )
                state = remote.get("billing_status")
                if state not in {
                    "consent_pending",
                    "pending",
                    "enabled",
                    "review",
                    "stop_pending",
                    "stopped",
                }:
                    continue
                with self.db.transaction() as s:
                    current = s.get(Agreement, ag.id)
                    self._require_existing(current, capability=capability, session=s)
                    current.state = state
                    if remote.get("next_charge_at"):
                        current.next_charge_at = datetime.fromisoformat(
                            remote["next_charge_at"].replace("Z", "+00:00")
                        )
                if not hasattr(self.provider, "charges"):
                    continue
                if (
                    getattr(self.provider, "environment", None) == "live"
                    and capability == "existing_recurring_cancel"
                ):
                    continue
                response = self.provider.charges(ag.provider_id)
                charges = response if isinstance(response, list) else response.get("items", [])
                # Отказ из истории не описывает текущий результат, если в том же цикле
                # уже есть оплаченная, проверяемая или неопределённая попытка.
                unsettled_cycles = {
                    x.get("cycle_number")
                    for x in charges
                    if type(x.get("cycle_number")) is int
                    and (x.get("status") != "fail" or x.get("validation_status") != "accepted")
                }
                for charge in charges:
                    cycle = charge.get("cycle_number")
                    if type(cycle) is not int or cycle < 0:
                        continue
                    key = hashlib.sha256(
                        json.dumps({"agreement": ag.id, "charge": charge}, sort_keys=True).encode()
                    ).hexdigest()
                    raw_status, raw_validation = charge.get("status"), charge.get("validation_status")
                    charge_status = (
                        raw_status
                        if isinstance(raw_status, str)
                        and raw_status
                        in {
                            "payed",
                            "fail",
                            "stop",
                            "pending",
                        }
                        else "unknown"
                    )
                    validation = (
                        raw_validation
                        if isinstance(raw_validation, str)
                        and raw_validation
                        in {
                            "accepted",
                            "review",
                        }
                        else "unknown"
                    )
                    status = "review" if validation == "review" else charge_status
                    with self.db.transaction() as s:
                        account = self.commerce._account(s, ag.account_id, True, customer=False)
                        current = s.scalar(select(Agreement).where(Agreement.id == ag.id).with_for_update())
                        self._require_existing(current, session=s)
                        occurred_at = self._attempt_time(charge.get("actual_at"))
                        if not s.get(RecurringAttempt, key):
                            s.add(
                                RecurringAttempt(
                                    id=key,
                                    agreement_id=ag.id,
                                    cycle_number=cycle,
                                    state=status,
                                    occurred_at=occurred_at,
                                )
                            )
                        evidence_key = "rc_attempt:" + key  # VARCHAR(80): 11-char prefix + SHA256(64).
                        if not s.get(Setting, evidence_key):
                            evidence = {
                                "account_id": str(account.id),
                                "agreement_id": ag.id,
                                "cycle_number": cycle,
                                "status": charge_status,
                                "validation_status": validation,
                                "actual_at": iso(occurred_at),
                                "scheduled_at": iso(self._attempt_time(charge.get("scheduled_at"))),
                            }
                            for field in ("amount_rub", "expected_amount_rub"):
                                try:
                                    value = D(str(charge.get(field)))
                                    if value.is_finite() and 0 <= value <= D("1e18"):
                                        evidence[field] = str(value)
                                except (ArithmeticError, ValueError):
                                    pass
                            payment_id = charge.get("payment_id")
                            if (
                                isinstance(payment_id, str)
                                and len(payment_id) <= 128
                                and all(c.isalnum() or c in "-_" for c in payment_id)
                            ):
                                evidence["payment_id"] = payment_id
                            s.add(Setting(key=evidence_key, value=evidence))
                        recorded_cycle = s.scalar(
                            select(RecurringCycle).where(
                                RecurringCycle.agreement_id == ag.id,
                                RecurringCycle.cycle_number == cycle,
                            )
                        )
                        if (
                            status == "fail"
                            and charge.get("validation_status") == "accepted"
                            and cycle not in unsettled_cycles
                            and not recorded_cycle
                            and current.state == "enabled"
                            and remote.get("id") == ag.provider_id
                            and self.commerce.public_settings is not None
                        ):
                            from .notifications import NotificationService

                            terms = current.consent
                            product = terms.get("product_name")
                            name = (
                                " ".join(product.split())
                                if isinstance(product, str) and product
                                else "Название отсутствует в сохранённых условиях"
                            )
                            period = terms.get("period_days")
                            period_label = (
                                f"{period} дней"
                                if type(period) is int and period > 0
                                else "не указан в сохранённых условиях"
                            )
                            NotificationService(self.db, self.commerce.public_settings).emit_in(
                                s,
                                account.id,
                                f"recurring_failed:{ag.id}:{cycle}",
                                "recurring_failed",
                                "Автопродление не прошло",
                                f"Тариф: {name}\nПодписка: {current.subscription_id}\n"
                                f"Период: {period_label}\nСогласованная сумма: {current.consent_amount:.2f} {current.currency}\n\n"
                                "Провайдер подтвердил отказ при попытке автопродления. Ранее оплаченный срок сохранён.\n"
                                "Если на банковском счёте недостаточно средств, пополните его. "
                                "Повторное списание по кнопке пока недоступно. Проверьте состояние автопродления в подписках.",
                                target="/#subscriptions",
                                actions=[{"label": "Проверить статус", "target": "/#subscriptions"}],
                            )
                    if (
                        status == "payed"
                        and charge.get("validation_status") == "accepted"
                        and charge.get("payment_id")
                    ):
                        self.service_cycle(self.provider.payment(charge["payment_id"]), charge)
            except DomainError as failure:
                if getattr(self.provider, "environment", None) == "live" and failure.code in {
                    "production_stage_denied",
                    "production_admission_denied",
                    "agreement_scope_unverified",
                }:
                    continue
                with self.db.transaction() as s:
                    s.get(Agreement, ag.id).state = "review"

    @staticmethod
    def _attempt_time(value):
        if not isinstance(value, str) or len(value) > 64:
            return None
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return result if result.tzinfo is not None else None
        except ValueError:
            return None
