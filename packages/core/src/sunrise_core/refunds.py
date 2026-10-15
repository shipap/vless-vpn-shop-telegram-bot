"""Решение о правах при возврате: только проверенное использование конкретного периода."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class VerifiedEntitlementUsage:
    order_id: str
    segment_id: str
    started_at: datetime
    observed_at: datetime
    traffic_bytes: int
    source: str


def paid_entitlement_matches(segment, snapshot):
    """Последующие оплаченные изменения исключают автоматическое снятие исходного периода."""
    try:
        return (
            segment.product_id == snapshot["product_id"]
            and segment.devices == snapshot["devices_limit"]
            and segment.paid_value == Decimal(snapshot["base_amount"]) - Decimal(snapshot["discount_amount"])
            and segment.starts_at == datetime.fromisoformat(snapshot["starts_at"])
            and segment.ends_at == datetime.fromisoformat(snapshot["resulting_expires_at"])
        )
    except (KeyError, ValueError, TypeError):
        return False


def decide_refund(*, full, disputed, evidence, instant, order_id, segment_id):
    def manual(reason):
        return {"action": "manual_review", "reason": reason}

    if disputed:
        return {"action": "dispute_freeze", "reason": "provider_dispute"}
    if not full:
        return manual("partial_refund")
    if not isinstance(evidence, VerifiedEntitlementUsage):
        return manual("attributable_usage_unavailable")
    if evidence.source != "verified_panel_attestation":
        return manual("usage_source_unverified")
    if evidence.order_id != order_id or evidence.segment_id != segment_id:
        return manual("usage_entitlement_mismatch")
    if any(x.tzinfo is None for x in (instant, evidence.started_at, evidence.observed_at)):
        return manual("usage_time_unverified")
    freshness = (instant - evidence.observed_at).total_seconds()
    elapsed = (instant - evidence.started_at).total_seconds()
    if not 0 <= freshness <= 300:
        return manual("usage_stale")
    if elapsed < 0:
        return manual("entitlement_not_started")
    if type(evidence.traffic_bytes) is not int or evidence.traffic_bytes < 0:
        return manual("traffic_unverified")
    if elapsed > 24 * 3600 or evidence.traffic_bytes > 100_000_000:
        return manual("partly_consumed_entitlement")
    return {"action": "automatic_revocation", "reason": "full_refund_nearly_unused"}
