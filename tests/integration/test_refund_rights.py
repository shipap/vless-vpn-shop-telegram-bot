from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select
from sunrise_core.errors import DomainError
from sunrise_core.refunds import VerifiedEntitlementUsage
from sunrise_infrastructure.billing import Billing
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import Account, Agreement, Job, Payment, Segment, Subscription, now

from tests.isolated_database import isolated_database
from tests.legal_fixture import accept_test_legal


@pytest.fixture
def refund_db():
    with isolated_database("refund_rights_test") as db:
        yield db


def purchased(db):
    commerce = Commerce(db, local_test=True)
    with commerce.db.transaction() as s:
        account = Account(email=str(uuid4()) + "@example.test")
        s.add(account)
        s.flush()
        aid = account.id
    accept_test_legal(Commerce(commerce.db), account.id)
    quote = commerce.quote(aid, "standard", 30, "purchase")
    order = commerce.create_order(aid, quote["id"], str(uuid4()))
    commerce.test_pay(aid, order["id"])
    with commerce.db.transaction() as s:
        payment = s.scalar(select(Payment).where(Payment.order_id == order["id"]))
        segment = s.scalar(select(Segment).where(Segment.order_id == order["id"]))
    billing = Billing(commerce, payment.terminal, "local_test")
    return commerce, billing, aid, order, payment, segment


def usage(order, segment):
    return VerifiedEntitlementUsage(
        order_id=order["id"],
        segment_id=segment.id,
        started_at=segment.starts_at,
        observed_at=now(),
        traffic_bytes=100_000_000,
        source="verified_panel_attestation",
    )


def test_confirmed_unused_full_refund_revokes_only_target_and_preserves_future_rights(refund_db, monkeypatch):
    c, billing, aid, order, payment, segment = purchased(refund_db)
    sid = order["subscription_id"] or c.order(aid, order["id"])["subscription_id"]
    future_quote = c.quote(aid, "standard", 30, "renew", sid)
    future_order = c.create_order(aid, future_quote["id"], str(uuid4()))
    c.test_pay(aid, future_order["id"])
    with c.db.transaction() as s:
        future = s.scalar(select(Segment).where(Segment.order_id == future_order["id"]))
        fid, future_start, future_end = future.id, future.starts_at, future.ends_at
        future_history = (
            future.starts_at,
            future.ends_at,
            future.paid_value,
            dict(future.price_basis),
            future.order_id,
        )
    operation = str(uuid4())
    result = billing.refund(
        payment.provider_id, operation, payment.amount, now(), usage=usage(order, segment)
    )
    assert result["entitlement_action"] == "automatic_revocation"
    billing.refund(payment.provider_id, operation, payment.amount, now(), usage=usage(order, segment))
    with c.db.transaction() as s:
        assert s.get(Segment, segment.id).kind == "refunded"
        assert s.get(Segment, fid).ends_at == future_end
        assert s.get(Subscription, sid).expires_at == future_end
        jobs = list(s.scalars(select(Job).where(Job.key.like("refund-rights:refund:" + payment.id + ":%"))))
        assert len(jobs) == 1 and jobs[0].payload["panel_status"] == "DISABLED"
    c.run_jobs(limit=500)
    with c.db.transaction() as s:
        assert s.get(Subscription, sid).status == "revoked"
    moment = future_start + timedelta(seconds=1)
    monkeypatch.setattr("sunrise_infrastructure.commerce.now", lambda: moment)
    monkeypatch.setattr("sunrise_infrastructure.panel_entitlements.now", lambda: moment)
    c.run_jobs(limit=500)
    with c.db.transaction() as s:
        assert s.get(Subscription, sid).status == "active"
        future = s.get(Segment, fid)
        assert (
            future.starts_at,
            future.ends_at,
            future.paid_value,
            future.price_basis,
            future.order_id,
        ) == future_history


def test_partial_installments_and_missing_attribution_remain_manual(refund_db):
    c, billing, aid, order, payment, segment = purchased(refund_db)
    sid = c.order(aid, order["id"])["subscription_id"]
    with c.db.transaction() as s:
        expiry = s.get(Subscription, sid).expires_at
    first = billing.refund(payment.provider_id, str(uuid4()), Decimal(50), now(), usage=usage(order, segment))
    last = billing.refund(
        payment.provider_id, str(uuid4()), payment.amount - Decimal(50), now(), usage=usage(order, segment)
    )
    assert first["entitlement_action"] == last["entitlement_action"] == "manual_review"
    with c.db.transaction() as s:
        assert s.get(Subscription, sid).expires_at == expiry
    c, billing, aid, order, payment, segment = purchased(refund_db)
    assert (
        billing.refund(payment.provider_id, str(uuid4()), payment.amount, now(), usage={"traffic_bytes": 0})[
            "entitlement_action"
        ]
        == "manual_review"
    )


def test_chargeback_freezes_only_disputed_subscription_and_preserves_cancellation_and_cash_facts(refund_db):
    c, billing, aid, order, payment, segment = purchased(refund_db)
    sid = c.order(aid, order["id"])["subscription_id"]
    result = billing.refund(payment.provider_id, str(uuid4()), payment.amount, now(), kind="chargeback")
    assert result["entitlement_action"] == "dispute_freeze"
    with pytest.raises(DomainError) as exc:
        c.quote(aid, "standard", 30, "renew", sid)
    assert exc.value.code == "subscription_disputed"
    assert c.quote(aid, "standard", 30, "purchase")["total"] == "99.00"
    assert c.dashboard(aid)["payments"]
    with c.db.transaction() as s:
        s.add(
            Agreement(
                account_id=aid,
                subscription_id=sid,
                state="enabled",
                provider_id="synthetic-" + sid,
                consent_amount=Decimal(99),
                currency="RUB",
            )
        )
    assert c.cancel_agreement(aid, sid)["state"] == "stop_pending"
    c.run_jobs()
    with c.db.transaction() as s:
        assert s.scalar(select(Agreement).where(Agreement.subscription_id == sid)).state == "stopped"
    import fakeredis
    from sunrise_infrastructure.admin import Admin
    from sunrise_infrastructure.security import RateGuard

    admin = Admin(c, RateGuard(fakeredis.FakeRedis()))
    detail = admin.payment_detail(payment.id)
    assert detail["dispute"]["enabled"] is True
    assert detail["refund_review_context"]["external_refundable_amount"] == "0.00"
    remaining = detail["refund_review_context"]["segments"][0]
    assert remaining["remaining_seconds"] > 0 and Decimal(remaining["remaining_service_value"]) > 0
    assert remaining["currency"] == "RUB" and remaining["valuation_source"] == "immutable_paid_quote"
    admin.subscription_operation(aid, sid, "resume", "Возобновление не снимает спор")
    with pytest.raises(DomainError):
        c.quote(aid, "standard", 30, "renew", sid)
    assert admin.payment_detail(payment.id)["entitlement_decisions"][0]["action"] == "dispute_freeze"
    assert admin.resolve_dispute(aid, payment.id, "Отдельное решение владельца")["rights_changed"] is False
    assert c.quote(aid, "standard", 30, "renew", sid)["total"] == "99.00"


def test_full_original_refund_does_not_revoke_separately_paid_upgrade_rights(refund_db):
    c, billing, aid, order, payment, segment = purchased(refund_db)
    sid = c.order(aid, order["id"])["subscription_id"]
    upgrade = c.quote(aid, "universal", 30, "upgrade", sid)
    upgrade_order = c.create_order(aid, upgrade["id"], str(uuid4()))
    c.test_pay(aid, upgrade_order["id"])
    with c.db.transaction() as s:
        expiry = s.get(Subscription, sid).expires_at
    result = billing.refund(
        payment.provider_id, str(uuid4()), payment.amount, now(), usage=usage(order, segment)
    )
    assert result["entitlement_action"] == "manual_review"
    import fakeredis
    from sunrise_infrastructure.admin import Admin
    from sunrise_infrastructure.security import RateGuard

    context = Admin(c, RateGuard(fakeredis.FakeRedis())).payment_detail(payment.id)["refund_review_context"]
    assert context["segments"][0]["remaining_service_value"] is None
    assert context["segments"][0]["unavailable_reason"] == "paid_basis_missing_or_changed"
    with c.db.transaction() as s:
        assert s.get(Subscription, sid).expires_at == expiry
        assert s.get(Segment, segment.id).kind == "paid"
