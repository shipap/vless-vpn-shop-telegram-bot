from uuid import uuid4

from sqlalchemy import select
from sunrise_infrastructure.billing import Billing
from sunrise_infrastructure.db import Account, LegalConsent, Payment, RefundReview, Segment, now
from test_v6_legal import legal_context as legal_context


def test_legacy_flags_backfill_preserves_raw_evidence_without_version_events(legal_context):
    from sunrise_infrastructure.v6_backfill import preserve_legacy_consent

    c, aid, _ = legal_context
    rows = [{"user_id": aid, "tos": 1, "tos_accepted": None}]
    with c.db.transaction() as s:
        assert preserve_legacy_consent(s, rows)["preserved"] == 1
    with c.db.transaction() as s:
        assert preserve_legacy_consent(s, rows)["unchanged"] == 1
        evidence = s.get(Account, aid).legacy_consent
        assert evidence["legacy_sources"][str(aid)]["flags"] == {"tos": 1, "tos_accepted": None}
        assert list(s.scalars(select(LegalConsent).where(LegalConsent.account_id == aid))) == []


def test_historical_refund_backfill_is_idempotent_and_never_changes_rights(legal_context):
    from sunrise_infrastructure.v6_backfill import backfill_refund_reviews

    c, aid, documents = legal_context
    c.accept_legal(aid, documents, "website", "checkout")
    q = c.quote(aid, "standard", 30, "purchase")
    order = c.create_order(aid, q["id"], str(uuid4()))
    c.test_pay(aid, order["id"])
    with c.db.transaction() as s:
        payment = s.scalar(select(Payment).where(Payment.order_id == order["id"]))
        segment = s.scalar(select(Segment).where(Segment.order_id == order["id"]))
        expiry = segment.ends_at
    Billing(c, payment.terminal, payment.environment).refund(
        payment.provider_id, str(uuid4()), payment.amount, now()
    )
    with c.db.transaction() as s:
        s.delete(s.scalar(select(RefundReview).where(RefundReview.payment_id == payment.id)))
    result = backfill_refund_reviews(c.db)
    assert result["created"] >= 1
    assert backfill_refund_reviews(c.db)["created"] == 0
    with c.db.transaction() as s:
        review = s.scalar(select(RefundReview).where(RefundReview.payment_id == payment.id))
        assert review.state == "pending" and review.evidence["source"] == "historical_review_backfill"
        assert s.get(Segment, segment.id).ends_at == expiry
