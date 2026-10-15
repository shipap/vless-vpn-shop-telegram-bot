"""Root-owned import authority survives only proven canonical aliases."""

from types import SimpleNamespace

import pytest
from sunrise_core.errors import DomainError
from sunrise_infrastructure.db import Account, Alias, Setting
from sunrise_infrastructure.promos import Promos
from sunrise_infrastructure.retention import legacy_operation_in, legacy_operation_key

CANONICAL = 9007199254740993
MERGED = 9007199254740995
EARLIER = 9007199254740997


@pytest.mark.parametrize(
    "merged",
    [
        {"legacy_free_trial_used": 1},
        {"legacy_free_trial_used": None},
        {"legacy_free_trial_used": True},
        {"legacy_free_trial_used": "0"},
        {"production_access": {"source": "legacy_user_is_active"}},
        {"legacy_free_trial_used": 0, "merged:" + str(EARLIER): {"legacy_free_trial_used": 1}},
    ],
)
def test_trial_cannot_reset_proven_merged_spent_or_unknown_history(merged):
    account = Account(
        id=CANONICAL, legacy_consent={"legacy_free_trial_used": 0, "merged:" + str(MERGED): merged}
    )
    tx = SimpleNamespace(scalars=lambda _: [MERGED, EARLIER], get=lambda *args: None)
    promos = Promos(SimpleNamespace(_has_purchase_history=lambda *args: False))
    with pytest.raises(DomainError) as failure:
        promos._require_trial_in(tx, account)
    assert failure.value.code == "trial_unavailable"


def test_explicit_unused_merged_trial_history_preserves_positive_eligibility():
    account = Account(
        id=CANONICAL,
        legacy_consent={"legacy_free_trial_used": 0, "merged:" + str(MERGED): {"legacy_free_trial_used": 0}},
    )
    tx = SimpleNamespace(scalars=lambda _: [MERGED], get=lambda *args: None)
    Promos(SimpleNamespace(_has_purchase_history=lambda *args: False))._require_trial_in(tx, account)


@pytest.mark.parametrize(
    "consent",
    [
        {"legacy_free_trial_used": 0},
        {"legacy_free_trial_used": 0, "merged:" + str(MERGED): {"legacy_free_trial_used": 0}},
        {"legacy_free_trial_used": 0, "unrelated": {"legacy_free_trial_used": 0}},
    ],
)
def test_every_proven_alias_requires_known_unused_trial_snapshot(consent):
    account = Account(id=CANONICAL, legacy_consent=consent)
    tx = SimpleNamespace(scalars=lambda _: [MERGED, EARLIER], get=lambda *args: None)
    with pytest.raises(DomainError) as denied:
        Promos(SimpleNamespace(_has_purchase_history=lambda *args: False))._require_trial_in(tx, account)
    assert denied.value.code == "trial_unavailable"


def test_complete_unused_alias_trial_evidence_and_genuine_new_account_remain_eligible():
    account = Account(
        id=CANONICAL,
        legacy_consent={
            "legacy_free_trial_used": 0,
            "merged:" + str(MERGED): {
                "legacy_free_trial_used": 0,
                "merged:" + str(EARLIER): {"legacy_free_trial_used": 0},
            },
        },
    )
    tx = SimpleNamespace(scalars=lambda _: [MERGED, EARLIER], get=lambda *args: None)
    promos = Promos(SimpleNamespace(_has_purchase_history=lambda *args: False))
    promos._require_trial_in(tx, account)
    promos._require_trial_in(
        SimpleNamespace(scalars=lambda _: [], get=lambda *args: None),
        Account(id=CANONICAL, legacy_consent={}),
    )


@pytest.mark.parametrize("value", [{"state": "review"}, {"state": "resolved"}, None])
def test_present_root_account_marker_on_proven_alias_keeps_canonical_account_fenced(value):
    marker = Setting(key=legacy_operation_key(MERGED, account=True), value=value)
    tx = SimpleNamespace(
        get=lambda model, key, **_: marker if model is Setting and key == marker.key else None,
        scalars=lambda query: [MERGED] if query.column_descriptions[0].get("entity") is Alias else [],
    )
    assert legacy_operation_in(tx, CANONICAL)
    assert legacy_operation_in(tx, CANONICAL, "new-subscription")


def test_foreign_account_marker_is_not_a_canonical_alias_grant_or_hold():
    marker = Setting(key=legacy_operation_key(MERGED, account=True), value={"state": "review"})
    tx = SimpleNamespace(
        get=lambda model, key, **_: marker if key == marker.key else None, scalars=lambda _: []
    )
    assert not legacy_operation_in(tx, CANONICAL)
