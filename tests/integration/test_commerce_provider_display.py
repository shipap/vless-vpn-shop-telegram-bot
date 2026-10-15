"""Read-side invoice, readiness and settlement facts use durable synthetic records."""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sunrise_infrastructure.db import Account, Job, Payment, ProviderOperation, Quote, Subscription, now

from tests.integration.test_payment_pipeline import enqueue
from tests.integration.test_payment_pipeline import pipeline as pipeline


def test_expired_quote_hides_existing_invoice_without_changing_paid_facts(pipeline):
    commerce, billing, aid, order, provider = pipeline
    billing.prepare_payment(order["id"], provider)
    assert commerce.order(aid, order["id"])["checkout_url"] is not None
    with commerce.db.transaction() as tx:
        tx.get(Quote, order["quote_id"]).expires_at = now() - timedelta(seconds=1)
    assert commerce.order(aid, order["id"])["checkout_url"] is None
    enqueue(commerce, order)
    billing.reconcile_inbox(provider)
    assert commerce.order(aid, order["id"])["status"] == "reconciliation_required"
    with commerce.db.transaction() as tx:
        assert tx.scalar(select(Payment).where(Payment.order_id == order["id"])).status == "paid"
        assert (
            tx.scalar(select(ProviderOperation).where(ProviderOperation.order_id == order["id"])).state
            == "reconciled"
        )


def test_profile_payment_fee_currency_comes_from_settlement(pipeline):
    commerce, billing, aid, order, provider = pipeline
    billing.prepare_payment(order["id"], provider)
    enqueue(commerce, order)
    billing.reconcile_inbox(provider)
    with commerce.db.transaction() as tx:
        payment = tx.scalar(select(Payment).where(Payment.order_id == order["id"]))
        payment.fee, payment.fee_currency = Decimal("0.25"), "USDT"
    payment = commerce.dashboard(aid)["payments"][0]
    assert payment["currency"] == "RUB"
    assert (Decimal(payment["fee"]), payment["fee_currency"]) == (Decimal("0.25"), "USDT")


@pytest.mark.parametrize(
    "state,error,owner_delta,expected",
    [
        ("pending", None, 0, "pending"),
        ("done", None, 0, "ready"),
        ("done", "superseded", 0, "review"),
        ("review", "panel_review_required", 0, "review"),
        ("done", None, 1, "review"),
    ],
)
def test_order_readiness_uses_exact_owned_provisioning_job(pipeline, state, error, owner_delta, expected):
    commerce, billing, aid, order, provider = pipeline
    billing.prepare_payment(order["id"], provider)
    enqueue(commerce, order)
    billing.reconcile_inbox(provider)
    with commerce.db.transaction() as tx:
        job = tx.scalar(select(Job).where(Job.key == "provision:" + order["id"]))
        job.state, job.last_error = state, error
        if state == "done":
            tx.get(Subscription, job.payload["subscription_id"]).status = "active"
        if owner_delta:
            account = Account(email="foreign-readiness@example.test")
            tx.add(account)
            tx.flush()
            job.account_id = account.id
    result = commerce.order(aid, order["id"])
    assert result["status"] == "paid"
    assert result["provisioning_status"] == expected
