"""An opaque probe admits only current scoped panel identities."""

from types import SimpleNamespace

import pytest
from sunrise_api.remna_membership import admitted_resource_in
from sunrise_api.webhook_router import remna_resource_digest


class Rows:
    def __init__(self, subscriptions=(), binding_user_id=None):
        self.subscriptions = list(subscriptions)
        self.binding_user_id = binding_user_id
        self.query = None

    def scalars(self, _query):
        self.query = str(_query.compile(compile_kwargs={"literal_binds": True}))
        return self.subscriptions

    def get(self, model, identifier, **_kwargs):
        if model.__name__ == "Setting" and identifier == "panel_test:sub":
            return SimpleNamespace(value={"binding": {"user_id": self.binding_user_id}})
        if model.__name__ == "Account" and identifier == 1:
            return SimpleNamespace(id=1)
        return None


class Scope:
    scoped_payments = True
    scope = SimpleNamespace(scope_id="synthetic-scope", panel=SimpleNamespace(node_uuids=("admitted-node",)))

    def __init__(self):
        self.current = 0
        self.checked = []

    def require_current(self, _session):
        self.current += 1

    def require_subscription_in(self, _session, subscription):
        self.checked.append(subscription.panel_user_id)


def context(subscriptions=(), binding_user_id=None):
    policy = Scope()
    panel = SimpleNamespace(contract="3.4_id", nodes={"admitted-node", "unrelated-node"})
    commerce = SimpleNamespace(runtime_policy=policy, panel_provider=panel)
    settings = SimpleNamespace(panel_test_node_uuids=["admitted-node"])
    return Rows(subscriptions, binding_user_id), commerce, settings, policy


def test_node_probe_uses_current_admission_not_unmapped_news_routes():
    rows, commerce, settings, policy = context()
    key = "synthetic-key"
    assert admitted_resource_in(
        rows, commerce, settings, "node", remna_resource_digest(key, "node", "admitted-node"), key
    )
    assert not admitted_resource_in(
        rows, commerce, settings, "node", remna_resource_digest(key, "node", "unrelated-node"), key
    )
    assert policy.current == 4


def test_user_probe_requires_exact_paid_scoped_binding(monkeypatch):
    sub = SimpleNamespace(panel_user_id=123, environment="scoped_live", account_id=1, id="sub")
    rows, commerce, settings, policy = context([sub], binding_user_id="123")
    monkeypatch.setattr("sunrise_api.remna_membership.registered_owner", lambda *_args: True)
    key = "synthetic-key"
    assert admitted_resource_in(
        rows, commerce, settings, "user", remna_resource_digest(key, "user", "123"), key
    )
    assert policy.checked == [123]
    assert "scoped_test_account:" in rows.query and "scope_id" in rows.query
    assert not admitted_resource_in(
        rows, commerce, settings, "user", remna_resource_digest(key, "user", "124"), key
    )
    assert policy.checked == [123]


def test_failed_own_binding_is_probe_failure_not_negative(monkeypatch):
    sub = SimpleNamespace(panel_user_id=123, environment="scoped_live", account_id=1, id="sub")
    rows, commerce, settings, policy = context([sub], binding_user_id="999")
    monkeypatch.setattr("sunrise_api.remna_membership.registered_owner", lambda *_args: True)
    with pytest.raises(ValueError, match="scoped_remna_binding_denied"):
        admitted_resource_in(
            rows,
            commerce,
            settings,
            "user",
            remna_resource_digest("synthetic-secret", "user", "123"),
            "synthetic-secret",
        )
