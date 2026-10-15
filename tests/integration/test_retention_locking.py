"""Real PostgreSQL locks keep money/owner mutations ordered before target locks."""

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import DBAPIError
from sunrise_infrastructure.admin import Admin
from sunrise_infrastructure.db import Account, now

from tests.integration.test_billing import billing as billing
from tests.integration.test_billing import fact
from tests.integration.test_panel_28_workflows import panel28_workflow as panel28_workflow
from tests.integration.test_panel_entitlement_freshness import entitlement as entitlement


def assert_accounts_locked_before(db, table, identifiers, action):
    inspected = False

    def inspect_locks(connection, cursor, statement, parameters, context, executemany):
        nonlocal inspected
        if inspected or "FOR UPDATE" not in statement or f"FROM {table}" not in statement:
            return
        inspected = True
        for identifier in identifiers:
            with db.engine.connect() as other:
                with pytest.raises(DBAPIError) as denied:
                    other.execute(
                        select(Account.id).where(Account.id == identifier).with_for_update(nowait=True)
                    )
                assert getattr(denied.value.orig, "sqlstate", None) == "55P03"

    event.listen(db.engine, "after_cursor_execute", inspect_locks)
    try:
        action()
    finally:
        event.remove(db.engine, "after_cursor_execute", inspect_locks)
    assert inspected


@pytest.mark.parametrize("operation", ["suspend", "compensate"])
def test_admin_locks_actor_and_target_before_subscription(entitlement, operation):
    from sunrise_infrastructure.db import Segment, StaffRole

    db, _, commerce, _, _, sid, *_ = entitlement
    commerce.accounts.settings.admin_email_allowlist = ["synthetic-lock-owner@example.test"]
    commerce.accounts.settings.admin_telegram_allowlist = ["70000000000000002"]
    with db.transaction() as session:
        session.add(
            Account(
                id=302,
                telegram_id=70000000000000002,
                email="synthetic-lock-owner@example.test",
                email_verified=True,
            )
        )
        session.add(StaffRole(account_id=302, role="owner", granted_by=302))
        segment = session.scalar(select(Segment).where(Segment.subscription_id == sid))
        segment.price_basis = segment.price_basis | {"panel_mappings": {}}

    admin = Admin(commerce, None)
    if operation == "compensate":

        def action():
            return admin.compensate(302, sid, 1, "Synthetic lock order validation")
    else:

        def action():
            return admin.subscription_operation(302, sid, "suspend", "Synthetic lock order validation")

    assert_accounts_locked_before(db, "v5_subscriptions", [301, 302], action)


@pytest.mark.parametrize("operation", ["accept", "refund"])
def test_billing_locks_payer_and_referrer_before_financial_target(billing, operation):
    commerce, service, payer, referrer, order = billing
    payment = fact(order)
    if operation == "refund":
        service.accept(payment)
    action = (
        (lambda: service.accept(payment))
        if operation == "accept"
        else (lambda: service.refund(payment.provider_id, "lock-proof", payment.amount, now()))
    )
    assert_accounts_locked_before(
        commerce.db, "v5_orders" if operation == "accept" else "v5_payments", [payer, referrer], action
    )


def test_refund_review_locks_actor_and_payer_before_payment(billing):
    from sunrise_infrastructure.db import Payment, RefundReview
    from sunrise_infrastructure.refund_reviews import RefundReviews

    commerce, service, payer, actor, order = billing
    payment = fact(order)
    service.accept(payment)
    service.refund(payment.provider_id, "review-lock-proof", payment.amount, now())
    with commerce.db.transaction() as session:
        recorded = session.scalar(select(Payment).where(Payment.provider_id == payment.provider_id))
        review = session.scalar(select(RefundReview).where(RefundReview.payment_id == recorded.id))
        review_id = review.id
    assert_accounts_locked_before(
        commerce.db,
        "v5_payments",
        [payer, actor],
        lambda: RefundReviews(commerce).resolve(actor, review_id, "keep", "Synthetic lock order validation"),
    )
