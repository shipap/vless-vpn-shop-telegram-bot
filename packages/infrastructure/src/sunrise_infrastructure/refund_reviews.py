"""Ручное решение меняет только доказанно связанный сегмент, никогда денежный остаток."""

from datetime import datetime

from sqlalchemy import func, select
from sunrise_core.errors import DomainError, missing
from sunrise_core.refunds import paid_entitlement_matches

from . import panel_entitlements
from .db import Audit, Job, Order, Payment, Quote, Refund, RefundReview, Segment, Setting, Subscription, now


def segment_state(segment):
    if not segment:
        return None
    return {
        "id": segment.id,
        "starts_at": segment.starts_at.isoformat(),
        "ends_at": segment.ends_at.isoformat(),
        "product_id": segment.product_id,
        "devices": segment.devices,
        "paid_value": str(segment.paid_value),
        "kind": segment.kind,
    }


class RefundReviews:
    def __init__(self, commerce):
        self.commerce, self.db = commerce, commerce.db

    def dto(self, s, row):
        payment = s.get(Payment, row.payment_id)
        order = s.get(Order, payment.order_id)
        refund = s.get(Refund, row.refund_id)
        sub = s.get(Subscription, order.subscription_id) if order.subscription_id else None
        return {
            "id": row.id,
            "payment_id": payment.id,
            "order_id": order.id,
            "account_id": str(payment.account_id),
            "refund_id": refund.id,
            "amount": str(refund.amount),
            "currency": payment.currency,
            "subscription_id": order.subscription_id,
            "subscription_version": sub.version if sub else None,
            "segment_id": row.segment_id,
            "state": row.state,
            "evidence": row.evidence,
            "resolution": row.resolution,
            "created_at": row.created_at.isoformat(),
            "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
        }

    def list(self, page=1, page_size=25):
        if page < 1 or not 1 <= page_size <= 200:
            raise DomainError("page_invalid", "Проверьте страницу.", 400)
        with self.db.transaction() as s:
            query = select(RefundReview)
            total = s.scalar(select(func.count()).select_from(query.subquery()))
            rows = s.scalars(
                query.order_by(RefundReview.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
            )
            return {
                "items": [self.dto(s, row) for row in rows],
                "total": total,
                "page": page,
                "page_size": page_size,
            }

    def resolve(self, actor, review_id, action, reason, ends_at=None, expected_subscription_version=None):
        if (
            action not in {"keep", "revoke", "shorten", "recalculate", "manual"}
            or not isinstance(reason, str)
            or not 5 <= len(reason.strip()) <= 500
        ):
            raise DomainError("refund_resolution_invalid", "Укажите решение и причину.", 400)
        with self.db.transaction() as s:
            # Аккаунты в порядке ID, затем платёж -> сегмент -> подписка.
            hint = s.get(RefundReview, review_id)
            if not hint:
                raise missing()
            payment_hint = s.get(Payment, hint.payment_id)
            if not payment_hint:
                raise missing()
            owner = payment_hint.account_id
            locked = self.commerce._lock_accounts_in(s, [owner, getattr(actor, "id", actor)])
            payment = s.scalar(
                select(Payment)
                .where(Payment.id == hint.payment_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if not payment or payment.account_id != locked[owner]:
                raise missing()
            row = s.scalar(
                select(RefundReview)
                .where(RefundReview.id == review_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            request = {"action": action, "ends_at": ends_at}
            if row.state == "resolved":
                if any(row.resolution.get(k) != v for k, v in request.items()):
                    raise DomainError(
                        "refund_already_resolved", "По возврату уже принято другое решение.", 409
                    )
                return self.dto(s, row)
            order = s.get(Order, payment.order_id)
            segment = (
                s.scalar(select(Segment).where(Segment.id == row.segment_id).with_for_update())
                if row.segment_id
                else None
            )
            sub = (
                s.scalar(
                    select(Subscription).where(Subscription.id == order.subscription_id).with_for_update()
                )
                if order.subscription_id
                else None
            )
            if self.commerce.accounts is not None:
                from .admin_operations import AdminOperations

                AdminOperations(self.commerce, self.commerce.accounts.settings)._guard(
                    s, actor, "refunds_manage", reason, "refund-review:" + review_id, True
                )
            before = {
                "segment": segment_state(segment),
                "subscription_version": sub.version if sub else None,
                "expires_at": sub.expires_at.isoformat() if sub and sub.expires_at else None,
                "state": row.state,
            }
            if action in {"revoke", "shorten", "recalculate"}:
                if sub:
                    from .retention import assert_mutation_allowed_in

                    assert_mutation_allowed_in(s, sub)
                if (
                    not sub
                    or not segment
                    or expected_subscription_version != sub.version
                    or segment.subscription_id != sub.id
                    or segment.order_id != order.id
                    or segment_state(segment) != row.evidence.get("segment")
                ):
                    raise DomainError(
                        "refund_rights_stale",
                        "Права изменились. Обновите дело и проверьте связанные периоды.",
                        409,
                    )
                if not paid_entitlement_matches(segment, s.get(Quote, order.quote_id).snapshot):
                    raise DomainError(
                        "refund_mapping_review",
                        "Сегмент содержит другие оплаченные изменения. Требуется отдельное ручное решение.",
                        409,
                    )
                instant = now()
                prior_status = panel_entitlements.local_status(s, sub)
                current_override = panel_entitlements.override(s, sub, instant)
                if action == "revoke":
                    segment.ends_at = max(segment.starts_at, min(segment.ends_at, instant))
                    segment.kind = "refunded"
                else:
                    try:
                        target = datetime.fromisoformat(ends_at)
                        if (
                            target.tzinfo is None
                            or not max(instant, segment.starts_at) < target < segment.ends_at
                        ):
                            raise ValueError()
                    except (TypeError, ValueError):
                        raise DomainError(
                            "refund_end_invalid",
                            "Укажите новый срок внутри оставшегося оплаченного периода.",
                            400,
                        ) from None
                    segment.ends_at = target
                if current_override and current_override.get("segment_id") == segment.id:
                    marker_row = s.get(Setting, panel_entitlements.override_key(sub.id))
                    if action == "revoke":
                        s.delete(marker_row)
                    else:
                        marker_row.value = current_override | {
                            "expires_at": min(
                                datetime.fromisoformat(current_override["expires_at"]), target
                            ).isoformat(),
                            "expiry_source": "owner_refund_review",
                            "expiry_review_id": row.id,
                            "pending_target_version": sub.version + 1,
                        }
                s.flush()
                remaining = panel_entitlements.remaining_segments(s, sub, instant)
                sub.expires_at = max(
                    (panel_entitlements.effective_end(s, sub, r) for r in remaining), default=instant
                )
                active = panel_entitlements.current_segment(s, sub, instant)
                current_override = panel_entitlements.override(s, sub, instant)
                if active and not current_override:
                    sub.product_id, sub.devices_limit = active.product_id, active.devices
                sub.version += 1
                sub.status = "provisioning_pending" if active else "revocation_pending"
                payload = self.commerce._provision_payload(
                    s,
                    sub,
                    active.price_basis.get("panel_mappings", {})
                    if active
                    else segment.price_basis.get("panel_mappings", {})
                    if sub.environment == "local_test"
                    and self.commerce.panel_provider is None
                    and not panel_entitlements.bound(sub)
                    else None,
                    prior_status=prior_status,
                    origin="refund_review",
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
                        key="refund-review:" + row.id,
                        kind="provision",
                        account_id=sub.account_id,
                        payload=payload,
                    )
                )
            row.state = "manual_pending" if action == "manual" else "resolved"
            row.resolved_at = None if action == "manual" else now()
            row.resolution = request | {
                "actor_id": str(actor) if actor is not None else None,
                "reason": reason,
                "recorded_at": now().isoformat(),
            }
            after = {
                "segment": segment_state(segment),
                "subscription_version": sub.version if sub else None,
                "expires_at": sub.expires_at.isoformat() if sub and sub.expires_at else None,
                "state": row.state,
                "resolution": row.resolution,
            }
            s.add(
                Audit(
                    actor_id=actor,
                    action="refund_review_resolve",
                    target=row.id,
                    reason=reason,
                    details={"before": before, "after": after},
                )
            )
            s.flush()
            return self.dto(s, row)
