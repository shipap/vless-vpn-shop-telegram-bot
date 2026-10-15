from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sunrise_core.errors import DomainError
from sunrise_infrastructure import panel_entitlements, retention
from sunrise_infrastructure.admin import Admin
from sunrise_infrastructure.db import Account, Setting, Subscription, now


def live_target():
    account = Account(id=9007199254740993)
    sub = Subscription(
        id="own-import",
        account_id=account.id,
        environment="live",
        panel_user_id=8,
        panel_uuid="22222222-2222-4222-8222-222222222222",
        expires_at=now() - timedelta(days=8),
        status="expired",
        version=1,
    )
    binding = {
        "scope_id": "synthetic-project",
        "generation": 1,
        "origin_nonce": "a" * 32,
        "user_id": "8",
        "uuid": sub.panel_uuid,
        "short_uuid": "syntheticshortuuid123",
        "username": "preserved_customer",
    }
    proof = {
        "origin": "imported_project_identity",
        "scope_id": "synthetic-project",
        "generation": 1,
        "binding": binding,
    }
    authorization = {
        "version": 1,
        "state": "approved",
        "scope_id": "synthetic-project",
        "environment": "live",
        "subscription_id": sub.id,
        "account_id": str(account.id),
        "origin_nonce": binding["origin_nonce"],
        "panel_user_id": "8",
        "binding_generation": 1,
        "legacy_uuid": sub.panel_uuid,
        "evidence_sha256": "b" * 64,
        "approved_at": now().isoformat(),
    }
    return account, sub, binding, proof, authorization


def test_live_retention_state_carries_exact_production_binding_without_changing_test_binding_shape():
    _, sub, binding, _, _ = live_target()
    kwargs = dict(
        subscription_id=sub.id,
        owner_id=str(sub.account_id),
        environment="live",
        contract="3.4_id",
        panel_host="panel.synthetic.test",
        binding=binding,
        target_version=1,
        expires_at=sub.expires_at.isoformat(),
        deadline=(sub.expires_at + timedelta(days=7)).isoformat(),
        job_id="cleanup",
        phase="pending",
    )
    state = retention.RetentionState(**kwargs)
    assert state.binding.model_dump() == binding
    isolated = {key: value for key, value in binding.items() if key not in {"scope_id", "generation", "uuid"}}
    assert (
        retention.RetentionState(
            **(kwargs | {"environment": "local_test", "binding": isolated})
        ).binding.model_dump()
        == isolated
    )
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        retention.RetentionState(**(kwargs | {"environment": "local_test"}))


@pytest.mark.parametrize(
    "delta",
    [
        {},
        None,
        {"state": "revoked"},
        {"scope_id": "foreign"},
        {"version": True},
        {"binding_generation": 2},
        {"panel_user_id": "9"},
        {"legacy_uuid": None},
        {"evidence_sha256": "bad"},
    ],
)
def test_imported_retention_requires_exact_root_authorization_and_keeps_original_origin(monkeypatch, delta):
    account, sub, binding, proof, authorization = live_target()
    auth = None if delta is None else authorization | delta
    marker = Setting(key="import_retention:" + sub.id, value=auth) if auth is not None else None
    tx = SimpleNamespace(
        get=lambda model, key, **_: (
            account
            if model is Account
            else marker
            if key == "import_retention:" + sub.id
            else Setting(key="subscription_retention", value={"enabled": True, "days_after_expiry": 7})
            if key == "subscription_retention"
            else None
        ),
        scalars=lambda *args: [],
        scalar=lambda *args: None,
    )
    policy = SimpleNamespace(
        production=True,
        scope=SimpleNamespace(scope_id="synthetic-project"),
        require_current=lambda *args: None,
    )
    commerce = SimpleNamespace(
        db=None,
        live=True,
        runtime_policy=policy,
        panel_provider=SimpleNamespace(contract="3.4_id", api_host="panel.synthetic.test"),
    )
    service = retention.RetentionService(commerce, SimpleNamespace())
    monkeypatch.setattr(service, "_owned", lambda *args: (binding, proof))
    monkeypatch.setattr(service, "_holds", lambda *args: None)
    monkeypatch.setattr(panel_entitlements, "verified_service_horizon", lambda *args: sub.expires_at)
    decision = service.decision_in(tx, sub, now())
    assert decision.eligible is (delta == {})
    assert proof["origin"] == "imported_project_identity"


def test_ordinary_admin_editor_cannot_issue_import_retention_permission():
    @contextmanager
    def transaction():
        yield SimpleNamespace(get=lambda *args: None, execute=lambda *args: None, add=lambda *args: None)

    admin = Admin(
        SimpleNamespace(db=SimpleNamespace(transaction=transaction)), None, accounts=SimpleNamespace()
    )
    admin.audit = lambda *args: None
    with pytest.raises(DomainError) as failure:
        admin._save_setting(
            9007199254740993,
            "import_retention:own-import",
            {"state": "approved"},
            "Недопустимое расширение допуска",
        )
    assert failure.value.code == "deployment_settings_denied"
