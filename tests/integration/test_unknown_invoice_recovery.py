"""Неизвестный исход счёта сверяется без повторного создания и без выдачи прав из списка."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sunrise_core.errors import DomainError
from sunrise_infrastructure.billing import Billing
from sunrise_infrastructure.db import Inbox, Order, Payment, ProviderOperation, Quote, Setting, now

from tests.integration.test_production_business_races import live_business as live_business


@pytest.fixture
def recovery(live_business):
    db, commerce, aid, sid, order_id, iid, fact = live_business
    with db.transaction() as session:
        operation = session.scalar(select(ProviderOperation).where(ProviderOperation.order_id == order_id))
        operation.provider_id, operation.state = None, "outcome_unknown"
        session.delete(session.get(Inbox, iid))
        operation_id = operation.id
    calls = []

    def listing(identifier):
        assert identifier == order_id
        calls.append("list")
        return {"items": [{"payment_id": "synthetic-payment", "order_id": order_id}], "total": 1}

    def lookup(identifier):
        calls.append(identifier)
        return dict(fact)

    provider = SimpleNamespace(
        terminal="synthetic-terminal",
        environment="live",
        list_payments=listing,
        payment=lookup,
    )
    billing = Billing(commerce, provider.terminal, "live")
    return db, billing, provider, order_id, operation_id, fact, calls


def test_unique_unknown_invoice_is_verified_then_enters_existing_inbox_once(recovery):
    db, billing, provider, order_id, operation_id, fact, calls = recovery
    assert billing.recover_unknown_invoices(provider) == 1
    assert calls == ["list", "synthetic-payment"]
    with db.transaction() as session:
        assert session.get(Order, order_id).status == "awaiting_payment"
        assert (
            session.scalar(select(func.count()).select_from(Payment).where(Payment.order_id == order_id)) == 0
        )
        assert session.scalar(select(Inbox).where(Inbox.state == "pending")).payload == {
            "payment_id": "synthetic-payment",
            "order_id": order_id,
            "source": "unknown_invoice_recovery",
        }
    assert billing.recover_unknown_invoices(provider) == 0
    assert billing.reconcile_inbox(provider) == 1
    assert billing.reconcile_inbox(provider) == 0
    with db.transaction() as session:
        assert session.get(Order, order_id).status == "paid"
        assert session.get(ProviderOperation, operation_id).provider_id == fact["payment_id"]
        assert (
            session.scalar(select(func.count()).select_from(Payment).where(Payment.order_id == order_id)) == 1
        )


@pytest.mark.parametrize(
    "items",
    [
        [],
        [{"payment_id": "foreign", "order_id": "other-order"}],
        [
            {"payment_id": "synthetic-payment", "order_id": "own"},
            {"payment_id": "second-payment", "order_id": "own"},
        ],
    ],
)
def test_absent_foreign_or_ambiguous_candidates_leave_unknown_and_never_cancel(recovery, items):
    db, billing, provider, order_id, operation_id, _, calls = recovery
    candidates = [
        item | {"order_id": order_id if item["order_id"] == "own" else item["order_id"]} for item in items
    ]
    provider.list_payments = lambda _: {"items": candidates, "total": len(candidates)}
    assert billing.recover_unknown_invoices(provider) == 0
    assert calls == []
    with db.transaction() as session:
        assert session.get(ProviderOperation, operation_id).state == "outcome_unknown"
        assert session.get(ProviderOperation, operation_id).provider_id is None
        assert session.get(Order, order_id).status == "awaiting_payment"
        assert session.scalar(select(func.count()).select_from(Inbox)) == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("amount", "999.00"),
        ("environment", "sandbox"),
        ("terminal_id", "foreign"),
        ("customer_id", "999999"),
        ("order_id", "foreign"),
        ("payment_id", "different"),
        ("payment_method", "crypto"),
        ("test", True),
        ("environment", None),
    ],
)
def test_candidate_details_must_pass_strict_existing_payment_verification(recovery, field, value):
    db, billing, provider, order_id, operation_id, fact, _ = recovery
    provider.payment = lambda _: dict(fact) | {field: value}
    assert billing.recover_unknown_invoices(provider) == 0
    with db.transaction() as session:
        assert session.get(ProviderOperation, operation_id).provider_id is None
        assert session.get(Order, order_id).status == "awaiting_payment"
        assert session.scalar(select(func.count()).select_from(Inbox)) == 0


def test_binding_or_admission_changed_during_get_cannot_queue_discovery(recovery):
    db, billing, provider, _, operation_id, fact, _ = recovery

    def lookup(_):
        with db.transaction() as session:
            session.get(ProviderOperation, operation_id).provider_id = "callback-payment"
            session.get(ProviderOperation, operation_id).state = "reconciled"
        return dict(fact)

    provider.payment = lookup
    assert billing.recover_unknown_invoices(provider) == 0
    with db.transaction() as session:
        assert session.get(ProviderOperation, operation_id).provider_id == "callback-payment"
        assert session.scalar(select(func.count()).select_from(Inbox)) == 0


def test_revoked_gate_during_collection_get_does_not_assign_or_queue(recovery):
    db, billing, provider, _, operation_id, _, _ = recovery
    original = provider.list_payments

    def listing(identifier):
        with db.transaction() as session:
            session.get(Setting, "production_runtime_gates").value = {"payment_reconcile": False}
        return original(identifier)

    provider.list_payments = listing
    with pytest.raises(DomainError):
        billing.recover_unknown_invoices(provider)
    with db.transaction() as session:
        assert session.get(ProviderOperation, operation_id).provider_id is None
        assert session.scalar(select(func.count()).select_from(Inbox)) == 0


def test_durable_terminal_lease_prevents_concurrent_collection_reads_and_allows_later_retry(recovery):
    db, billing, provider, _, _, _, calls = recovery
    entered, release = Event(), Event()

    def listing(_):
        calls.append("list")
        entered.set()
        assert release.wait(10)
        return {"items": [], "total": 0}

    provider.list_payments = listing
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(billing.recover_unknown_invoices, provider)
        try:
            assert entered.wait(10)
            assert executor.submit(billing.recover_unknown_invoices, provider).result(10) == 0
            assert calls == ["list"]
        finally:
            release.set()
        assert first.result(10) == 0
    assert Billing(billing.commerce, provider.terminal, "live").recover_unknown_invoices(provider) == 0
    with db.transaction() as session:
        marker = session.get(Setting, billing._unknown_invoice_recovery_key())
        marker.value = marker.value | {"next_check_at": (now() - timedelta(seconds=1)).isoformat()}
    assert billing.recover_unknown_invoices(provider) == 0
    assert calls == ["list", "list"]


def test_callback_during_discovery_remains_authoritative_and_fulfills_only_once(recovery):
    db, billing, provider, order_id, _, fact, _ = recovery

    def listing(identifier):
        with db.transaction() as session:
            session.add(
                Inbox(
                    event_key="synthetic-late-callback",
                    payload={
                        "payment_id": fact["payment_id"],
                        "order_id": order_id,
                    },
                )
            )
        assert billing.reconcile_inbox(provider) == 1
        return {"items": [{"payment_id": fact["payment_id"], "order_id": identifier}], "total": 1}

    provider.list_payments = listing
    assert billing.recover_unknown_invoices(provider) == 0
    assert billing.reconcile_inbox(provider) == 0
    with db.transaction() as session:
        assert session.get(Order, order_id).status == "paid"
        assert (
            session.scalar(select(func.count()).select_from(Payment).where(Payment.order_id == order_id)) == 1
        )


@pytest.mark.parametrize("change", ["request", "amount", "status", "operation_order"])
def test_discovery_rechecks_full_binding_after_network(recovery, change):
    db, billing, provider, order_id, operation_id, fact, _ = recovery

    def lookup(_):
        with db.transaction() as session:
            operation = session.get(ProviderOperation, operation_id)
            order = session.get(Order, order_id)
            if change == "request":
                operation.request = operation.request | {"scope_id": "foreign"}
            elif change == "amount":
                order.amount += 1
            elif change == "status":
                order.status = "canceled"
            else:
                other = session.scalar(select(Order).where(Order.id != order_id))
                operation.order_id = other.id
        return dict(fact)

    provider.payment = lookup
    assert billing.recover_unknown_invoices(provider) == 0
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Inbox)) == 0


def test_expired_or_replaced_claim_cannot_enqueue_late_get_result(recovery):
    db, billing, provider, _, _, fact, _ = recovery

    def lookup(_):
        with db.transaction() as session:
            marker = session.get(Setting, billing._unknown_invoice_recovery_key())
            marker.value = marker.value | {"claim": "new-worker-claim"}
        return dict(fact)

    provider.payment = lookup
    assert billing.recover_unknown_invoices(provider) == 0
    with db.transaction() as session:
        assert (
            session.get(Setting, billing._unknown_invoice_recovery_key()).value["claim"] == "new-worker-claim"
        )
        assert session.scalar(select(func.count()).select_from(Inbox)) == 0


def test_unknown_batch_is_ten_and_rotates_after_absent_candidates(recovery):
    db, billing, provider, order_id, operation_id, _, _ = recovery
    with db.transaction() as session:
        original = session.get(Order, order_id)
        operation = session.get(ProviderOperation, operation_id)
        quote = session.get(Quote, original.quote_id)
        for index in range(11):
            added_quote = Quote(
                account_id=original.account_id, snapshot=quote.snapshot, expires_at=quote.expires_at
            )
            session.add(added_quote)
            session.flush()
            order = Order(
                account_id=original.account_id,
                quote_id=added_quote.id,
                idempotency_key=f"unknown-batch-{index}",
                amount=original.amount,
                currency=original.currency,
                environment="live",
            )
            session.add(order)
            session.flush()
            session.add(
                ProviderOperation(
                    order_id=order.id,
                    terminal=operation.terminal,
                    state="outcome_unknown",
                    request=operation.request | {"order_id": order.id},
                )
            )
    reads = []
    provider.list_payments = lambda identifier: reads.append(identifier) or {"items": [], "total": 0}
    assert billing.recover_unknown_invoices(provider) == 0
    assert len(reads) == len(set(reads)) == 10
    first_batch = set(reads)
    with db.transaction() as session:
        marker = session.get(Setting, billing._unknown_invoice_recovery_key())
        marker.value = marker.value | {"next_check_at": (now() - timedelta(seconds=1)).isoformat()}
    assert billing.recover_unknown_invoices(provider) == 0
    assert len(reads) == 20 and len(set(reads) - first_batch) == 2


def test_no_unknown_operations_means_no_provider_collection_read(recovery):
    db, billing, provider, _, operation_id, _, calls = recovery
    with db.transaction() as session:
        operation = session.get(ProviderOperation, operation_id)
        operation.provider_id, operation.state = "synthetic-known", "created"
    assert billing.recover_unknown_invoices(provider) == 0
    assert calls == []


def test_existing_payment_contradiction_requires_review_without_collection_read(recovery):
    db, billing, provider, order_id, _, _, calls = recovery
    with db.transaction() as session:
        payment = session.scalar(select(Payment))
        payment.order_id = order_id
    assert billing.recover_unknown_invoices(provider) == 0
    assert calls == []


@pytest.mark.parametrize("change", ["request", "operation_order", "terminal"])
def test_recovery_inbox_rechecks_saved_request_before_applying_second_get(recovery, change):
    db, billing, provider, order_id, operation_id, fact, _ = recovery
    assert billing.recover_unknown_invoices(provider) == 1

    def lookup(_):
        with db.transaction() as session:
            operation = session.get(ProviderOperation, operation_id)
            if change == "request":
                operation.request = operation.request | {"scope_id": "foreign"}
            elif change == "operation_order":
                operation.order_id = session.scalar(select(Order.id).where(Order.id != order_id))
            else:
                operation.terminal = "foreign"
        return dict(fact)

    provider.payment = lookup
    billing.reconcile_inbox(provider)
    with db.transaction() as session:
        assert session.get(ProviderOperation, operation_id).provider_id is None
        assert session.get(Order, order_id).status == "awaiting_payment"
        assert (
            session.scalar(select(func.count()).select_from(Payment).where(Payment.order_id == order_id)) == 0
        )
        assert session.scalar(select(Inbox)).state == ("pending" if change == "request" else "review")


def test_recovery_inbox_rechecks_gate_in_final_mutation_transaction(recovery):
    db, billing, provider, order_id, operation_id, fact, _ = recovery
    assert billing.recover_unknown_invoices(provider) == 1
    after_read = False
    original_require = billing.runtime_policy.require

    def lookup(_):
        nonlocal after_read
        after_read = True
        return dict(fact)

    def require(capability=None):
        original_require(capability)
        if after_read and capability == "payment_reconcile":
            with db.transaction() as session:
                session.get(Setting, "production_runtime_gates").value = {"payment_reconcile": False}

    provider.payment = lookup
    billing.runtime_policy.require = require
    billing.reconcile_inbox(provider)
    with db.transaction() as session:
        assert session.get(ProviderOperation, operation_id).provider_id is None
        assert session.get(Order, order_id).status == "awaiting_payment"
        assert (
            session.scalar(select(func.count()).select_from(Payment).where(Payment.order_id == order_id)) == 0
        )


def test_expired_claim_during_collection_stops_before_specific_get(recovery):
    db, billing, provider, order_id, _, fact, calls = recovery

    def listing(_):
        calls.append("list")
        with db.transaction() as session:
            marker = session.get(Setting, billing._unknown_invoice_recovery_key())
            marker.value = marker.value | {"lease_until": (now() - timedelta(seconds=1)).isoformat()}
        return {"items": [{"order_id": order_id, "payment_id": fact["payment_id"]}], "total": 1}

    provider.list_payments = listing
    assert billing.recover_unknown_invoices(provider) == 0
    assert calls == ["list"]
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Inbox)) == 0


@pytest.mark.parametrize("limit,interval", [(11, 300), (0, 300), (True, 300), (10, 299), (10, True)])
def test_unknown_recovery_rejects_unbounded_or_fast_polling(recovery, limit, interval):
    _, billing, provider, _, _, _, calls = recovery
    with pytest.raises(ValueError):
        billing.recover_unknown_invoices(provider, limit=limit, interval_seconds=interval)
    assert calls == []
