"""Root migration entitlements reuse a claimed slot, never fabricate old financial rows."""

import hashlib
import json
from types import SimpleNamespace

import pytest
from sunrise_core.errors import DomainError
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import Account, Setting, now
from sunrise_infrastructure.promos import (
    LegacyPromoUsageRegistry,
    legacy_pending_grant_id,
    legacy_promo_terms_sha256,
    legacy_usage_in,
    normalize_legacy_percent_grant_terms,
    validate_promo,
)

CID = 9007199254740993
SOURCE = 9007199254740995
OTHER = 9007199254740997
GID = "c" * 64
CODE = "PRECLAIMED20"


def canonical_sha(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def legacy_marker(scope="synthetic-project"):
    terms = validate_promo({"code": CODE, "percent": "20", "total_limit": 20})
    grant = {
        "source_account_id": str(CID),
        "code": CODE,
        "source_ref_sha256": "d" * 64,
        "evidence_sha256": "e" * 64,
        "terms_basis": "migration_current_effective_source_policy",
        "terms": terms,
        "terms_sha256": canonical_sha(terms),
    }
    marker = {
        "version": 1,
        "scope_id": scope,
        "environment": "live",
        "complete": True,
        "source_namespace": "legacy_shop_v1",
        "source_snapshot_sha256": "a" * 64,
        "evidence_sha256": "b" * 64,
        "approved_at": now().isoformat(),
        "codes": {CODE: {"global_claimed": 20, "source_counter": 20, "used_account_ids": [str(CID)]}},
        "pending_grants": {GID: grant},
    }
    return marker


def service(marker, actor=None, aliases=None):
    actor = actor or Account(
        id=CID,
        pending_promo={
            "id": GID,
            "legacy_grant_id": GID,
            "code": CODE,
            "percent": "20",
            "state": "pending",
            "reward_type": "percent",
        },
    )
    current = validate_promo({"code": CODE, "percent": "5", "total_limit": 20})
    rows = {
        "discounts": Setting(key="discounts", value={"promos": [current]}),
        "legacy_promo_usage": Setting(key="legacy_promo_usage", value=marker) if marker is not None else None,
    }
    tx = SimpleNamespace(
        get=lambda model, key, **kwargs: actor if model is Account and key == actor.id else rows.get(key),
        scalar=lambda *args, **kwargs: 0,
        scalars=lambda *args, **kwargs: aliases or [],
        execute=lambda *args, **kwargs: SimpleNamespace(all=lambda: []),
    )
    commerce = Commerce.__new__(Commerce)
    commerce.runtime_policy = SimpleNamespace(
        production=True,
        scope=SimpleNamespace(scope_id="synthetic-project"),
        require_current=lambda *args: None,
    )
    commerce.local_test = False
    commerce._has_purchase_history = lambda *args: False
    return commerce, tx


def test_preclaimed_grant_uses_root_migration_terms_and_binds_private_snapshot_provenance():
    marker = legacy_marker()
    commerce, tx = service(marker)
    promo = commerce._promo(tx, CID, CODE, "purchase")
    assert promo["percent"] == "20"
    context = promo["legacy_promo_grant"]
    assert context == {
        "version": 1,
        "scope_id": marker["scope_id"],
        "grant_id": GID,
        "source_account_id": str(CID),
        "code": CODE,
        "source_ref_sha256": "d" * 64,
        "evidence_sha256": "e" * 64,
        "terms_basis": "migration_current_effective_source_policy",
        "terms_sha256": marker["pending_grants"][GID]["terms_sha256"],
    }


def test_exhausted_preclaimed_code_stays_closed_to_a_new_claimant():
    commerce, tx = service(legacy_marker(), Account(id=OTHER))
    with pytest.raises(DomainError) as denied:
        commerce._promo(tx, OTHER, CODE, "purchase")
    assert denied.value.code == "promo_used"


@pytest.mark.parametrize("change", ["missing", "foreign", "terms", "owner", "ref"])
def test_pending_grant_requires_exact_current_root_authority(change):
    marker = legacy_marker()
    if change == "missing":
        marker = None
    elif change == "foreign":
        marker["scope_id"] = "foreign"
    elif change == "terms":
        marker["pending_grants"][GID]["terms_sha256"] = "f" * 64
    elif change == "owner":
        marker["pending_grants"][GID]["source_account_id"] = str(OTHER)
    else:
        marker["pending_grants"][GID]["source_ref_sha256"] = None
    commerce, tx = service(marker)
    with pytest.raises(DomainError) as denied:
        commerce._promo(tx, CID, CODE, "purchase")
    assert denied.value.code == "legacy_promo_usage_review"


@pytest.mark.parametrize("change", ["boolean_version", "foreign_field", "wrong_scope"])
def test_saved_grant_context_is_exact_and_typed(change):
    marker = legacy_marker()
    commerce, tx = service(marker)
    context = commerce._promo(tx, CID, CODE, "purchase")["legacy_promo_grant"]
    if change == "boolean_version":
        context["version"] = True
    elif change == "foreign_field":
        context["client_amount"] = "0"
    else:
        context["scope_id"] = "foreign"
    with pytest.raises(DomainError) as denied:
        legacy_usage_in(tx, commerce, CID, CODE, snapshot={"legacy_promo_grant": context})
    assert denied.value.code == "legacy_promo_usage_review"


def test_normalizer_uses_current_effective_terms_and_stable_migration_identity():
    values = {"code": CODE, "percent": 20, "enabled": True, "audience": "all", "total_limit": 20}
    normalized = normalize_legacy_percent_grant_terms(values)
    assert normalized == legacy_marker()["pending_grants"][GID]["terms"]
    assert legacy_promo_terms_sha256(values) == canonical_sha(normalized)
    assert normalize_legacy_percent_grant_terms(values | {"total_limit": 0})["total_limit"] is None
    assert legacy_pending_grant_id(
        "synthetic-project", str(CID), "raw-source-code"
    ) == legacy_pending_grant_id("synthetic-project", str(CID), "raw-source-code")
    assert legacy_pending_grant_id(
        "synthetic-project", str(CID), "raw-source-code"
    ) != legacy_pending_grant_id("synthetic-project", str(SOURCE), "raw-source-code")


@pytest.mark.parametrize("identity", [True, 1, "0", "01", "-1", "9223372036854775808"])
def test_migration_identity_rejects_noncanonical_bigint(identity):
    with pytest.raises(ValueError):
        legacy_pending_grant_id("synthetic-project", identity, CODE)


@pytest.mark.parametrize(
    "change",
    [
        "bool_version",
        "incomplete",
        "duplicate_identity",
        "counter_underflow",
        "unknown_field",
        "gift",
        "trial",
    ],
)
def test_complete_registry_cannot_invent_extra_claims_or_legacy_gifts(change):
    from pydantic import ValidationError

    marker = legacy_marker()
    if change == "bool_version":
        marker["version"] = True
    elif change == "incomplete":
        marker["complete"] = False
    elif change == "duplicate_identity":
        marker["codes"][CODE]["used_account_ids"] *= 2
    elif change == "counter_underflow":
        marker["codes"][CODE]["global_claimed"] = 19
    elif change == "unknown_field":
        marker["invented"] = True
    else:
        grant = marker["pending_grants"][GID]
        grant["terms"] = grant["terms"] | (
            {"reward_type": "free_days", "reward_days": 3, "product_id": "standard"}
            if change == "gift"
            else {"trial": True}
        )
        grant["terms_sha256"] = canonical_sha(grant["terms"])
    with pytest.raises(ValidationError):
        LegacyPromoUsageRegistry.model_validate(marker)


def test_existing_source_claims_follow_only_proven_aliases_and_unknown_baseline_denies():
    marker = legacy_marker()
    marker["codes"][CODE]["used_account_ids"] = [str(SOURCE)]
    marker["pending_grants"][GID]["source_account_id"] = str(SOURCE)
    commerce, tx = service(marker, aliases=[SOURCE])
    assert commerce._promo(tx, CID, CODE, "purchase")["legacy_promo_grant"]["source_account_id"] == str(
        SOURCE
    )
    marker["pending_grants"] = {}
    marker["codes"][CODE]["global_claimed"] = None
    commerce, tx = service(marker, Account(id=OTHER))
    with pytest.raises(DomainError) as denied:
        commerce._promo(tx, OTHER, CODE, "purchase")
    assert denied.value.code == "legacy_promo_usage_review"


@pytest.mark.parametrize("key", ["legacy_promo_usage", "legacy_promo_usage:foreign"])
def test_ordinary_admin_cannot_write_root_legacy_promo_authority(key):
    from sunrise_infrastructure.admin import Admin

    admin = Admin.__new__(Admin)

    def forbidden():
        raise AssertionError("Root-only authority reached a database write")

    admin.db = SimpleNamespace(transaction=forbidden)
    with pytest.raises(DomainError) as denied:
        admin._save_setting(CID, key, legacy_marker(), "synthetic root authority")
    assert denied.value.code == "deployment_settings_denied"
