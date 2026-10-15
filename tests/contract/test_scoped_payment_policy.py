"""Scoped real invoices cannot promote the production or imported-data context."""

import hashlib
import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sunrise_infrastructure import settings
from sunrise_infrastructure.db import Account, Setting


@pytest.fixture
def synthetic_scope(tmp_path, monkeypatch):
    from sunrise_infrastructure import instance_config

    monkeypatch.setattr(instance_config, "PROJECT_ROOT", tmp_path)
    private = tmp_path / ".private/scoped-test"
    private.mkdir(parents=True)
    (tmp_path / "config").mkdir()
    instance = tmp_path / "config/instance.json"
    instance.write_text("{}", encoding="utf-8")
    data = {
        "version": 1,
        "scope_id": "synthetic-scoped",
        "database": {
            "host": "127.0.0.1",
            "port": 55439,
            "name": "sunrise_test",
            "role": "synthetic",
            "schema": "public",
            "database_oid": 123,
            "schema_oid": 456,
            "schema_owner": "synthetic",
            "search_path": "public",
        },
        "redis": {"host": "127.0.0.1", "port": 56379, "database": 12, "key_prefix": "scoped:synthetic:"},
        "instance_sha256": hashlib.sha256(instance.read_bytes()).hexdigest(),
        "bot_id": "12345",
        "telegram_id": "23456",
        "landing_origin": "https://landing.synthetic.invalid",
        "dashboard_origin": "https://dashboard.synthetic.invalid",
        "callback_origin": "https://callback.synthetic.invalid",
        "rollypay": {
            "origin": "https://bank.synthetic.invalid",
            "terminal": "synthetic-terminal",
            "checkout_hosts": ["checkout.synthetic.invalid"],
        },
        "panel": {
            "origin": "https://panel.synthetic.invalid",
            "contract": "3.4_id",
            "version": "3.4.5",
            "internal_squads": ["12345678-1234-1234-1234-123456789abc"],
            "external_squads": [],
            "subscription_hosts": ["vpn.synthetic.invalid"],
            "node_uuids": [],
        },
    }
    path = private / "admission.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    config = dict(
        _env_file=None,
        instance_config_path="config/instance.json",
        runtime_mode="scoped_test",
        local_test=True,
        cookie_secure=True,
        database_url="postgresql+psycopg://synthetic@127.0.0.1:55439/sunrise_test",
        redis_url="redis://127.0.0.1:56379/12",
        secret_key="synthetic-scoped-runtime-policy-key-123456",  # noqa: S106
        scoped_test_admission_file=".private/scoped-test/admission.json",
        scoped_test_admission_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        bot_test_token="12345:synthetic",  # noqa: S106 -- synthetic bot token only
        test_bot_ids="12345",
        test_telegram_recipients="23456",  # noqa: S106
        landing_url=data["landing_origin"],
        dashboard_url=data["dashboard_origin"],
        webhook_base_url=data["callback_origin"],
        rollypay_base_url=data["rollypay"]["origin"],
        rollypay_terminal="synthetic-terminal",
        payment_allowed_hosts=["bank.synthetic.invalid"],
        allowed_hosts=[
            "landing.synthetic.invalid",
            "dashboard.synthetic.invalid",
            "callback.synthetic.invalid",
        ],
        allowed_origins=[data["dashboard_origin"]],
        panel_base_url=data["panel"]["origin"],
        panel_contract="3.4_id",
        panel_test_authorized=True,
        panel_allowed_hosts=["panel.synthetic.invalid"],
        panel_test_squads=data["panel"]["internal_squads"],
        panel_subscription_allowed_hosts=["vpn.synthetic.invalid"],
    )
    return config, path, data


def test_scoped_config_admits_only_exact_isolated_resources(synthetic_scope):
    config, _, _ = synthetic_scope
    instance = settings.Settings(**config)
    policy = settings.RuntimePolicy.for_settings(instance)
    assert policy.payment_environment == "scoped_live"
    assert policy.production is False


@pytest.mark.parametrize(
    "override",
    [
        {"test_telegram_recipients": "23456,34567"},
        {"test_bot_ids": "99999"},
        {"rollypay_terminal": "foreign-terminal"},
        {"dashboard_url": "https://foreign.synthetic.invalid"},
        {"database_url": "postgresql+psycopg://synthetic@127.0.0.1:55439/production"},
        {"redis_url": "redis://127.0.0.1:56379/1"},
        {"recurring_test_authorized": True},
        {"email_allowlist": ["customer@real.invalid"]},
        {"panel_test_user_ids": ["42"]},
        {"panel_allowed_hosts": ["foreign.synthetic.invalid"]},
        {"local_test": False},
    ],
)
def test_scoped_config_rejects_scope_expansion(synthetic_scope, override):
    config, _, _ = synthetic_scope
    with pytest.raises(ValueError):
        settings.Settings(**(config | override))


def test_scoped_admission_hash_tampering_rejects_startup(synthetic_scope):
    config, path, data = synthetic_scope
    path.write_text(json.dumps(data | {"telegram_id": "99999"}), encoding="utf-8")
    with pytest.raises(ValueError, match="scoped_test_admission_changed"):
        settings.Settings(**config)


def test_scoped_invoice_rejects_sandbox_provider_before_database_access(tmp_path):
    from sunrise_infrastructure.billing import Billing

    policy = policy_fixture(tmp_path)
    policy.require = lambda *_: None
    commerce = SimpleNamespace(db=SimpleNamespace(), runtime_policy=policy)
    billing = Billing(commerce, "synthetic-terminal", "scoped_live")
    with pytest.raises(Exception, match="окружение"):
        billing.prepare_payment(
            "qa_own-order", SimpleNamespace(terminal="synthetic-terminal", environment="sandbox")
        )


def test_scoped_payment_context_requires_explicit_real_provider_evidence():
    assert settings.payment_context_matches({"environment": "prod", "test": False}, "scoped_live")
    assert not settings.payment_context_matches({"environment": "sandbox", "test": True}, "scoped_live")


def test_scoped_test_is_not_production_and_requires_its_own_admission():
    with pytest.raises(ValueError, match="scoped_test_admission_missing"):
        settings.Settings(
            _env_file=None,
            instance_config_path=None,
            runtime_mode="scoped_test",
            local_test=True,
            database_url="postgresql+psycopg://synthetic@127.0.0.1:55439/sunrise_test",
            secret_key="synthetic-scoped-runtime-policy-key-123456",  # noqa: S106
        )


def policy_fixture(tmp_path):
    from sunrise_infrastructure.scoped_test import ScopedTestPolicy

    path = tmp_path / "scope.json"
    path.write_text('{"synthetic":true}', encoding="utf-8")
    policy = ScopedTestPolicy(
        SimpleNamespace(scoped_test_admission_sha256=hashlib.sha256(path.read_bytes()).hexdigest()),
        SimpleNamespace(scope_id="synthetic-scoped", order_prefix="qa_", telegram_id="12345"),
        path,
    )
    policy._attested = True
    return policy


def account_fixture(policy):
    account = SimpleNamespace(id=23, telegram_id=12345, email="owner@example.test", legacy_consent={})
    marker = SimpleNamespace(
        value={
            "scope_id": "synthetic-scoped",
            "account_id": "23",
            "telegram_id": "12345",
            "origin": "new_synthetic_account",
        }
    )
    session = SimpleNamespace(
        get=lambda model, key, **_: (
            marker
            if model is Setting and key == "scoped_test_account:23"
            else account
            if model is Account and key == 23
            else None
        ),
        scalar=lambda _: None,
    )
    return account, marker, session


@pytest.mark.parametrize("change", ["foreign_telegram", "imported", "real_email", "unmarked"])
def test_scoped_payment_rejects_foreign_or_imported_account(tmp_path, change):
    policy = policy_fixture(tmp_path)
    account, marker, session = account_fixture(policy)
    if change == "foreign_telegram":
        account.telegram_id = 67890
    elif change == "imported":
        account.legacy_consent = {"source": "legacy"}
    elif change == "real_email":
        account.email = "customer@real.invalid"
    else:
        marker.value = {}
    with pytest.raises(Exception, match="владелец"):
        policy.require_account_in(session, account)


def test_scoped_payment_accepts_only_own_persisted_operation(tmp_path):
    policy = policy_fixture(tmp_path)
    account, marker, session = account_fixture(policy)
    assert policy.production is False
    policy.require_account_in(session, account)
    order = SimpleNamespace(
        id="qa_own-order", account_id=23, environment="scoped_live", amount=99, currency="RUB"
    )
    operation = SimpleNamespace(
        order_id="qa_own-order",
        terminal="own-terminal",
        provider_id="own-payment",
        state="created",
        request={
            "scope_id": "synthetic-scoped",
            "environment": "scoped_live",
            "provider": "rollypay",
            "terminal": "own-terminal",
            "order_id": "qa_own-order",
            "account_id": "23",
            "amount": "99",
            "currency": "RUB",
        },
    )
    policy.scope = SimpleNamespace(
        scope_id="synthetic-scoped",
        order_prefix="qa_",
        telegram_id="12345",
        rollypay=SimpleNamespace(terminal="own-terminal"),
    )
    policy.require_order_in(session, order, operation=operation, provider_id="own-payment")
    operation.request["scope_id"] = "foreign-scope"
    with pytest.raises(Exception, match="заказ"):
        policy.require_order_in(session, order, operation=operation, provider_id="own-payment")


def test_scoped_invoice_transport_and_recurring_are_distinct(tmp_path):
    policy = policy_fixture(tmp_path)
    with pytest.raises(Exception, match="недоступ"):
        policy.require("existing_recurring_status")
    with pytest.raises(Exception, match="недоступ"):
        policy.require("broadcast_delivery")


def test_scoped_invoice_cannot_recreate_already_saved_provider_payment(tmp_path, monkeypatch):
    from sunrise_infrastructure.db import Order, ProviderOperation

    policy = policy_fixture(tmp_path)
    account, marker, session = account_fixture(policy)
    order = SimpleNamespace(
        id="qa_own-order", account_id=23, environment="scoped_live", amount=99, currency="RUB"
    )
    operation = SimpleNamespace(
        order_id="qa_own-order",
        terminal="own-terminal",
        provider_id="own-payment",
        state="created",
        request={
            "scope_id": "synthetic-scoped",
            "environment": "scoped_live",
            "provider": "rollypay",
            "terminal": "own-terminal",
            "order_id": "qa_own-order",
            "account_id": "23",
            "amount": "99",
            "currency": "RUB",
            "payment_method": "sbp",
        },
    )
    original_get = session.get
    session.get = lambda model, key, **kw: (
        order if model is Order and key == order.id else original_get(model, key, **kw)
    )
    session.scalar = lambda query: (
        operation if query.column_descriptions[0]["entity"] is ProviderOperation else None
    )
    policy.scope = SimpleNamespace(
        scope_id="synthetic-scoped",
        order_prefix="qa_",
        telegram_id="12345",
        rollypay=SimpleNamespace(terminal="own-terminal"),
    )

    @contextmanager
    def transaction():
        yield session

    policy._database = SimpleNamespace(transaction=transaction)
    monkeypatch.setattr(policy, "require_current", lambda *_: None)
    with pytest.raises(Exception, match="заказ"):
        policy.authorize_invoice("qa_own-order", "23", "99", "RUB")


@pytest.mark.parametrize("environment", ["local_test", "sandbox", "live"])
def test_scoped_subscription_rejects_fake_or_production_existing_target(tmp_path, environment):
    policy = policy_fixture(tmp_path)
    account, marker, session = account_fixture(policy)
    subscription = SimpleNamespace(id="old-subscription", account_id=23, environment=environment)
    with pytest.raises(Exception, match="подписка"):
        policy.require_subscription_in(session, subscription)


def test_scoped_order_rejects_legacy_uuid_even_with_matching_operation(tmp_path):
    from sunrise_core.errors import DomainError

    policy = policy_fixture(tmp_path)
    account, marker, session = account_fixture(policy)
    policy.scope = SimpleNamespace(
        scope_id="synthetic-scoped",
        order_prefix="qa_",
        telegram_id="12345",
        rollypay=SimpleNamespace(terminal="own-terminal"),
    )
    identifier = "3d67a608-f07a-4dbd-92f7-5b1f937e750f"
    order = SimpleNamespace(
        id=identifier, account_id=23, environment="scoped_live", amount=99, currency="RUB"
    )
    operation = SimpleNamespace(
        order_id=identifier,
        terminal="own-terminal",
        provider_id=None,
        state="in_flight",
        request={
            "scope_id": "synthetic-scoped",
            "environment": "scoped_live",
            "provider": "rollypay",
            "terminal": "own-terminal",
            "order_id": identifier,
            "account_id": "23",
            "amount": "99",
            "currency": "RUB",
        },
    )
    with pytest.raises(DomainError, match="заказ"):
        policy.require_order_in(session, order, operation=operation)
