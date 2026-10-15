import importlib
from datetime import timedelta

import pytest
from sunrise_infrastructure.db import now


def policy():
    try:
        module = importlib.import_module("sunrise_core.refunds")
    except ModuleNotFoundError:
        module = None
    assert module is not None, "Missing safe full-refund entitlement decision policy"
    return module


@pytest.mark.parametrize(
    "hours,traffic,expected",
    [
        (24, 100_000_000, "automatic_revocation"),
        (24.0001, 1, "manual_review"),
        (1, 100_000_001, "manual_review"),
        (1, 1, "automatic_revocation"),
    ],
)
def test_full_refund_requires_both_usage_and_tenure_thresholds(hours, traffic, expected):
    module = policy()
    instant = now()
    evidence = module.VerifiedEntitlementUsage(
        order_id="synthetic_order",
        segment_id="synthetic_segment",
        started_at=instant - timedelta(hours=hours),
        observed_at=instant,
        traffic_bytes=traffic,
        source="verified_panel_attestation",
    )
    decision = module.decide_refund(
        full=True,
        disputed=False,
        evidence=evidence,
        instant=instant,
        order_id="synthetic_order",
        segment_id="synthetic_segment",
    )
    assert decision["action"] == expected


@pytest.mark.parametrize("case", ["missing", "stale", "partial", "wrong_entitlement", "untrusted", "dispute"])
def test_uncertain_partial_or_disputed_refunds_never_auto_revoke(case):
    module = policy()
    instant = now()
    evidence = module.VerifiedEntitlementUsage(
        order_id="other" if case == "wrong_entitlement" else "synthetic_order",
        segment_id="synthetic_segment",
        started_at=instant - timedelta(hours=1),
        observed_at=instant - timedelta(seconds=301) if case == "stale" else instant,
        traffic_bytes=0,
        source="unverified" if case == "untrusted" else "verified_panel_attestation",
    )
    decision = module.decide_refund(
        full=case != "partial",
        disputed=case == "dispute",
        evidence=None if case == "missing" else evidence,
        instant=instant,
        order_id="synthetic_order",
        segment_id="synthetic_segment",
    )
    assert decision["action"] == ("dispute_freeze" if case == "dispute" else "manual_review")
