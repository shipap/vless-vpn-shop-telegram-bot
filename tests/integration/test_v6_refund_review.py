from uuid import uuid4

import fakeredis
import pytest
from sqlalchemy import select
from sunrise_core.errors import DomainError
from sunrise_infrastructure.admin import Admin
from sunrise_infrastructure.billing import Billing
from sunrise_infrastructure.db import Audit, Payment, RefundReview, Segment, now
from sunrise_infrastructure.security import RateGuard
from test_v6_legal import legal_context as legal_context


def test_manual_refund_resolution_is_scoped_idempotent_and_audited(legal_context):
    c, aid, documents = legal_context
    c.accept_legal(aid, documents, "website", "checkout")
    q = c.quote(aid, "standard", 30, "purchase")
    order = c.create_order(aid, q["id"], str(uuid4()))
    c.test_pay(aid, order["id"])
    with c.db.transaction() as s:
        p = s.scalar(select(Payment).where(Payment.order_id == order["id"]))
        segment = s.scalar(select(Segment).where(Segment.order_id == order["id"]))
        original_end = segment.ends_at
    Billing(c, p.terminal, p.environment).refund(p.provider_id, str(uuid4()), p.amount, now())
    admin = Admin(c, RateGuard(fakeredis.FakeRedis()))
    review = next(r for r in admin.refund_reviews()["items"] if r["payment_id"] == p.id)
    with pytest.raises(DomainError) as stale:
        admin.resolve_refund(
            aid, review["id"], "revoke", "Проверка решения", expected_subscription_version=9999
        )
    assert stale.value.code == "refund_rights_stale"
    result = admin.resolve_refund(aid, review["id"], "keep", "Оставить оплаченное право")
    assert result["state"] == "resolved"
    assert admin.resolve_refund(aid, review["id"], "keep", "Повтор подтверждения")["state"] == "resolved"
    with c.db.transaction() as s:
        assert s.get(Segment, segment.id).ends_at == original_end
        assert s.get(RefundReview, review["id"]).resolution["action"] == "keep"
        assert (
            len(
                list(
                    s.scalars(
                        select(Audit).where(
                            Audit.action == "refund_review_resolve", Audit.target == review["id"]
                        )
                    )
                )
            )
            == 1
        )


@pytest.mark.parametrize("action", ["revoke", "shorten", "recalculate"])
def test_refund_mutation_keeps_unrelated_paid_segment(legal_context, action, monkeypatch):
    from datetime import datetime, timedelta

    from sunrise_infrastructure.db import Job, Subscription

    c, aid, documents = legal_context
    c.accept_legal(aid, documents, "website", "checkout")
    q = c.quote(aid, "standard", 30, "purchase")
    order = c.create_order(aid, q["id"], str(uuid4()))
    c.test_pay(aid, order["id"])
    with c.db.transaction() as s:
        p = s.scalar(select(Payment).where(Payment.order_id == order["id"]))
        segment = s.scalar(select(Segment).where(Segment.order_id == order["id"]))
        sid = segment.subscription_id
    future_quote = c.quote(aid, "standard", 30, "renew", sid)
    future_order = c.create_order(aid, future_quote["id"], str(uuid4()))
    c.test_pay(aid, future_order["id"])
    with c.db.transaction() as s:
        future = s.scalar(select(Segment).where(Segment.order_id == future_order["id"]))
        future_id, future_end = future.id, future.ends_at
        sub = s.get(Subscription, segment.subscription_id)
        sub.expires_at = future_end
        version = sub.version
    Billing(c, p.terminal, p.environment).refund(p.provider_id, str(uuid4()), p.amount, now())
    admin = Admin(c, RateGuard(fakeredis.FakeRedis()))
    review = next(r for r in admin.refund_reviews(page_size=200)["items"] if r["payment_id"] == p.id)
    end = (now() + timedelta(days=10)).isoformat() if action != "revoke" else None
    result = admin.resolve_refund(
        aid, review["id"], action, "Проверено точное соответствие периода", end, version
    )
    assert result["state"] == "resolved"
    with c.db.transaction() as s:
        assert s.get(Segment, future_id).ends_at == future_end
        assert s.get(Subscription, segment.subscription_id).expires_at == future_end
        assert s.get(Subscription, segment.subscription_id).version == version + 1
        job = s.scalar(select(Job).where(Job.key == "refund-review:" + review["id"]))
        if action == "revoke":
            assert job.payload["panel_status"] == "DISABLED"
        else:
            assert datetime.fromisoformat(job.payload["expires_at"]) == datetime.fromisoformat(end)
            assert job.payload["active_segment_id"] == segment.id
        future_start = s.get(Segment, future_id).starts_at
    gap = future_start - timedelta(days=1)
    with c.db.transaction() as s:
        payload = c._provision_payload(s, s.get(Subscription, segment.subscription_id), moment=gap)
        assert payload["panel_status"] == "DISABLED"
    monkeypatch.setattr("sunrise_infrastructure.commerce.now", lambda: future_start + timedelta(seconds=1))
    c.apply_segment_boundaries()
    with c.db.transaction() as s:
        sub = s.get(Subscription, segment.subscription_id)
        resumed_version = sub.version
        job = s.scalar(select(Job).where(Job.key == "segment:" + future_id + ":" + str(sub.version)))
        assert job.payload["active_segment_id"] == future_id
        assert job.payload["panel_status"] == "ACTIVE"
        assert datetime.fromisoformat(job.payload["expires_at"]) == future_end
    c.apply_segment_boundaries()
    with c.db.transaction() as s:
        assert s.get(Subscription, segment.subscription_id).version == resumed_version


def test_refund_threshold_cannot_relax_owner_caps(legal_context):
    c, aid, _ = legal_context
    admin = Admin(c, RateGuard(fakeredis.FakeRedis()))
    with pytest.raises(DomainError):
        admin.update_refund_policy(aid, 25, 100000000, "Проверка верхнего предела")
    with pytest.raises(DomainError):
        admin.update_refund_policy(aid, 24, 100000001, "Проверка верхнего предела")
