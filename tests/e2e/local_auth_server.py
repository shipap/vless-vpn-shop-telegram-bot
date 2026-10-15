"""Настоящие FastAPI/PostgreSQL/OTP; Redis эмулируется только в этом E2E-сервере."""

import hmac
import json
import os
import re
import shutil
import socket
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import fakeredis
import pyotp
import uvicorn
from fastapi import Header, HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sunrise_api.main import create_app
from sunrise_infrastructure.commerce import bootstrap, validate_payment_methods
from sunrise_infrastructure.db import (
    Account,
    Audit,
    Base,
    CreditLedger,
    Job,
    LegalConsent,
    Order,
    Payment,
    Quote,
    Session,
    Setting,
    StaffRole,
    Subscription,
    now,
)
from sunrise_infrastructure.settings import Settings

from tests.isolated_database import isolated_database

ROOT = Path(__file__).resolve().parents[2]
HOST, PORT = "127.0.0.1", 8150


class SyntheticSettings(Settings):
    instance_config_path: str | None = None

    @classmethod
    def settings_customise_sources(
        cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings
    ):
        return (init_settings,)


def backend_url(url):
    parsed = make_url(url)
    if (
        parsed.get_backend_name() != "postgresql"
        or parsed.host not in {"127.0.0.1", "localhost"}
        or parsed.port != 55439
        or parsed.database not in {"sunrise_test", "sunrise_test_backend", "sunrise_test_bot"}
    ):
        raise ValueError("e2e_database_not_allowed")
    return parsed.set(database="sunrise_test_backend").render_as_string(hide_password=False)


def synthetic_settings(database_url, mail_dir, owner_totp):
    return SyntheticSettings(
        _env_file=None,
        database_url=database_url,
        secret_key="isolated-browser-auth-secret-key-" * 2,
        local_test=True,
        turnstile_test_bypass=True,
        email_allowlist=["customer@sunrise.test", "credit@sunrise.test", "owner@sunrise.test"],
        admin_email_allowlist=["owner@sunrise.test"],
        admin_totp_secrets={"owner@sunrise.test": owner_totp},
        allowed_hosts=[HOST],
        allowed_origins=["http://127.0.0.1:3210"],
        dashboard_url="http://127.0.0.1:3210/",
        bot_url="https://t.me/synthetic_fixture_bot",
        landing_url="http://127.0.0.1:3110/",
        mail_dir=str(mail_dir),
        smtp_test_authorized=False,
        rollypay_test_authorized=False,
        panel_test_authorized=False,
        emoji_test_authorized=False,
        recurring_test_authorized=False,
        telegram_miniapp_enabled=False,
    )


def reset_fixture(db, redis_client, schema, mode, fixture_dir):
    if mode not in {"auth", "ui"}:
        raise HTTPException(400)
    if not re.fullmatch(r"e2e_auth_test_[a-f0-9]{32}", schema):
        raise RuntimeError("invalid_isolated_schema")
    parsed = db.engine.url
    if (parsed.host, parsed.port, parsed.database) != ("127.0.0.1", 55439, "sunrise_test_backend"):
        raise RuntimeError("isolated_database_not_allowed")
    with db.engine.begin() as connection:
        if connection.scalar(text("SELECT current_schema()")) != schema:
            raise RuntimeError("isolated_schema_not_current")
        names = ", ".join('"' + table.name + '"' for table in Base.metadata.sorted_tables)
        connection.execute(text("TRUNCATE TABLE " + names + " RESTART IDENTITY CASCADE"))
    redis_client.flushall()
    bootstrap(db)
    from sunrise_infrastructure.commerce import Commerce

    Commerce(db).legal.configure(
        None,
        [
            {"document_type": "offer", "version": "v1", "url": "https://legal.example.test/offer"},
            {"document_type": "privacy", "version": "v1", "url": "https://legal.example.test/privacy"},
        ],
        "Изолированные синтетические документы браузерного теста",
    )
    if mode == "auth":
        return {"mode": mode, "accounts": 0}
    moment = now()
    with db.transaction() as session:
        session.add(
            Setting(
                key="payment_methods",
                value=validate_payment_methods(
                    {
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
                                "mapping_contract": "synthetic-reviewed-contract",
                            }
                        ],
                    }
                ),
            )
        )
        customer = Account(id=9007199254740993, email="customer@sunrise.test", email_verified=True)
        credit = Account(
            id=9007199254740994,
            email="credit@sunrise.test",
            email_verified=True,
            credit_balance=Decimal("99.00"),
            credit_reserved=Decimal("0"),
            credit_currency="RUB",
            credit_provenance="owner_adjustment",
        )
        owner = Account(id=9007199254740995, email="owner@sunrise.test", email_verified=True)
        session.add_all([customer, credit, owner])
        session.flush()
        session.add(StaffRole(account_id=owner.id, role="owner", granted_by=owner.id))
        subscription = Subscription(
            id="e2e-paid-standard",
            account_id=customer.id,
            product_id="standard",
            name="Тестовая подписка",
            status="active",
            expires_at=moment + timedelta(days=30),
            devices_limit=4,
            environment="local_test",
        )
        quote = Quote(
            id="e2e-paid-quote",
            account_id=customer.id,
            snapshot={
                "product_id": "standard",
                "action": "purchase",
                "period_days": 30,
                "total": "99.00",
                "currency": "RUB",
            },
            expires_at=moment + timedelta(days=30),
        )
        session.add_all([subscription, quote])
        session.flush()
        order = Order(
            id="e2e-paid-order",
            account_id=customer.id,
            quote_id=quote.id,
            idempotency_key="e2e-paid-order",
            status="paid",
            amount=Decimal("99.00"),
            currency="RUB",
            source="site",
            environment="local_test",
            subscription_id=subscription.id,
            fulfilled_at=moment,
        )
        session.add(order)
        session.flush()
        session.add_all(
            [
                CreditLedger(
                    account_id=credit.id,
                    original_account_id=credit.id,
                    operation="e2e-opening-credit",
                    amount=Decimal("99.00"),
                    currency="RUB",
                    provenance="owner_adjustment",
                ),
                Payment(
                    id="e2e-paid-payment",
                    provider="synthetic",
                    provider_id="e2e-paid-payment",
                    terminal="synthetic",
                    environment="local_test",
                    order_id=order.id,
                    account_id=customer.id,
                    amount=Decimal("99.00"),
                    currency="RUB",
                    status="paid",
                ),
                Job(
                    id="e2e-provision-job",
                    key="provision:e2e-paid-order",
                    kind="provision",
                    account_id=customer.id,
                    payload={"order_id": order.id},
                    state="failed",
                    attempts=1,
                ),
                Setting(
                    key="content:ui_quality_check",
                    value={"nodes": [{"type": "text", "text": "Исходный тестовый материал"}], "version": 1},
                ),
                Audit(
                    actor_id=customer.id,
                    action="content_update",
                    target="ui_quality_check",
                    reason="Синтетическая исходная версия материала",
                    details={
                        "before": {"nodes": [], "version": 0},
                        "after": {
                            "nodes": [{"type": "text", "text": "Исходный тестовый материал"}],
                            "version": 1,
                        },
                    },
                ),
                Audit(
                    actor_id=customer.id,
                    action="login_event",
                    target="e2e-customer-login",
                    reason="verified_code",
                    details={
                        "email": "customer@sunrise.test",
                        "challenge_id": "e2e-customer-login",
                        "account_id": str(customer.id),
                        "result": "success",
                        "ip": "127.0.0.1",
                        "ip_provenance": "direct",
                    },
                ),
            ]
        )
    (fixture_dir / "cash-credit-fixture.json").write_text(
        json.dumps(
            {
                "email": "credit@sunrise.test",
                "account_id": str(credit.id),
                "product_id": "standard",
                "amount": "99.00",
                "currency": "RUB",
                "environment": "synthetic_local",
            }
        ),
        encoding="utf-8",
    )
    (fixture_dir / "bigint-fixture.json").write_text(json.dumps({"id": str(customer.id)}), encoding="utf-8")
    return {"mode": mode, "accounts": 3}


def main():
    os.chdir(ROOT)
    run_id = os.environ.get("SUNRISE_E2E_AUTH_RUN_ID", "")
    if not re.fullmatch(r"[a-f0-9-]{36}", run_id):
        raise ValueError("e2e_run_id_required")
    with socket.socket() as probe:
        probe.settimeout(1)
        if probe.connect_ex((HOST, PORT)) == 0:
            raise ValueError("e2e_port_already_owned")
    source = Settings(_env_file=ROOT / ".env")
    os.environ["SUNRISE_TEST_DATABASE_URL"] = backend_url(source.database_url)
    lifecycle = ROOT / ".artifacts" / "e2e-auth-lifecycle.json"
    lifecycle.parent.mkdir(parents=True, exist_ok=True)
    lifecycle.write_text(json.dumps({"run_id": run_id, "schema_dropped": False}), encoding="utf-8")
    fixture_parent = (ROOT / ".private" / "e2e-ui-fixtures").resolve()
    fixture_dir = (fixture_parent / run_id).resolve()
    if not fixture_parent.is_relative_to(ROOT.resolve()) or fixture_dir.parent != fixture_parent:
        raise ValueError("e2e_fixture_path_invalid")
    fixture_dir.mkdir(parents=True, exist_ok=False)
    owner_totp = pyotp.random_base32()
    (fixture_dir / "owner.json").write_text(
        json.dumps({"run_id": run_id, "totp_secret": owner_totp}), encoding="utf-8"
    )
    with isolated_database("e2e_auth_test") as db:
        schema = db.engine.url.query["options"].removeprefix("-csearch_path=")
        if not re.fullmatch(r"e2e_auth_test_[a-f0-9]{32}", schema):
            raise ValueError("invalid_isolated_schema")
        lifecycle.write_text(
            json.dumps({"run_id": run_id, "schema": schema, "schema_dropped": False}),
            encoding="utf-8",
        )
        config = synthetic_settings(
            db.engine.url.render_as_string(hide_password=False), fixture_dir / "mail", owner_totp
        )
        redis_client = fakeredis.FakeRedis()
        app = create_app(config, redis_client=redis_client)
        app.state.commerce.legal.configure(
            None,
            [
                {"document_type": "offer", "version": "v1", "url": "https://legal.example.test/offer"},
                {"document_type": "privacy", "version": "v1", "url": "https://legal.example.test/privacy"},
            ],
            "Изолированные синтетические документы браузерного теста",
        )
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=HOST,
                port=PORT,
                access_log=False,
                proxy_headers=False,
                log_level="critical",
                log_config=None,
            )
        )

        @app.get("/__e2e__/status")
        def status():
            with db.transaction() as tx:
                counts = {
                    name: tx.scalar(select(func.count()).select_from(model))
                    for name, model in (
                        ("accounts", Account),
                        ("sessions", Session),
                        ("legal_consents", LegalConsent),
                    )
                }
            return {"environment": "isolated-e2e-auth", "run_id": run_id, "redis_mode": "emulated", **counts}

        @app.post("/__e2e__/ui/setup")
        def setup(mode: str = "ui", x_e2e_run_id: str = Header(default="")):
            if not hmac.compare_digest(x_e2e_run_id, run_id):
                raise HTTPException(404)
            return {"run_id": run_id, **reset_fixture(db, redis_client, schema, mode, fixture_dir)}

        @app.post("/__e2e__/shutdown")
        def shutdown(x_e2e_run_id: str = Header(default="")):
            if not hmac.compare_digest(x_e2e_run_id, run_id):
                raise HTTPException(404)
            server.should_exit = True
            return {"stopping": True}

        try:
            server.run()
        finally:
            app.state.commerce.db.engine.dispose()
    if (
        not fixture_parent.is_relative_to(ROOT.resolve())
        or fixture_dir.resolve().parent != fixture_parent
        or fixture_dir.name != run_id
    ):
        raise ValueError("e2e_fixture_cleanup_path_invalid")
    shutil.rmtree(fixture_dir)
    lifecycle.write_text(
        json.dumps({"run_id": run_id, "schema": schema, "schema_dropped": True}),
        encoding="utf-8",
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:  # noqa: BLE001 -- private configuration/transport errors never reach logs
        raise SystemExit("Synthetic E2E auth server stopped: " + type(error).__name__) from None
