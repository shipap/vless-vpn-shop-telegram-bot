"""Сверка поздней комиссии читает только известный синтетический платёж."""

from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sunrise_core.errors import DomainError
from sunrise_infrastructure.billing import Billing
from sunrise_infrastructure.db import (
    Audit,
    Inbox,
    Job,
    Ledger,
    Order,
    Payment,
    Product,
    ProviderOperation,
    Quote,
    Setting,
    Settlement,
    now,
)

from tests.integration.test_payment_pipeline import enqueue
from tests.integration.test_payment_pipeline import pipeline as pipeline


def confirmed(pipeline):
    commerce, billing, account, order, provider = pipeline
    billing.prepare_payment(order["id"], provider)
    enqueue(commerce, order)
    assert billing.reconcile_inbox(provider) == 1
    with commerce.db.transaction() as session:
        payment = session.scalar(select(Payment).where(Payment.order_id == order["id"]))
        return commerce, billing, account, order, provider, payment.id


def refresh(billing, provider, **kwargs):
    return billing.refresh_missing_settlements(provider, interval_seconds=300, limit=kwargs.get("limit", 10))


def test_expired_quote_never_creates_new_provider_invoice(pipeline):
    commerce, billing, _, order, provider = pipeline
    with commerce.db.transaction() as session:
        session.get(Quote, order["quote_id"]).expires_at = now() - timedelta(seconds=1)
    with pytest.raises(DomainError, match="истёк") as error:
        billing.prepare_payment(order["id"], provider)
    assert error.value.code == "quote_expired"
    assert provider.payments == {}
    with commerce.db.transaction() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(ProviderOperation)
                .where(ProviderOperation.order_id == order["id"])
            )
            == 0
        )


def test_expired_quote_keeps_prior_unknown_operation_and_never_recreates(pipeline):
    commerce, billing, _, order, provider = pipeline
    provider.timeout = True
    with pytest.raises(DomainError):
        billing.prepare_payment(order["id"], provider)
    with commerce.db.transaction() as session:
        session.get(Quote, order["quote_id"]).expires_at = now() - timedelta(seconds=1)
    assert billing.prepare_payment(order["id"], provider)["state"] == "outcome_unknown"
    assert len(provider.payments) == 1


def test_internal_settlement_review_is_discoverable_with_verified_order(pipeline):
    from types import SimpleNamespace

    from sunrise_infrastructure.operational_monitor import OperationalMonitor

    commerce, _, _, order, _, pid = confirmed(pipeline)
    with commerce.db.transaction() as session:
        session.add(
            Inbox(
                event_key="settlement-review:" + str(uuid4()),
                state="review",
                payload={
                    "source": "settlement_refresh",
                    "reason": "settlement_payment_status_review",
                    "order_id": order["id"],
                    "payment_id": pid,
                },
            )
        )
    snapshot = OperationalMonitor(commerce.db, SimpleNamespace()).snapshot()
    review = next(item for item in snapshot["items"] if item["kind"] == "payment_event")
    assert review["order_id"] == order["id"]


@pytest.mark.parametrize("mismatch", ["order", "payment", "provider", "terminal", "amount"])
def test_internal_review_never_links_unverified_payload_order(pipeline, mismatch):
    from types import SimpleNamespace

    from sunrise_infrastructure.operational_monitor import OperationalMonitor

    commerce, _, _, order, _, pid = confirmed(pipeline)
    payload = {
        "source": "settlement_refresh",
        "reason": "settlement_payment_status_review",
        "order_id": order["id"],
        "payment_id": pid,
    }
    with commerce.db.transaction() as session:
        if mismatch == "order":
            payload["order_id"] = str(uuid4())
        elif mismatch == "payment":
            payload["payment_id"] = str(uuid4())
        else:
            payment = session.get(Payment, pid)
            setattr(payment, mismatch, Decimal("999") if mismatch == "amount" else "foreign")
        session.add(Inbox(event_key=str(uuid4()), state="review", payload=payload))
    items = OperationalMonitor(commerce.db, SimpleNamespace()).snapshot()["items"]
    review = next(item for item in items if item["kind"] == "payment_event")
    assert review["order_id"] is None


def test_settlement_rechecks_binding_after_provider_http(pipeline):
    commerce, billing, _, order, provider, pid = confirmed(pipeline)
    original_lookup = provider.payment

    def lookup(identifier):
        result = original_lookup(identifier) | {"platform_fee_usdt": "0.48", "amount_usdt_net": "5.50"}
        with commerce.db.transaction() as session:
            operation = session.scalar(
                select(ProviderOperation).where(ProviderOperation.order_id == order["id"])
            )
            operation.provider_id = "changed-during-http"
        return result

    provider.payment = lookup
    assert refresh(billing, provider) == 1
    with commerce.db.transaction() as session:
        assert session.get(Payment, pid).fee is None
        assert session.get(Setting, billing._settlement_refresh_key(pid)).value["state"] == "review"


def test_newer_callback_settlement_during_get_cannot_be_overwritten(pipeline):
    from sunrise_infrastructure.billing import VerifiedPayment

    commerce, billing, _, order, provider, pid = confirmed(pipeline)
    original_lookup = provider.payment

    def lookup(identifier):
        stale_response = original_lookup(identifier) | {
            "platform_fee_usdt": "0.48",
            "amount_usdt_net": "5.50",
        }
        with commerce.db.transaction() as session:
            payment = session.get(Payment, pid)
            fact = VerifiedPayment(
                payment.provider,
                payment.provider_id,
                payment.terminal,
                payment.environment,
                payment.order_id,
                payment.amount,
                payment.currency,
                payment.paid_at,
                Decimal("0.50"),
                "USDT",
                Decimal("5.48"),
            )
        billing.accept(fact)
        return stale_response

    provider.payment = lookup
    assert refresh(billing, provider) == 1
    with commerce.db.transaction() as session:
        payment = session.get(Payment, pid)
        assert (payment.fee, payment.net_usdt) == (Decimal("0.50"), Decimal("5.48"))
        assert (
            session.scalar(select(func.count()).select_from(Settlement).where(Settlement.payment_id == pid))
            == 1
        )


def test_partial_settlement_repeated_observation_does_not_duplicate_ledger(pipeline):
    commerce, billing, _, order, provider, pid = confirmed(pipeline)
    provider.payments["test-" + order["id"]]["platform_fee_usdt"] = "0.48"
    assert refresh(billing, provider) == 1
    with commerce.db.transaction() as session:
        marker = session.get(Setting, billing._settlement_refresh_key(pid))
        marker.value = marker.value | {"next_check_at": (now() - timedelta(seconds=1)).isoformat()}
    assert refresh(billing, provider) == 1
    with commerce.db.transaction() as session:
        assert (
            session.scalar(select(func.count()).select_from(Settlement).where(Settlement.payment_id == pid))
            == 1
        )


def test_settlement_refresh_uses_only_verified_known_payment_get(pipeline):
    import httpx
    from sunrise_infrastructure.providers import RollyPay

    commerce, billing, _, order, synthetic, pid = confirmed(pipeline)
    requests = []
    response = synthetic.payments["test-" + order["id"]] | {
        "platform_fee_usdt": "0.48",
        "amount_usdt_net": "5.50",
    }

    def transport(request):
        requests.append((request.method, request.url.path))
        return httpx.Response(200, json=response)

    provider = RollyPay(
        "https://payments.example.test",
        "synthetic",
        billing.terminal,
        {"payments.example.test"},
        transport=httpx.MockTransport(transport),
    )
    try:
        assert refresh(billing, provider) == 1
    finally:
        provider.client.close()
    assert requests == [("GET", "/api/v1/payments/test-" + order["id"])]
    with commerce.db.transaction() as session:
        assert session.get(Payment, pid).net_usdt == Decimal("5.50")


def test_settlement_transport_unknown_reschedules_redacted_without_new_invoice(pipeline):
    commerce, billing, _, order, provider, pid = confirmed(pipeline)

    def unavailable(identifier):
        raise DomainError("arbitrary-private-provider-text", "private diagnostic", 503)

    provider.payment = unavailable
    assert refresh(billing, provider) == 1
    assert refresh(billing, provider) == 0
    with commerce.db.transaction() as session:
        marker = session.get(Setting, billing._settlement_refresh_key(pid))
        assert marker.value["state"] == "pending"
        assert marker.value["last_error"] == "provider_unavailable"
        assert "private" not in repr(marker.value)
        assert (
            session.scalar(
                select(func.count())
                .select_from(ProviderOperation)
                .where(ProviderOperation.order_id == order["id"])
            )
            == 1
        )
        assert session.scalar(select(func.count()).select_from(Inbox).where(Inbox.state == "review")) == 0


@pytest.mark.parametrize("valid_proof", [True, False])
def test_missing_test_flag_requires_exact_saved_sandbox_proof(pipeline, valid_proof):
    commerce, billing, _, order, provider, pid = confirmed(pipeline)
    response = provider.payments["test-" + order["id"]]
    response.pop("test")
    response.update(platform_fee_usdt="0.48", amount_usdt_net="5.50")
    if not valid_proof:
        with commerce.db.transaction() as session:
            operation = session.scalar(
                select(ProviderOperation).where(ProviderOperation.order_id == order["id"])
            )
            request = dict(operation.request)
            request.pop("sandbox_creation")
            operation.request = request
    assert refresh(billing, provider) == 1
    with commerce.db.transaction() as session:
        payment = session.get(Payment, pid)
        assert payment.fee == (Decimal("0.48") if valid_proof else None)
        assert session.get(Setting, billing._settlement_refresh_key(pid)).value["state"] == (
            "completed" if valid_proof else "review"
        )


def test_late_settlement_appends_once_without_reissuing_paid_rights(pipeline):
    commerce, billing, _, order, provider, pid = confirmed(pipeline)
    before = {}
    with commerce.db.transaction() as session:
        before = {
            model: session.scalar(select(func.count()).select_from(model)) for model in (Job, Ledger, Payment)
        }
        original = session.get(Payment, pid)
        facts = (original.amount, original.currency, original.paid_at, original.order_id)
    provider.payments["test-" + order["id"]].update(platform_fee_usdt="0.48", amount_usdt_net="5.50")
    assert refresh(billing, provider) == 1
    assert refresh(billing, provider) == 0
    with commerce.db.transaction() as session:
        payment = session.get(Payment, pid)
        assert (payment.fee, payment.fee_currency, payment.net_usdt) == (
            Decimal("0.48"),
            "USDT",
            Decimal("5.50"),
        )
        assert (payment.amount, payment.currency, payment.paid_at, payment.order_id) == facts
        assert (
            session.scalar(select(func.count()).select_from(Settlement).where(Settlement.payment_id == pid))
            == 1
        )
        assert all(
            session.scalar(select(func.count()).select_from(model)) == count
            for model, count in before.items()
        )


def test_missing_late_fields_preserve_verified_fee_and_reschedule_without_duplicates(pipeline):
    commerce, billing, _, order, provider, pid = confirmed(pipeline)
    with commerce.db.transaction() as session:
        payment = session.get(Payment, pid)
        payment.fee, payment.fee_currency = Decimal("0.48"), "USDT"
    assert refresh(billing, provider) == 1
    assert refresh(billing, provider) == 0
    with commerce.db.transaction() as session:
        payment = session.get(Payment, pid)
        assert (payment.fee, payment.fee_currency, payment.net_usdt) == (Decimal("0.48"), "USDT", None)
        marker = session.get(Setting, billing._settlement_refresh_key(pid))
        assert marker.value["state"] == "pending" and marker.value["next_check_at"] > now().isoformat()
        assert (
            session.scalar(select(func.count()).select_from(Settlement).where(Settlement.payment_id == pid))
            == 0
        )


def test_known_fee_without_verified_currency_still_requires_refresh(pipeline):
    commerce, billing, _, order, provider, pid = confirmed(pipeline)
    with commerce.db.transaction() as session:
        payment = session.get(Payment, pid)
        payment.fee, payment.fee_currency, payment.net_usdt = Decimal("0.48"), None, Decimal("5.50")
    provider.payments["test-" + order["id"]].update(platform_fee_usdt="0.48", amount_usdt_net="5.50")
    assert refresh(billing, provider) == 1
    with commerce.db.transaction() as session:
        assert session.get(Payment, pid).fee_currency == "USDT"


def test_unknown_fee_units_are_audited_without_subtraction_and_replay_is_safe(pipeline):
    from sunrise_infrastructure.billing import VerifiedPayment

    commerce, billing, _, order, provider, pid = confirmed(pipeline)
    with commerce.db.transaction() as session:
        payment = session.get(Payment, pid)
        payment.fee, payment.fee_currency = Decimal("1000"), None
        original_facts = (
            payment.provider,
            payment.provider_id,
            payment.terminal,
            payment.environment,
            payment.order_id,
            payment.amount,
            payment.currency,
            payment.paid_at,
        )
    provider.payments["test-" + order["id"]].update(platform_fee_usdt="0.48", amount_usdt_net="5.50")
    assert refresh(billing, provider) == 1
    with commerce.db.transaction() as session:
        snapshot = session.scalar(select(Settlement).where(Settlement.payment_id == pid))
        assert snapshot.fee == Decimal("0.48") and snapshot.fee_currency == "USDT"
        audit = session.scalar(
            select(Audit).where(Audit.target == pid, Audit.action == "settlement_currency_verified")
        )
        assert audit.details["before"] == {"fee": "1000.00000000", "fee_currency": None}
    billing.accept(VerifiedPayment(*original_facts, Decimal("0.50"), "USDT", Decimal("5.48")))
    billing.accept(VerifiedPayment(*original_facts, Decimal("0.48"), "USDT", Decimal("5.50")))
    with commerce.db.transaction() as session:
        payment = session.get(Payment, pid)
        assert (payment.fee, payment.net_usdt) == (Decimal("0.50"), Decimal("5.48"))
        assert (
            session.scalar(select(func.count()).select_from(Settlement).where(Settlement.payment_id == pid))
            == 2
        )
        assert session.scalar(select(func.count()).select_from(Audit).where(Audit.target == pid)) == 1


def test_known_different_fee_currency_is_never_reinterpreted(pipeline):
    commerce, billing, _, order, provider, pid = confirmed(pipeline)
    with commerce.db.transaction() as session:
        payment = session.get(Payment, pid)
        payment.fee, payment.fee_currency = Decimal("10"), "RUB"
    provider.payments["test-" + order["id"]].update(platform_fee_usdt="0.48", amount_usdt_net="5.50")
    assert refresh(billing, provider) == 1
    with commerce.db.transaction() as session:
        payment = session.get(Payment, pid)
        assert (payment.fee, payment.fee_currency, payment.net_usdt) == (Decimal("10"), "RUB", None)
        assert (
            session.scalar(select(func.count()).select_from(Settlement).where(Settlement.payment_id == pid))
            == 0
        )
        assert session.get(Setting, billing._settlement_refresh_key(pid)).value["state"] == "review"


def test_maximum_payment_id_keeps_marker_bounded_without_changing_identity(pipeline):
    commerce, billing, _, order, provider, pid = confirmed(pipeline)
    maximum_id = "synthetic-payment-" + "x" * (64 - len("synthetic-payment-"))
    with commerce.db.transaction() as session:
        session.get(Payment, pid).id = maximum_id
    provider.payments["test-" + order["id"]].update(platform_fee_usdt="0.48", amount_usdt_net="5.50")
    assert refresh(billing, provider) == 1
    with commerce.db.transaction() as session:
        assert session.get(Payment, maximum_id).fee == Decimal("0.48")
        markers = list(
            session.scalars(select(Setting).where(Setting.value["state"].as_string() == "completed"))
        )
        assert len(markers) == 1 and len(markers[0].key) <= 80


@pytest.mark.parametrize(
    "change",
    [
        {"terminal_id": "other"},
        {"environment": "live"},
        {"customer_id": "other"},
        {"amount": "999"},
        {"payment_currency": "EUR"},
        {"order_id": "other"},
        {"payment_id": "other"},
        {"test": False},
        {"amount": "not-a-number"},
        {"terminal_id": None},
        {"environment": None},
        {"customer_id": None},
    ],
)
def test_settlement_response_mismatch_is_durable_review_without_financial_effects(pipeline, change):
    commerce, billing, _, order, provider, pid = confirmed(pipeline)
    provider.payments["test-" + order["id"]].update(
        change | {"platform_fee_usdt": "0.48", "amount_usdt_net": "5.50"}
    )
    assert refresh(billing, provider) == 1
    with commerce.db.transaction() as session:
        payment = session.get(Payment, pid)
        assert payment.fee is None and payment.net_usdt is None
        assert session.get(Order, order["id"]).status == "paid"
        review = session.scalar(select(Inbox).where(Inbox.state == "review"))
        assert review.payload["source"] == "settlement_refresh"
        assert review.payload["order_id"] == order["id"]
        assert set(review.payload) == {"source", "reason", "order_id", "payment_id"}
        assert session.get(Setting, billing._settlement_refresh_key(pid)).value["state"] == "review"


@pytest.mark.parametrize("field", ["platform_fee_usdt", "amount_usdt_net"])
def test_boolean_is_not_a_verified_settlement_money_amount(pipeline, field):
    commerce, billing, _, order, provider, pid = confirmed(pipeline)
    provider.payments["test-" + order["id"]][field] = True
    assert refresh(billing, provider) == 1
    with commerce.db.transaction() as session:
        payment = session.get(Payment, pid)
        assert payment.fee is None and payment.net_usdt is None
        assert session.get(Setting, billing._settlement_refresh_key(pid)).value["state"] == "review"


def test_strict_payment_amount_boolean_cannot_equal_one_ruble():
    from types import SimpleNamespace

    billing = Billing(SimpleNamespace(db=None), "synthetic-terminal", "sandbox")
    event = {"payment_id": "synthetic-payment", "order_id": "synthetic-order"}
    request = {"amount": "1.00", "currency": "RUB", "account_id": "1"}
    response = event | {
        "amount": True,
        "payment_currency": "RUB",
        "terminal_id": billing.terminal,
        "environment": "sandbox",
        "test": True,
        "customer_id": "1",
    }
    with pytest.raises(DomainError) as error:
        billing._verify_payment_response(event, request, response, strict=True)
    assert error.value.code == "payment_amount_mismatch"


def test_boolean_one_ruble_get_is_reviewed_without_recording_fee(pipeline):
    commerce, billing, aid, _, provider = pipeline
    with commerce.db.transaction() as session:
        product = session.scalar(select(Product).where(Product.code == "standard"))
        product.price = Decimal("1.00")
        product.version += 1
    quote = commerce.quote(aid, "standard", 30, "purchase")
    order = commerce.create_order(aid, quote["id"], str(uuid4()))
    with commerce.db.transaction() as session:
        assert session.get(Order, order["id"]).amount == Decimal("1.00")
    billing.prepare_payment(order["id"], provider)
    enqueue(commerce, order)
    assert billing.reconcile_inbox(provider) == 1
    provider.payments["test-" + order["id"]].update(
        amount=True, platform_fee_usdt="0.48", amount_usdt_net="0.50"
    )
    assert refresh(billing, provider) == 1
    with commerce.db.transaction() as session:
        payment = session.scalar(select(Payment).where(Payment.order_id == order["id"]))
        assert payment.amount == Decimal("1.00") and payment.fee is None and payment.net_usdt is None
        assert session.get(Setting, billing._settlement_refresh_key(payment.id)).value["state"] == "review"


@pytest.mark.parametrize("field", ["platform_fee_usdt", "amount_usdt_net"])
def test_negative_cumulative_settlement_is_reviewed(pipeline, field):
    commerce, billing, _, order, provider, pid = confirmed(pipeline)
    provider.payments["test-" + order["id"]][field] = "-1.00"
    assert refresh(billing, provider) == 1
    with commerce.db.transaction() as session:
        payment = session.get(Payment, pid)
        assert payment.fee is None and payment.net_usdt is None
        assert session.get(Setting, billing._settlement_refresh_key(pid)).value["state"] == "review"


@pytest.mark.parametrize("status", ["refunded", "chargeback", "canceled", "processing"])
def test_nonpaid_confirmed_payment_is_reviewed_without_running_refund(pipeline, status):
    commerce, billing, _, order, provider, pid = confirmed(pipeline)
    provider.payments["test-" + order["id"]]["status"] = status
    assert refresh(billing, provider) == 1
    with commerce.db.transaction() as session:
        payment = session.get(Payment, pid)
        assert payment.status == "paid" and payment.fee is None
        row = session.scalar(select(Inbox).where(Inbox.state == "review"))
        assert row.payload["reason"] == "settlement_payment_status_review"


def test_due_batches_are_fair_and_provider_http_has_no_open_database_transaction(pipeline):
    commerce, billing, _, order, provider, pid = confirmed(pipeline)
    with commerce.db.transaction() as session:
        original = session.get(Payment, pid)
        original_quote = session.get(Quote, order["quote_id"])
        second_quote = Quote(
            account_id=original_quote.account_id,
            snapshot=dict(original_quote.snapshot),
            expires_at=original_quote.expires_at,
        )
        session.add(second_quote)
        session.flush()
        second_order = Order(
            account_id=original.account_id,
            quote_id=second_quote.id,
            idempotency_key=str(uuid4()),
            amount=original.amount,
            currency=original.currency,
            environment="sandbox",
            status="paid",
        )
        session.add(second_order)
        session.flush()
        second = Payment(
            provider="rollypay",
            provider_id="second-safe",
            terminal=billing.terminal,
            environment="sandbox",
            order_id=second_order.id,
            account_id=original.account_id,
            amount=original.amount,
            currency=original.currency,
            paid_at=original.paid_at,
        )
        session.add(second)
        session.add(
            ProviderOperation(
                order_id=second_order.id,
                terminal=billing.terminal,
                state="reconciled",
                provider_id="second-safe",
                request={
                    "order_id": second_order.id,
                    "amount": str(original.amount),
                    "currency": original.currency,
                    "account_id": str(original.account_id),
                },
            )
        )
        provider.payments["second-safe"] = provider.payments["test-" + order["id"]] | {
            "payment_id": "second-safe",
            "order_id": second_order.id,
        }
    calls = []
    original_lookup = provider.payment
    original_transaction = commerce.db.transaction
    active_transactions = 0

    @contextmanager
    def tracked_transaction():
        nonlocal active_transactions
        with original_transaction() as session:
            active_transactions += 1
            try:
                yield session
            finally:
                active_transactions -= 1

    commerce.db.transaction = tracked_transaction

    def lookup(identifier):
        # SAVEPOINT fixture has an outer transaction; service SQL transactions stay short.
        assert active_transactions == 0
        calls.append(identifier)
        return original_lookup(identifier)

    provider.payment = lookup
    assert refresh(billing, provider, limit=1) == 1
    assert refresh(billing, provider, limit=1) == 1
    assert len(set(calls)) == 2
    assert refresh(billing, provider, limit=1) == 0


def test_refresh_ignores_other_terminal_live_unknown_and_completed_settlement(pipeline):
    commerce, billing, _, _, provider, pid = confirmed(pipeline)
    with commerce.db.transaction() as session:
        payment = session.get(Payment, pid)
        payment.fee, payment.fee_currency, payment.net_usdt = Decimal(0), "USDT", Decimal(0)
    provider.payment = lambda identifier: pytest.fail("Unexpected provider lookup")
    assert refresh(billing, provider) == 0
    assert refresh(Billing(commerce, "other", "sandbox"), provider) == 0
    with pytest.raises(DomainError) as denied:
        Billing(commerce, billing.terminal, "live")
    assert denied.value.code == "production_admission_denied"


@pytest.mark.parametrize("limit,interval", [(True, 300), (0, 300), (51, 300), (1, True), (1, 59), (1, 86401)])
def test_settlement_operational_policy_bounds(pipeline, limit, interval):
    _, billing, _, _, provider = pipeline
    with pytest.raises(ValueError):
        billing.refresh_missing_settlements(provider, limit=limit, interval_seconds=interval)


def test_worker_tick_refreshes_existing_paid_settlement_using_configured_policy(pipeline, monkeypatch):
    import fakeredis
    from sunrise_worker import main as worker_main

    from tests.e2e.local_auth_server import SyntheticSettings

    commerce, _, _, order, provider, pid = confirmed(pipeline)
    provider.payments["test-" + order["id"]].update(platform_fee_usdt="0.48", amount_usdt_net="5.50")
    config = SyntheticSettings(
        _env_file=None,
        instance_config_path=None,
        database_url=commerce.db.engine.url.render_as_string(hide_password=False),
        secret_key="synthetic-worker-settlement-key-" * 2,
        local_test=True,
        payment_settlement_interval_seconds=120,
        payment_settlement_batch_size=1,
    )
    monkeypatch.setattr(worker_main, "Database", lambda url: commerce.db)
    worker = worker_main.Worker(config, redis_client=fakeredis.FakeRedis(), provider=provider)
    worker.tick()
    with commerce.db.transaction() as session:
        assert session.get(Payment, pid).fee == Decimal("0.48")


def test_expired_invoice_candidates_never_starve_fresh_worker_payment(pipeline, monkeypatch):
    import fakeredis
    from sunrise_worker import main as worker_main

    from tests.e2e.local_auth_server import SyntheticSettings

    commerce, _, _, order, provider = pipeline
    with commerce.db.transaction() as session:
        initial_quote = session.get(Quote, order["quote_id"])
        initial_quote.expires_at = now() - timedelta(seconds=1)
        original_order = session.get(Order, order["id"])
        fresh_id = None
        for index in range(10):
            quote = Quote(
                account_id=initial_quote.account_id,
                snapshot=dict(initial_quote.snapshot),
                expires_at=now() + timedelta(minutes=5) if index == 9 else initial_quote.expires_at,
            )
            session.add(quote)
            session.flush()
            candidate = Order(
                account_id=original_order.account_id,
                quote_id=quote.id,
                idempotency_key=str(uuid4()),
                amount=original_order.amount,
                currency=original_order.currency,
                environment="sandbox",
            )
            session.add(candidate)
            session.flush()
            if index == 9:
                fresh_id = candidate.id
    config = SyntheticSettings(
        _env_file=None,
        instance_config_path=None,
        database_url=commerce.db.engine.url.render_as_string(hide_password=False),
        secret_key="synthetic-worker-fresh-invoice-key-" * 2,
        local_test=True,
        dashboard_url="https://dashboard.example.test",
    )
    monkeypatch.setattr(worker_main, "Database", lambda url: commerce.db)
    worker_main.Worker(config, redis_client=fakeredis.FakeRedis(), provider=provider).tick()
    assert set(provider.payments) == {"test-" + fresh_id}


def test_settlement_settings_are_bounded_operational_policy():
    from tests.e2e.local_auth_server import SyntheticSettings

    values = {
        "_env_file": None,
        "instance_config_path": None,
        "database_url": "postgresql+psycopg://synthetic:synthetic@127.0.0.1:55439/sunrise_test_backend",
        "secret_key": "synthetic-settlement-settings-key-" * 2,
    }
    settings = SyntheticSettings(**values)
    assert settings.payment_settlement_interval_seconds == 300
    assert settings.payment_settlement_batch_size == 10
    for extra in ({"payment_settlement_interval_seconds": 59}, {"payment_settlement_batch_size": 51}):
        with pytest.raises(ValueError):
            SyntheticSettings(**(values | extra))
