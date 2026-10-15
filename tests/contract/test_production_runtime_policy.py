"""Production admission uses synthetic files and in-memory transports only."""

import hashlib
import json
from contextlib import contextmanager
from types import SimpleNamespace

import fakeredis
import pytest
from sunrise_infrastructure import security, settings


def test_isolated_mode_keeps_existing_default_and_redis_keys():
    config = settings.Settings(
        _env_file=None,
        instance_config_path=None,
        database_url="postgresql+psycopg://synthetic@127.0.0.1:55439/sunrise_test",
        secret_key="synthetic-runtime-policy-key-1234567890",  # noqa: S106
    )
    assert getattr(config, "runtime_mode", None) == "isolated"
    client = fakeredis.FakeRedis()
    wrapped = security.configured_redis(config, client=client)
    security.RateGuard(wrapped).resend("synthetic")
    assert client.get("sunrise:resend:synthetic") == b"1"


def test_production_namespace_preserves_lua_args_and_atomic_auth_semantics():
    assert hasattr(security, "NamespacedRedis"), "Production Redis namespace is missing"
    raw = fakeredis.FakeRedis()
    client = security.NamespacedRedis(raw, "approved:synthetic:")
    guard = security.RateGuard(client)
    guard.resend("recipient")
    with pytest.raises(Exception, match="120 секунд"):
        guard.resend("recipient")
    for _ in range(10):
        guard.fail("login")
    assert raw.exists("approved:synthetic:sunrise:block:login")
    assert not raw.exists("sunrise:block:login")
    client.set("sunrise:oidc-flow:one", "payload", nx=True, ex=60)
    assert client.getdel("sunrise:oidc-flow:one") == b"payload"
    assert client.getdel("sunrise:oidc-flow:one") is None
    assert client.eval("return ARGV[1]", 0, "not-a-key") == b"not-a-key"
    guard.unblock("login")
    assert not raw.exists("approved:synthetic:sunrise:block:login")
    assert not hasattr(client, "execute_command")


def test_live_vendor_proof_is_strict_and_sandbox_normalization_is_separate():
    assert hasattr(settings, "payment_context_matches"), "Explicit live provider context is missing"
    matches = settings.payment_context_matches
    for flag in [None, 0, "false", True]:
        assert not matches({"environment": "prod", "test": flag}, "live")
    assert matches({"environment": "prod"}, "live")
    assert matches({"environment": "prod", "test": False}, "live")
    assert not matches({"environment": "sandbox", "test": True}, "live")
    assert not matches({"environment": "prod", "test": False}, "sandbox")
    assert matches({"environment": "sandbox", "test": True}, "sandbox")


def test_root_stage_map_cannot_promote_or_drop_required_keys():
    assert hasattr(settings, "RuntimeGates"), "Fresh root stage gates are missing"
    gate_type = settings.RuntimeGates
    fields = {key: False for key in settings.PRODUCTION_CAPABILITIES}
    valid = {"version": 1, "scope_id": "synthetic-project", "generation": 1, "gates": fields}
    assert gate_type.model_validate(valid).gates == fields
    for invalid in [
        fields | {"foreign": True},
        {k: v for k, v in fields.items() if k != "auth_mail"},
        fields | {"auth_mail": 1},
    ]:
        with pytest.raises(ValueError):
            gate_type.model_validate(valid | {"gates": invalid})


def test_fresh_root_marker_and_stage_revocation_prevent_cached_write(tmp_path, monkeypatch):
    assert hasattr(settings, "RuntimePolicy"), "Fresh production admission policy is missing"
    path = tmp_path / "admission.json"
    path.write_text('{"synthetic":true}', encoding="utf-8")
    pin = hashlib.sha256(path.read_bytes()).hexdigest()
    gates = {key: True for key in settings.PRODUCTION_CAPABILITIES}
    marker = {
        "version": 1,
        "scope_id": "synthetic-project",
        "instance_sha256": "a" * 64,
        "admission_sha256": pin,
        "source_manifest_sha256": "b" * 64,
    }
    scope = SimpleNamespace(
        scope_id="synthetic-project",
        instance_sha256="a" * 64,
        source_manifest_sha256="b" * 64,
        maximum_capabilities=gates,
    )
    config = SimpleNamespace(runtime_mode="production", production_admission_sha256=pin)
    policy = settings.RuntimePolicy(config, scope, path)
    policy._database = object()
    policy._attested = True
    monkeypatch.setattr(policy, "_check_database", lambda _: None)
    from sunrise_infrastructure.db import Setting

    rows = {
        "production_admission": SimpleNamespace(value=marker),
        "production_runtime_gates": SimpleNamespace(
            value={"version": 1, "scope_id": scope.scope_id, "generation": 1, "gates": gates.copy()}
        ),
    }
    session = SimpleNamespace(get=lambda model, key, **_: rows.get(key) if model is Setting else None)
    policy.require_current(session, "invoice_create")
    rows["production_runtime_gates"].value["gates"]["invoice_create"] = False
    with pytest.raises(Exception, match="недоступ"):
        policy.require_current(session, "invoice_create")
    policy.require_current(session, "payment_reconcile")
    policy.require_current(session, "existing_recurring_cancel")
    path.write_text(json.dumps({"synthetic": False}), encoding="utf-8")
    with pytest.raises(Exception, match="допуск"):
        policy.require_current(session, "payment_reconcile")


@pytest.fixture
def synthetic_admission(tmp_path, monkeypatch):
    from sunrise_infrastructure import instance_config

    monkeypatch.setattr(instance_config, "PROJECT_ROOT", tmp_path)
    private = tmp_path / ".private" / "production"
    private.mkdir(parents=True)
    (tmp_path / "config").mkdir()
    instance = tmp_path / "config" / "instance.json"
    instance.write_text("{}", encoding="utf-8")
    (tmp_path / "apps").mkdir()
    source = tmp_path / "apps" / "synthetic.py"
    source.write_text("# Synthetic source; never executed.\n", encoding="utf-8")
    manifest = private / "source-manifest.json"
    manifest.write_text(
        json.dumps(
            [{"path": "apps/synthetic.py", "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}]
        ),
        encoding="utf-8",
    )
    scope = {
        "version": 1,
        "scope_id": "synthetic-project",
        "database": {
            "host": "postgres.synthetic.test",
            "port": 5432,
            "name": "synthetic_live_copy",
            "role": "synthetic_app",
            "schema": "public",
            "database_oid": 12000,
            "schema_oid": 22000,
            "schema_owner": "synthetic_owner",
            "search_path": "public",
        },
        "redis": {
            "host": "redis.synthetic.test",
            "port": 6379,
            "database": 3,
            "key_prefix": "approved:synthetic:",
        },
        "instance_sha256": hashlib.sha256(instance.read_bytes()).hexdigest(),
        "source_manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        "bot_id": "100001",
        "rollypay": {
            "origin": "https://bank.synthetic.test",
            "terminal": "synthetic-terminal",
            "checkout_hosts": ["pay.synthetic.test"],
        },
        "panel": {
            "origin": "https://panel.synthetic.test",
            "contract": "3.4_id",
            "version": "3.4.5",
            "internal_squads": ["11111111-1111-4111-8111-111111111111"],
            "external_squads": [],
            "subscription_hosts": ["sub.synthetic.test"],
            "node_uuids": [],
        },
        "smtp": {
            "host": "smtp.synthetic.test",
            "port": 587,
            "mode": "starttls",
            "sender": "shop@synthetic.test",
        },
        "maximum_capabilities": {key: True for key in settings.PRODUCTION_CAPABILITIES},
    }
    admission = private / "admission.json"
    admission.write_text(json.dumps(scope), encoding="utf-8")
    config = settings.Settings(
        _env_file=None,
        runtime_mode="production",
        local_test=False,
        production_admission_file=".private/production/admission.json",
        production_admission_sha256=hashlib.sha256(admission.read_bytes()).hexdigest(),
        database_url="postgresql+psycopg://synthetic_app@postgres.synthetic.test:5432/synthetic_live_copy",
        redis_url="redis://redis.synthetic.test:6379/3",
        secret_key="synthetic-production-key-1234567890",  # noqa: S106
        cookie_secure=True,
        turnstile_test_bypass=False,
        bot_token="100001:synthetic-token-value-123456789",  # noqa: S106
        rollypay_base_url="https://bank.synthetic.test",
        rollypay_terminal="synthetic-terminal",
        panel_base_url="https://panel.synthetic.test",
        panel_contract="3.4_id",
        panel_allowed_hosts=["panel.synthetic.test"],
        smtp_host="smtp.synthetic.test",
        smtp_sender="shop@synthetic.test",
        smtp_mode="starttls",
        smtp_port=587,
    )
    policy = settings.RuntimePolicy.for_settings(config)
    identity = {key: value for key, value in scope["database"].items() if key not in {"host", "port"}}
    rows = {
        "production_admission": SimpleNamespace(value=policy._marker()),
        "production_runtime_gates": SimpleNamespace(
            value={
                "version": 1,
                "scope_id": scope["scope_id"],
                "generation": 1,
                "gates": scope["maximum_capabilities"].copy(),
            }
        ),
    }
    engine = object()
    session = SimpleNamespace(
        get_bind=lambda: engine,
        execute=lambda _: SimpleNamespace(mappings=lambda: SimpleNamespace(one=lambda: identity)),
        get=lambda model, key, **_: rows.get(key),
    )

    @contextmanager
    def transaction():
        yield session

    database = SimpleNamespace(engine=engine, transaction=transaction)
    return config, policy, database, rows, identity, manifest, source


def test_exact_production_admission_attests_without_startup_ddl(synthetic_admission):
    config, policy, database, _, _, _, _ = synthetic_admission
    assert not policy._attested
    assert policy.admit(database) is policy
    policy.require("invoice_create")
    assert config.local_test is False


@pytest.mark.parametrize("failure", ["identity", "marker", "source", "extra_source", "duplicate_manifest"])
def test_changed_scope_or_source_denies_before_any_transport(synthetic_admission, failure):
    _, policy, database, rows, identity, manifest, source = synthetic_admission
    if failure == "identity":
        identity["schema_oid"] += 1
    elif failure == "marker":
        rows["production_admission"].value["scope_id"] = "foreign"
    elif failure == "source":
        source.write_text("changed", encoding="utf-8")
    elif failure == "extra_source":
        source.with_name("extra.py").write_text("unexpected", encoding="utf-8")
    else:
        records = json.loads(manifest.read_text(encoding="utf-8"))
        manifest.write_text(json.dumps(records * 2), encoding="utf-8")
    with pytest.raises(ValueError):
        policy.admit(database)
    assert not policy._attested
