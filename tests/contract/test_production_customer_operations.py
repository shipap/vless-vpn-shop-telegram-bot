from types import SimpleNamespace

from sunrise_infrastructure.customer_operations import CustomerOperations


def test_live_customer_preflight_uses_exact_admitted_scope_without_external_http():
    calls = []
    scope = "synthetic-project"
    binding = {"scope_id": scope, "user_id": "8"}

    def lookup(session, job, sub, payload):
        calls.append(payload)
        if payload.get("scope_id") != scope:
            raise ValueError("panel_project_origin_denied")
        assert job.kind == "customer_preflight"
        return binding

    commerce = SimpleNamespace(
        live=True,
        db=None,
        runtime_policy=SimpleNamespace(scope=SimpleNamespace(scope_id=scope)),
        panel_provider=SimpleNamespace(contract="3.4_id", validate_binding=lambda value: None),
        provisioning=SimpleNamespace(_auto_binding=lookup),
    )
    sub = SimpleNamespace(panel_user_id=8, panel_uuid="preserved", environment="live")
    assert CustomerOperations(commerce)._binding(None, SimpleNamespace(id=9007199254740993), sub) == binding
    assert calls == [{"environment": "live", "scope_id": scope}]
