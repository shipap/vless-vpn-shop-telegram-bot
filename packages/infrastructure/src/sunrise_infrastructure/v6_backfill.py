"""Явные совместимые backfill без внешних интеграций и без создания согласий/денег/прав."""

from sqlalchemy import func, select

from .db import Account, Alias, Audit, Order, Payment, Refund, RefundReview, Segment
from .refund_reviews import segment_state


def preserve_legacy_consent(session, rows):
    result = {"preserved": 0, "unchanged": 0, "unmatched": 0, "conflicts": 0}
    for row in rows:
        flags = {k: row[k] for k in ("tos", "tos_accepted") if k in row}
        if not flags:
            continue
        if any(v is not None and type(v) not in {bool, int, str} for v in flags.values()):
            raise ValueError("Неверный тип исторического tos свидетельства")
        original = int(row["user_id"])
        alias = session.get(Alias, original)
        account = session.scalar(
            select(Account).where(Account.id == (alias.canonical_id if alias else original)).with_for_update()
        )
        if not account:
            result["unmatched"] += 1
            continue
        evidence = {"source_table": "public.users", "flags": flags}
        sources = dict((account.legacy_consent or {}).get("legacy_sources", {}))
        if str(original) in sources:
            result["unchanged" if sources[str(original)] == evidence else "conflicts"] += 1
            continue
        sources[str(original)] = evidence
        account.legacy_consent = (account.legacy_consent or {}) | {"legacy_sources": sources}
        result["preserved"] += 1
    return result


def backfill_refund_reviews(db):
    if db.engine.url.host not in {"127.0.0.1", "localhost"} or db.engine.url.database not in {
        "sunrise_legacy",
        "sunrise_test",
        "sunrise_test_backend",
    }:
        raise ValueError("Backfill разрешён только явно выбранной изолированной локальной копии")
    result = {"created": 0, "existing": 0, "automatic_preserved": 0}
    with db.transaction() as s:
        s.execute(select(func.pg_advisory_xact_lock(617045505)))
        for refund in s.scalars(select(Refund).order_by(Refund.id)):
            if s.scalar(select(RefundReview.id).where(RefundReview.refund_id == refund.id)):
                result["existing"] += 1
                continue
            payment = s.scalar(select(Payment).where(Payment.id == refund.payment_id).with_for_update())
            if s.scalar(select(RefundReview.id).where(RefundReview.refund_id == refund.id)):
                result["existing"] += 1
                continue
            order = s.get(Order, payment.order_id)
            decisions = list(
                s.scalars(
                    select(Audit).where(
                        Audit.target == payment.id, Audit.action == "refund_entitlement_decision"
                    )
                )
            )
            decision = next(
                (a for a in decisions if a.details.get("operation_id") == refund.operation_id), None
            )
            if decision and decision.details.get("action") == "automatic_revocation":
                result["automatic_preserved"] += 1
                continue
            segment = s.scalar(select(Segment).where(Segment.order_id == order.id).with_for_update())
            details = decision.details if decision else {}
            s.add(
                RefundReview(
                    refund_id=refund.id,
                    payment_id=payment.id,
                    segment_id=segment.id if segment else None,
                    state="dispute" if details.get("kind") == "chargeback" else "pending",
                    evidence={
                        "source": "historical_review_backfill",
                        "reason": decision.reason if decision else "historical_usage_unverified",
                        "kind": details.get("kind", "refund"),
                        "operation_id": refund.operation_id,
                        "usage": details.get("usage_snapshot"),
                        "segment": segment_state(segment),
                        "original_audit_id": decision.id if decision else None,
                    },
                )
            )
            result["created"] += 1
        if result["created"]:
            s.add(
                Audit(
                    action="refund_review_backfill",
                    target="v6_refund_reviews",
                    reason="Явный перенос исторических возвратов без изменения денег и прав",
                    details=result.copy(),
                )
            )
    return result
