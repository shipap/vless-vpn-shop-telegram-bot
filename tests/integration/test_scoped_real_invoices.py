"""Real wire semantics with synthetic PostgreSQL and in-memory provider transports."""

import hashlib
import hmac
import json
import os
import re
import time
from datetime import timedelta
from uuid import uuid4

import fakeredis
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sunrise_infrastructure.billing import Billing
from sunrise_infrastructure.commerce import Commerce, bootstrap
from sunrise_infrastructure.db import (
    Account,
    Inbox,
    Payment,
    Product,
    Segment,
    Setting,
    Subscription,
    now,
)
from sunrise_infrastructure.providers import RollyPay
from sunrise_infrastructure.settings import RuntimePolicy, Settings

from scripts.run_isolated_tests import _owned_database, _validate_url
from tests.contract.test_scoped_payment_policy import synthetic_scope  # noqa: F401 -- pytest fixture
from tests.legal_fixture import accept_test_legal


@pytest.fixture
def scoped_database(synthetic_scope):  # noqa: F811 -- pytest fixture dependency
    config, path, data = synthetic_scope
    source = os.getenv("SUNRISE_TEST_DATABASE_URL")
    if not source:
        pytest.skip("Synthetic PostgreSQL required")
    url = _validate_url(source, "sunrise_test_backend")
    with _owned_database(url, "pytest_run_backend_" + uuid4().hex) as database:
        bootstrap(database)
        data["panel"]["internal_squads"] = [
            "12345678-1234-1234-1234-123456789abc",
            "22345678-1234-1234-1234-123456789abc",
        ]
        config["panel_test_squads"] = data["panel"]["internal_squads"]
        with database.transaction() as session:
            identity = dict(
                session.execute(
                    text("""SELECT current_database() AS name, current_user AS role,
                current_schema() AS schema, d.oid::bigint AS database_oid, n.oid::bigint AS schema_oid,
                pg_get_userbyid(n.nspowner) AS schema_owner, current_setting('search_path') AS search_path
                FROM pg_database d CROSS JOIN pg_namespace n WHERE d.datname=current_database() AND n.nspname=current_schema()""")
                )
                .mappings()
                .one()
            )
        data["database"] = {"host": url.host, "port": url.port, **identity}
        path.write_text(json.dumps(data), encoding="utf-8")
        config.update(
            database_url=url.render_as_string(hide_password=False),
            scoped_test_admission_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        settings = Settings(**config)
        policy = RuntimePolicy.for_settings(settings)
        with database.transaction() as session:
            session.add(Setting(key="scoped_test_admission", value=policy._marker()))
            session.add(
                Setting(
                    key="payment_methods",
                    value={
                        "version": 1,
                        "minimum_subscription_amount": "1.00",
                        "methods": [
                            {
                                "id": "sbp",
                                "label": "СБП",
                                "provider": "rollypay",
                                "provider_method": "sbp",
                                "discount_percent": "0",
                                "enabled": True,
                                "currencies": ["RUB"],
                                "mapping_contract": "synthetic-v1",
                            }
                        ],
                    },
                )
            )
        policy.admit(database)
        object.__setattr__(settings, "_runtime_policy", policy)
        with database.transaction() as session:
            account = Account(
                telegram_id=int(data["telegram_id"]), email="owner@example.test", email_verified=True
            )
            session.add(account)
            session.flush()
            account_id = account.id
            session.add(
                Setting(
                    key="scoped_test_account:" + str(account_id),
                    value={
                        "scope_id": data["scope_id"],
                        "account_id": str(account_id),
                        "telegram_id": data["telegram_id"],
                        "origin": "new_synthetic_account",
                    },
                )
            )
            for product in session.scalars(select(Product)):
                product.internal_squad_uuid = data["panel"]["internal_squads"][
                    0 if product.code == "standard" else 1
                ]
        commerce = Commerce(database, local_test=True, public_settings=settings, runtime_policy=policy)
        accept_test_legal(commerce, account_id)
        yield database, settings, policy, commerce, account_id


def prepared_invoice(scoped_database):
    database, config, policy, commerce, account_id = scoped_database
    quote = commerce.quote(account_id, "standard", 30, "purchase", payment_method="sbp")
    order = commerce.create_order(account_id, quote["id"], uuid4().hex)
    assert order["environment"] == "scoped_live"
    created, expires = now(), now() + timedelta(hours=1)
    payment_id = "synthetic-" + uuid4().hex
    requests = []
    response = {
        "payment_id": payment_id,
        "order_id": order["id"],
        "amount": order["total"],
        "payment_currency": "RUB",
        "customer_id": str(account_id),
        "terminal_id": config.rollypay_terminal,
        "environment": "prod",
        "test": False,
        "payment_method": "sbp",
        "pay_url": "https://checkout.synthetic.invalid/own-payment",
        "created_at": created.isoformat(),
        "expires_at": expires.isoformat(),
        "paid_at": now().isoformat(),
        "status": "paid",
    }

    def handle(request):
        requests.append(
            (request.method, request.url.path, json.loads(request.content) if request.content else None)
        )
        return httpx.Response(200, json=response)

    provider = RollyPay(
        config.rollypay_base_url,
        "synthetic-key",
        config.rollypay_terminal,
        set(config.payment_allowed_hosts),
        transport=httpx.MockTransport(handle),
        environment="scoped_live",
        runtime_policy=policy,
    )
    billing = Billing(commerce, provider.terminal, "scoped_live")
    prepared = billing.prepare_payment(order["id"], provider)
    assert prepared["state"] == "created"
    return order, billing, provider, requests, response


def test_scoped_invoice_reconciles_once_without_fake_paid_or_customer_notice(scoped_database):
    database, config, policy, commerce, account_id = scoped_database
    order, billing, provider, requests, response = prepared_invoice(scoped_database)
    assert requests[0][2]["test"] is False
    assert requests[0][2]["customer_id"] == str(account_id)
    assert billing.poll_known_payments() == 1
    assert billing.reconcile_inbox(provider) == 1
    assert billing.poll_known_payments() == 0
    assert billing.reconcile_inbox(provider) == 0
    with database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Payment)) == 1
        assert session.scalar(select(func.count()).select_from(Segment)) == 1
        payment = session.scalar(select(Payment))
        subscription = session.scalar(select(Subscription))
        assert payment.environment == subscription.environment == "scoped_live"
        assert payment.provider == "rollypay"
        assert payment.fee is None
        assert subscription.panel_user_id is None
        assert subscription.status == "provisioning_pending"
    assert len(requests) == 2
    from sunrise_core.errors import DomainError

    with pytest.raises(DomainError):
        commerce.test_pay(account_id, order["id"])
    provider.client.close()


def test_scoped_paid_invoice_provisions_real_contract_and_refreshes_own_subscription(scoped_database):
    exercise_scoped_panel_renewal(scoped_database)


def exercise_scoped_panel_renewal(scoped_database):
    from sunrise_core.errors import DomainError
    from sunrise_infrastructure.panel import configured_panel
    from sunrise_infrastructure.panel_events import PanelEvents

    database, config, policy, commerce, account_id = scoped_database
    state, methods, revoke_on_get = {}, [], [False]
    config.panel_token = "synthetic-panel-key"  # noqa: S105 -- in-memory test adapter only

    def panel_request(request):
        methods.append(request.method)
        if request.method == "GET" and revoke_on_get[0]:
            with database.transaction() as session:
                row = session.get(Setting, "scoped_test_admission")
                row.value = row.value | {"scope_id": "revoked"}
            revoke_on_get[0] = False
        if request.method in {"POST", "PATCH"}:
            body = json.loads(request.content)
            short = body.get("shortUuid") or state["shortUuid"]
            state.update(
                body
                | {
                    "id": 432,
                    "activeInternalSquads": [{"uuid": body["activeInternalSquads"][0]}],
                    "subscriptionUrl": "https://vpn.synthetic.invalid/sub/" + short,
                    "userTraffic": {"usedTrafficBytes": 0},
                    "createdAt": state.get("createdAt") or now().isoformat(),
                }
            )
        if request.url.path.endswith("hwid/devices/432"):
            return httpx.Response(200, json={"response": {"devices": [], "total": 0}})
        return httpx.Response(200, json={"response": state}) if state else httpx.Response(404)

    panel = configured_panel(config, transport=httpx.MockTransport(panel_request))
    commerce.panel_provider = panel
    provisioning = commerce.provisioning
    order, billing, provider, requests, response = prepared_invoice(scoped_database)
    billing.poll_known_payments()
    assert billing.reconcile_inbox(provider) == 1
    assert provisioning.run_jobs() == 1
    with database.transaction() as session:
        subscription = session.scalar(select(Subscription))
        assert subscription.panel_user_id == 432
        assert subscription.status == "active"
        record = session.get(Setting, "panel_test:" + subscription.id)
        assert record.value["origin"] == "new_test_identity"
        assert record.value["environment"] == "scoped_live"
        assert record.value["creation_intent"]["order_id"] == order["id"]
        subscription_id = subscription.id
    # Fresh scoped identity uses its durable origin proof, then verifies the created card.
    # Unknown POST outcomes remain GET-only recovery in the dedicated callback case below.
    assert methods == ["POST", "GET"]
    events = PanelEvents(database, panel, config)
    assert events.reconcile_owned(subscription_id) is not None
    quote = commerce.quote(
        account_id, "standard", 30, "renew", subscription_id=subscription_id, payment_method="sbp"
    )
    assert quote["id"]
    renewal = commerce.create_order(account_id, quote["id"], uuid4().hex)
    response.update(
        order_id=renewal["id"],
        payment_id="renew-" + uuid4().hex,
        amount=renewal["total"],
        created_at=now().isoformat(),
        expires_at=(now() + timedelta(hours=1)).isoformat(),
        paid_at=now().isoformat(),
    )
    billing.prepare_payment(renewal["id"], provider)
    billing.poll_known_payments()
    assert billing.reconcile_inbox(provider) == 1
    assert provisioning.run_jobs() == 1
    events.reconcile_owned(subscription_id)
    next_quote = commerce.quote(
        account_id, "standard", 30, "renew", subscription_id=subscription_id, payment_method="sbp"
    )
    next_order = commerce.create_order(account_id, next_quote["id"], uuid4().hex)
    response.update(
        order_id=next_order["id"],
        payment_id="revoke-" + uuid4().hex,
        amount=next_order["total"],
        created_at=now().isoformat(),
        expires_at=(now() + timedelta(hours=1)).isoformat(),
        paid_at=now().isoformat(),
    )
    billing.prepare_payment(next_order["id"], provider)
    billing.poll_known_payments()
    assert billing.reconcile_inbox(provider) == 1
    prior = list(methods)
    revoke_on_get[0] = True
    with pytest.raises(DomainError, match="Допуск"):
        provisioning.run_jobs()
    assert methods == prior + ["GET"]
    panel.client.close()
    provider.client.close()


def test_scoped_callback_rejects_unknown_or_foreign_order_before_inbox(scoped_database, monkeypatch):
    import sunrise_api.main as api

    database, config, policy, commerce, account_id = scoped_database
    order, billing, provider, requests, response = prepared_invoice(scoped_database)
    signing = "synthetic-signing-key-with-no-real-secret"
    config.rollypay_signing_secret = signing
    monkeypatch.setattr(api, "Database", lambda _: database)
    monkeypatch.setattr(api, "configured_panel", lambda _: None)
    app = api.create_app(config, redis_client=fakeredis.FakeRedis(), payment_provider=provider)
    with TestClient(app, base_url="https://callback.synthetic.invalid") as client:
        for body, expected in [
            (
                {
                    "order_id": "foreign-order",
                    "payment_id": response["payment_id"],
                    "environment": "prod",
                    "test": False,
                },
                403,
            ),
            (
                {
                    "order_id": order["id"],
                    "payment_id": "foreign-payment",
                    "environment": "prod",
                    "test": False,
                },
                403,
            ),
            (
                {
                    "order_id": order["id"],
                    "payment_id": response["payment_id"],
                    "environment": "prod",
                    "test": False,
                },
                200,
            ),
        ]:
            raw, timestamp = json.dumps(body).encode(), str(int(time.time()))
            signature = hmac.new(
                signing.encode(), timestamp.encode() + b"." + raw, hashlib.sha256
            ).hexdigest()
            result = client.post(
                "/api/v1/webhooks/rollypay",
                content=raw,
                headers={"X-Signature": signature, "X-Timestamp": timestamp},
            )
            assert result.status_code == expected, result.text
    with database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Inbox)) == 1
    assert len(requests) == 1
    provider.client.close()


def test_scoped_actual_auth_transport_enrolls_new_verified_email_account_only(scoped_database):
    from sunrise_core.errors import DomainError
    from sunrise_infrastructure.accounts import Accounts
    from sunrise_infrastructure.mail import configured_mail
    from sunrise_infrastructure.security import RateGuard

    from tests.contract.test_production_transports import SyntheticSMTP
    from tests.legal_fixture import current_test_legal

    database, original_config, original_policy, _, owner_id = scoped_database
    path = original_policy.path
    data = json.loads(path.read_text(encoding="utf-8"))
    smtp_scope = {
        "host": "smtp.synthetic.invalid",
        "port": 587,
        "mode": "starttls",
        "sender": "auth@synthetic.invalid",
    }
    data.update(allow_email_signup=True, smtp=smtp_scope)
    path.write_text(json.dumps(data), encoding="utf-8")
    config_data = original_config.model_dump() | dict(
        smtp_test_authorized=True,
        smtp_host=smtp_scope["host"],
        smtp_port=587,
        smtp_mode="starttls",
        smtp_sender=smtp_scope["sender"],
        smtp_allowed_hosts=[smtp_scope["host"]],
        scoped_test_admission_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    config = Settings(_env_file=None, **config_data)
    policy = RuntimePolicy.for_settings(config)
    with database.transaction() as session:
        session.get(Setting, "scoped_test_admission").value = policy._marker()
    policy.admit(database)
    object.__setattr__(config, "_runtime_policy", policy)
    smtp = SyntheticSMTP()
    transport = configured_mail(config, factory=lambda **_: smtp)
    accounts = Accounts(database, RateGuard(fakeredis.FakeRedis()), config, mail_transport=transport)
    email = "new-customer@external.invalid"
    challenge = accounts.request_code(email, "127.0.0.1")
    assert len(smtp.messages) == 1

    assert smtp.messages[0]["To"] == email
    with database.transaction() as session:
        assert session.scalar(select(Account).where(Account.email == email)) is None
    code = re.search(
        r"\b[0-9]{6}\b", smtp.messages[0].get_body(preferencelist=("plain",)).get_content()
    ).group()
    _, result = accounts.verify_code(
        challenge["challenge_id"],
        code,
        "127.0.0.1",
        legal_accepted=True,
        documents=current_test_legal(Commerce(database)),
    )
    account_id = int(result["account"]["id"])
    with database.transaction() as session:
        account = session.get(Account, account_id)
        assert account.email_verified is True
        assert account.telegram_id is None
        policy.require_account_in(session, account)
        marker = session.get(Setting, "scoped_test_account:" + str(account_id))
        assert marker.value["origin"] == "new_verified_email_account"
    commerce = Commerce(database, local_test=True, public_settings=config, runtime_policy=policy)
    quote = commerce.quote(account_id, "standard", 30, "purchase", payment_method="sbp")
    assert commerce.create_order(account_id, quote["id"], uuid4().hex)["environment"] == "scoped_live"
    exercise_scoped_panel_renewal((database, config, policy, commerce, account_id))
    with database.transaction() as session:
        foreign = Account(email="old-customer@external.invalid", email_verified=True)
        session.add(foreign)
    with pytest.raises(DomainError):
        accounts.request_code("old-customer@external.invalid", "127.0.0.2")
    assert len(smtp.messages) == 1


def test_scoped_zero_external_credit_order_has_durable_service_authority(scoped_database):
    from sunrise_core.errors import DomainError
    from sunrise_infrastructure.db import CreditLedger, Order
    from sunrise_infrastructure.panel import configured_panel

    database, config, policy, commerce, account_id = scoped_database
    state = {}
    config.panel_token = "synthetic-panel-key"  # noqa: S105 -- in-memory test adapter only

    def handle(request):
        if request.url.path.endswith("hwid/devices/542"):
            return httpx.Response(200, json={"response": {"devices": [], "total": 0}})
        if request.method in {"POST", "PATCH"}:
            body = json.loads(request.content)
            short = body.get("shortUuid") or state["shortUuid"]
            state.update(
                body
                | {
                    "id": 542,
                    "activeInternalSquads": [{"uuid": body["activeInternalSquads"][0]}],
                    "subscriptionUrl": "https://vpn.synthetic.invalid/sub/" + short,
                    "userTraffic": {"usedTrafficBytes": 0},
                    "createdAt": state.get("createdAt") or now().isoformat(),
                }
            )
        return httpx.Response(200, json={"response": state}) if state else httpx.Response(404)

    commerce.panel_provider = configured_panel(config, transport=httpx.MockTransport(handle))
    provisioning = commerce.provisioning
    baseline = commerce.quote(account_id, "universal", 30, "purchase", payment_method="sbp")
    with database.transaction() as session:
        account = session.get(Account, account_id)
        account.credit_balance, account.credit_currency = 10000, "RUB"
    quote = commerce.quote(
        account_id, "universal", 30, "purchase", credit_amount=baseline["total"], payment_method="sbp"
    )
    order = commerce.create_order(account_id, quote["id"], uuid4().hex)
    assert order["status"] == "paid"
    assert order["total"] == "0.00"
    assert provisioning.run_jobs() == 1
    with database.transaction() as session:
        subscription = session.scalar(select(Subscription))
        policy.require_subscription_in(session, subscription)
        assert (
            session.get(Setting, "panel_test:" + subscription.id).value["creation_intent"]["order_id"]
            == order["id"]
        )
        policy.require_paid_service_in(session, session.get(Order, order["id"]))
        assert session.scalar(select(func.count()).select_from(Payment)) == 0
        subscription_id = subscription.id
    downgrade_quote = commerce.quote(
        account_id, "standard", 30, "downgrade", subscription_id=subscription_id, payment_method="sbp"
    )
    downgrade = commerce.create_order(account_id, downgrade_quote["id"], uuid4().hex)
    assert downgrade["total"] == "0.00"
    assert downgrade["status"] == "paid"
    assert provisioning.run_jobs() == 1
    assert state["activeInternalSquads"] == [{"uuid": "12345678-1234-1234-1234-123456789abc"}]
    with database.transaction() as session:
        policy.require_paid_service_in(session, session.get(Order, downgrade["id"]))
        assert session.scalar(select(func.count()).select_from(Payment)) == 0
        from sunrise_infrastructure.db import Quote

        source_quote = session.get(Quote, order["quote_id"])
        original_snapshot = dict(source_quote.snapshot)
        source_quote.snapshot = original_snapshot | {"action": "downgrade"}
    with database.transaction() as session, pytest.raises(DomainError, match="Источник"):
        policy.require_paid_service_in(session, session.get(Order, downgrade["id"]))
    with database.transaction() as session:
        session.get(Quote, order["quote_id"]).snapshot = original_snapshot
        ledger = session.scalar(select(CreditLedger))
        session.delete(ledger)
    with database.transaction() as session, pytest.raises(DomainError, match="Источник"):
        policy.require_paid_service_in(session, session.get(Order, order["id"]))
    commerce.panel_provider.client.close()


def test_scoped_legacy_uuid_order_cannot_send_external_invoice_post(scoped_database):
    from sunrise_core.errors import DomainError
    from sunrise_infrastructure.db import Order, ProviderOperation

    database, config, policy, commerce, account_id = scoped_database
    quote = commerce.quote(account_id, "standard", 30, "purchase", payment_method="sbp")
    placed = commerce.create_order(account_id, quote["id"], uuid4().hex)
    legacy_uuid = str(uuid4())
    with database.transaction() as session:
        order = session.get(Order, placed["id"])
        order.id = legacy_uuid
        session.flush()
        session.add(
            ProviderOperation(
                order_id=legacy_uuid,
                terminal=config.rollypay_terminal,
                state="in_flight",
                request={
                    "scope_id": policy.scope.scope_id,
                    "environment": "scoped_live",
                    "provider": "rollypay",
                    "terminal": config.rollypay_terminal,
                    "order_id": legacy_uuid,
                    "account_id": str(account_id),
                    "amount": placed["total"],
                    "currency": placed["currency"],
                    "payment_method": "sbp",
                },
            )
        )
    calls = []

    def handle(request):
        calls.append(request.method)
        return httpx.Response(200, json={})

    provider = RollyPay(
        config.rollypay_base_url,
        "synthetic-key",
        config.rollypay_terminal,
        set(config.payment_allowed_hosts),
        environment="scoped_live",
        runtime_policy=policy,
        transport=httpx.MockTransport(handle),
    )
    with pytest.raises(DomainError, match="заказ"):
        provider.create_payment(legacy_uuid, placed["total"], placed["currency"], str(account_id), "sbp")
    assert calls == []
    provider.client.close()


def test_scoped_signed_own_callback_recovers_unknown_create_without_another_post(
    scoped_database, monkeypatch
):
    import sunrise_api.main as api
    from sunrise_infrastructure.db import ProviderOperation

    database, config, policy, commerce, account_id = scoped_database
    order, billing, provider, requests, response = prepared_invoice(scoped_database)
    with database.transaction() as session:
        operation = session.scalar(select(ProviderOperation))
        operation.provider_id, operation.state, operation.pay_url = None, "outcome_unknown", None
        operation.request = {k: v for k, v in operation.request.items() if k != "live_creation"}
    signing = "synthetic-signing-key-with-no-real-secret"
    config.rollypay_signing_secret = signing
    monkeypatch.setattr(api, "Database", lambda _: database)
    monkeypatch.setattr(api, "configured_panel", lambda _: None)
    app = api.create_app(config, redis_client=fakeredis.FakeRedis(), payment_provider=provider)
    body = {
        "order_id": order["id"],
        "payment_id": response["payment_id"],
        "environment": "prod",
        "test": False,
    }
    raw, timestamp = json.dumps(body).encode(), str(int(time.time()))
    signature = hmac.new(signing.encode(), timestamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
    with TestClient(app, base_url="https://callback.synthetic.invalid") as client:
        result = client.post(
            "/api/v1/webhooks/rollypay",
            content=raw,
            headers={"X-Signature": signature, "X-Timestamp": timestamp},
        )
        assert result.status_code == 200, result.text
    with database.transaction() as session:
        assert session.scalar(select(ProviderOperation)).provider_id is None
    assert billing.reconcile_inbox(provider) == 1
    with database.transaction() as session:
        assert session.scalar(select(ProviderOperation)).provider_id == response["payment_id"]
        assert session.scalar(select(func.count()).select_from(Payment)) == 1
    assert [method for method, _, _ in requests] == ["POST", "GET"]
    provider.client.close()
