"""Malformed access metadata fails closed without touching payment records."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sunrise_infrastructure.db import Alias, Setting
from sunrise_infrastructure.panel_entitlements import effective_end, owned_account, verified_observation


@pytest.mark.parametrize(
    "entry", [None, [], 42, {}, {"origin": "verified_panel_snapshot", "effective_ends_at": "invalid-time"}]
)
def test_invalid_effective_window_returns_no_access_interval(entry):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    segment = SimpleNamespace(id="paid-segment", starts_at=start, ends_at=start + timedelta(days=30))
    sub = SimpleNamespace(id="own-sub", environment="local_test")
    row = SimpleNamespace(
        value={"subscription_id": sub.id, "environment": sub.environment, "segments": {segment.id: entry}}
    )
    session = SimpleNamespace(get=lambda *_: row)
    assert effective_end(session, sub, segment) == start


def test_effective_window_keeps_verified_shortening_even_when_binding_is_temporarily_stale():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    segment = SimpleNamespace(id="paid-segment", starts_at=start, ends_at=start + timedelta(days=30))
    sub = SimpleNamespace(id="own-sub", environment="local_test")
    cap = start + timedelta(days=10)
    row = SimpleNamespace(
        value={
            "subscription_id": sub.id,
            "environment": sub.environment,
            "binding": {"uuid": "old-own-identity"},
            "segments": {
                segment.id: {"origin": "verified_panel_snapshot", "effective_ends_at": cap.isoformat()}
            },
        }
    )
    assert effective_end(SimpleNamespace(get=lambda *_: row), sub, segment) == cap


@pytest.mark.parametrize(
    "invalid",
    [
        "proof_subscription",
        "proof_environment",
        "proof_owner",
        "actual_uuid",
        "actual_short_uuid",
        "live_environment",
        "last_error",
        "zero_version",
        "negative_version",
        "boolean_version",
        "future_version",
        "observation_subscription",
        "observation_source",
        "observation_account",
        "observation_environment",
        "observation_binding",
        "payload_null",
        "payload_list",
        "actual_numeric_identity",
    ],
)
def test_observation_cannot_prove_current_identity_when_scope_or_success_is_invalid(invalid):
    binding = {"uuid": "20000000-0000-4000-8000-000000000002", "short_uuid": "owned-short-id"}
    sub = SimpleNamespace(
        id="own-sub",
        account_id=301,
        environment="local_test",
        version=4,
        panel_uuid=binding["uuid"],
        panel_user_id=None,
        short_uuid=binding["short_uuid"],
    )
    proof = SimpleNamespace(
        value={
            "subscription_id": sub.id,
            "account_id": "301",
            "environment": sub.environment,
            "binding": binding,
        }
    )
    record = SimpleNamespace(
        subscription_id=sub.id,
        last_error=None,
        payload={
            "source": "remnawave",
            "binding": binding,
            "environment": sub.environment,
            "account_id": "301",
            "subscription_version": 4,
        },
    )
    if invalid.startswith("proof_"):
        field = {
            "proof_subscription": "subscription_id",
            "proof_environment": "environment",
            "proof_owner": "account_id",
        }[invalid]
        proof.value[field] = "foreign"
    elif invalid == "actual_uuid":
        sub.panel_uuid = "20000000-0000-4000-8000-000000000009"
    elif invalid == "actual_short_uuid":
        sub.short_uuid = "different-short-id"
    elif invalid == "last_error":
        record.last_error = "panel_unavailable"
    elif invalid == "live_environment":
        sub.environment = proof.value["environment"] = record.payload["environment"] = "live"
    elif invalid == "actual_numeric_identity":
        sub.panel_uuid, sub.panel_user_id = None, 123
        binding.pop("uuid")
        binding["user_id"] = "999"
    elif invalid == "observation_subscription":
        record.subscription_id = "foreign"
    elif invalid.startswith("observation_"):
        field = {
            "observation_source": "source",
            "observation_account": "account_id",
            "observation_environment": "environment",
            "observation_binding": "binding",
        }[invalid]
        record.payload[field] = "foreign"
    elif invalid.startswith("payload_"):
        record.payload = None if invalid == "payload_null" else []
    else:
        record.payload["subscription_version"] = {
            "zero_version": 0,
            "negative_version": -1,
            "boolean_version": True,
            "future_version": 5,
        }[invalid]
    session = SimpleNamespace(
        scalar=lambda *_: record,
        get=lambda model, key: proof if model is Setting and key == "panel_test:own-sub" else None,
    )
    assert verified_observation(session, sub) is None


@pytest.mark.parametrize("numeric", [False, True])
def test_successful_scoped_observation_retains_exact_registered_identity(numeric):
    binding = (
        {"user_id": "123", "short_uuid": "owned-short-id"}
        if numeric
        else {
            "uuid": "20000000-0000-4000-8000-000000000002",
            "short_uuid": "owned-short-id",
        }
    )
    sub = SimpleNamespace(
        id="own-sub",
        account_id=301,
        environment="local_test",
        version=4,
        panel_uuid=None if numeric else binding["uuid"],
        panel_user_id=123 if numeric else None,
        short_uuid=binding["short_uuid"],
    )
    proof = SimpleNamespace(
        value={
            "subscription_id": sub.id,
            "account_id": "301",
            "environment": sub.environment,
            "binding": binding,
        }
    )
    record = SimpleNamespace(
        subscription_id=sub.id,
        last_error=None,
        payload={
            "source": "remnawave",
            "binding": dict(binding),
            "environment": sub.environment,
            "account_id": "301",
            "subscription_version": 4,
        },
    )
    session = SimpleNamespace(
        scalar=lambda *_: record,
        get=lambda model, key: proof if model is Setting and key == "panel_test:own-sub" else None,
    )
    assert verified_observation(session, sub) is record


@pytest.mark.parametrize("owner", [None, True, -1, "0", "0300", "٣٠٠", "9223372036854775808", "9" * 5000])
def test_invalid_owner_never_reaches_alias_lookup(owner):
    lookups = []
    session = SimpleNamespace(get=lambda model, key: lookups.append((model, key)))
    assert owned_account(session, SimpleNamespace(account_id=301), owner) is False
    assert lookups == []


def test_verified_canonical_account_alias_preserves_owned_observation():
    session = SimpleNamespace(
        get=lambda model, key: SimpleNamespace(canonical_id=301) if model is Alias and key == 300 else None
    )
    assert owned_account(session, SimpleNamespace(account_id=301), "300") is True


@pytest.fixture
def scoped_authority(monkeypatch):
    from sunrise_infrastructure import panel_entitlements as rights
    from sunrise_infrastructure.db import Product

    binding = {"uuid": "20000000-0000-4000-8000-000000000002", "short_uuid": "owned"}
    sub = SimpleNamespace(
        id="own",
        account_id=301,
        environment="local_test",
        version=4,
        panel_uuid=binding["uuid"],
        panel_user_id=None,
        short_uuid="owned",
        product_id="product",
        expires_at=rights.now() + timedelta(days=3),
    )
    proof = SimpleNamespace(
        value=dict(subscription_id=sub.id, account_id="301", environment=sub.environment, binding=binding)
    )
    data = dict(
        source="remnawave",
        account_id="301",
        environment=sub.environment,
        binding=binding,
        subscription_version=4,
        devices_limit=4,
        internal_squad_uuids=["squad"],
        panel_product_id="product",
    )
    record = SimpleNamespace(
        subscription_id=sub.id, payload=data, status="ACTIVE", last_error=None, observed_at=rights.now()
    )
    marker = SimpleNamespace(value=proof.value | dict(version=4, adopted_at=rights.now().isoformat()))
    product = SimpleNamespace(id="product", available=True)

    def get(model, key):
        if model is Product:
            return product
        return {"panel_test:own": proof, rights.override_key("own"): marker}.get(key)

    session = SimpleNamespace(get=get, scalar=lambda *_: record, scalars=lambda *_: [])
    monkeypatch.setattr(rights, "pending_target", lambda *_: False)
    monkeypatch.setattr(rights, "paid_horizon", lambda *_: None)
    monkeypatch.setattr(
        rights, "mapped_product", lambda *_: {"panel_product_state": "matched", "panel_product_id": "product"}
    )
    return rights, session, sub, record, marker


@pytest.mark.parametrize(
    "field,value",
    [
        ("binding", {}),
        ("account_id", "999"),
        ("environment", "live"),
        ("source", "webhook"),
        ("subscription_version", True),
        ("subscription_version", 5),
        ("last_error", "failed"),
    ],
)
def test_shared_pricing_denies_unverified_observation(scoped_authority, field, value):
    from sunrise_core.errors import DomainError

    rights, session, sub, record, _ = scoped_authority
    if field == "last_error":
        record.last_error = value
    else:
        record.payload[field] = value
    assert rights.verified_observation(session, sub) is None
    assert rights.facts(session, sub)["renewal_pricing_state"] == "blocked"
    with pytest.raises(DomainError):
        rights.require_pricing(session, sub)


@pytest.mark.parametrize(
    "field,value",
    [
        ("account_id", "999"),
        ("binding", {}),
        ("version", 0),
        ("version", True),
        ("version", 5),
        ("payload", None),
        ("payload", []),
    ],
)
def test_override_requires_scoped_positive_authority(scoped_authority, field, value):
    rights, session, sub, _, marker = scoped_authority
    if field == "payload":
        marker.value = value
    else:
        marker.value[field] = value
    assert rights.override(session, sub) is None


@pytest.mark.parametrize(
    "invalid",
    [
        "no_proof",
        "zero_limit",
        "large_limit",
        "unknown_status",
        "malformed_chain",
        "payload_null",
        "payload_list",
    ],
)
def test_own_target_never_authorizes_malformed_snapshot(scoped_authority, invalid):
    rights, session, sub, _, _ = scoped_authority
    binding = rights.registered_binding(session, sub)
    expiry = sub.expires_at.isoformat()
    target = dict(
        subscription_id=sub.id,
        environment=sub.environment,
        version=4,
        panel_binding=binding,
        panel_status="DISABLED",
        expires_at=expiry,
        devices_limit=4,
        internal_squad_uuid="squad",
    )
    job = SimpleNamespace(id="target", account_id=301, payload=target)
    session.scalars = lambda *_: [job]
    snapshot = dict(status="DISABLED", expires_at=expiry, devices_limit=4, internal_squad_uuids=["squad"])
    rotations = []
    if invalid == "no_proof":
        session.get = lambda *_: None
    elif invalid.endswith("limit"):
        target["devices_limit"] = snapshot["devices_limit"] = 0 if invalid == "zero_limit" else 101
    elif invalid == "unknown_status":
        target["panel_status"] = snapshot["status"] = "UNKNOWN"
    elif invalid == "malformed_chain":
        target["panel_binding"] = binding | {"short_uuid": "old"}
        rotations = None
    else:
        job.payload = None if invalid == "payload_null" else []
    assert rights.own_snapshot_target(session, sub, snapshot, binding, rotations) is None


@pytest.mark.parametrize("invalid", [None, [], {"account_id": "9" * 5000}, {"version": 0}])
def test_rotation_ignores_malformed_or_unscoped_access_metadata(scoped_authority, invalid):
    rights, session, sub, _, marker = scoped_authority
    previous = rights.registered_binding(session, sub)
    original = marker.value if invalid is None or isinstance(invalid, list) else marker.value | invalid
    if invalid is None or isinstance(invalid, list):
        original = invalid
    marker.value = original
    rights.rebase_rotation(session, sub, previous, previous | {"short_uuid": "new"}, 4, "rotation")
    assert marker.value == original
