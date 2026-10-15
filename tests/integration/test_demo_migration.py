import importlib
import os
import re
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.schema import DropSchema
from sunrise_infrastructure.db import Database, Product, Setting

from scripts.run_demo_instance import settings_for_demo
from tests.integration.test_bootstrap_configuration import custom_seed


def demo_control_url():
    value = os.getenv("SUNRISE_TEST_DATABASE_URL")
    if not value:
        return None
    parsed = make_url(value)
    assert parsed.host in {"127.0.0.1", "localhost"}
    assert parsed.port == 55439 and parsed.database == "sunrise_test_backend"
    if parsed.query:
        assert set(parsed.query) == {"options"}
        assert re.fullmatch(r"-csearch_path=pytest_run_backend_[a-f0-9]{32}", parsed.query["options"])
    # Демо создаёт свою схему; родительский search_path не передаётся контроллеру.
    return parsed.set(query={}).render_as_string(hide_password=False)


def test_explicit_demo_preparation_uses_real_migrations_and_configurable_seed():
    url = demo_control_url()
    if not url:
        pytest.skip("Требуется отдельная синтетическая PostgreSQL")
    schema = "demo_instance_" + uuid4().hex
    settings = settings_for_demo(
        {
            "SUNRISE_DATABASE_URL": url,
            "SUNRISE_SECRET_KEY": "synthetic-demo-migration-key-" * 2,
            "SUNRISE_LOCAL_TEST": "true",
            "APP_NAME": "VPN Shop",
        },
        schema,
        3102,
        3202,
    )
    module = importlib.import_module("scripts.run_demo_instance")
    prepare = getattr(module, "prepare_database", None)
    assert prepare is not None, "Missing explicit demo migration preparation"
    database = Database(settings.database_url)
    try:
        prepare(settings, custom_seed())
        module.prepared_database(settings)
        with database.transaction() as session:
            assert session.execute(text("SELECT current_schema()")).scalar() == schema
            assert session.execute(text("SELECT version_num FROM v5_alembic_version")).scalar() == "v11_0019"
            assert session.scalar(select(Product)).name == "Свой тариф"
            assert session.get(Setting, "demo_instance").value["schema"] == schema
            assert session.get(Setting, "discounts").value["referral_percent"] == "8"
        with pytest.raises(RuntimeError):
            prepare(settings, custom_seed())
        with database.transaction() as session:
            assert session.scalar(select(Product)).name == "Свой тариф"
            session.get(Setting, "demo_instance").value = {"schema": schema, "prepared": False}
        with pytest.raises(RuntimeError):
            module.prepared_database(settings)
    finally:
        with database.engine.begin() as connection:
            exists = connection.scalar(
                text("SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=:schema)"), {"schema": schema}
            )
            if exists:
                assert schema.startswith("demo_instance_") and len(schema) == 46
                connection.execute(DropSchema(schema, cascade=True))
        database.engine.dispose()


def test_invalid_demo_seed_creates_no_schema():
    url = demo_control_url()
    if not url:
        pytest.skip("Требуется отдельная синтетическая PostgreSQL")
    schema = "demo_instance_" + uuid4().hex
    settings = settings_for_demo(
        {
            "SUNRISE_DATABASE_URL": url,
            "SUNRISE_SECRET_KEY": "synthetic-demo-migration-key-" * 2,
            "SUNRISE_LOCAL_TEST": "true",
        },
        schema,
        3102,
        3202,
    )
    prepare = getattr(importlib.import_module("scripts.run_demo_instance"), "prepare_database", None)
    assert prepare is not None, "Missing explicit demo migration preparation"
    seed = custom_seed()
    seed["products"][0]["included_devices"] = 0
    with pytest.raises(ValueError):
        prepare(settings, seed)
    database = Database(settings.database_url)
    try:
        with database.engine.begin() as connection:
            assert (
                connection.scalar(
                    text("SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=:schema)"),
                    {"schema": schema},
                )
                is False
            )
    finally:
        database.engine.dispose()


def test_failed_child_migration_cleans_only_new_owned_schema(monkeypatch):
    from types import SimpleNamespace

    url = demo_control_url()
    if not url:
        pytest.skip("Требуется отдельная синтетическая PostgreSQL")
    module = importlib.import_module("scripts.run_demo_instance")
    schema = "demo_instance_" + uuid4().hex
    settings = settings_for_demo(
        {
            "SUNRISE_DATABASE_URL": url,
            "SUNRISE_SECRET_KEY": "synthetic-demo-migration-key-" * 2,
            "SUNRISE_LOCAL_TEST": "true",
        },
        schema,
        3102,
        3202,
    )

    def fail_child(argv, **kwargs):
        assert kwargs["env"]["SUNRISE_DATABASE_URL"] == settings.database_url
        assert settings.database_url not in " ".join(argv)
        assert "SUNRISE_PANEL_TOKEN" not in kwargs["env"]
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(module.subprocess, "run", fail_child)
    with pytest.raises(RuntimeError, match="Миграция демо"):
        module.prepare_database(settings, custom_seed())
    database = Database(settings.database_url)
    try:
        with database.engine.begin() as connection:
            assert (
                connection.scalar(
                    text("SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=:schema)"),
                    {"schema": schema},
                )
                is False
            )
    finally:
        database.engine.dispose()


def test_demo_customer_and_admin_use_real_email_otp_sink_without_external_transports(tmp_path, monkeypatch):
    import json

    import fakeredis
    import pyotp
    from fastapi.testclient import TestClient
    from sunrise_api.main import create_app
    from sunrise_infrastructure.db import Account, now

    moment = now()
    monkeypatch.setattr("sunrise_infrastructure.admin_mfa.now", lambda: moment)

    url = demo_control_url()
    if not url:
        pytest.skip("Требуется отдельная синтетическая PostgreSQL")
    schema = "demo_instance_" + uuid4().hex
    customer, admin = uuid4().hex + "@example.test", uuid4().hex + "@example.test"
    values = {
        "SUNRISE_DATABASE_URL": url,
        "SUNRISE_SECRET_KEY": "synthetic-demo-auth-key-" * 3,
        "SUNRISE_LOCAL_TEST": "true",
        "SUNRISE_EMAIL_ALLOWLIST": json.dumps([customer, admin]),
        "SUNRISE_ADMIN_EMAIL_ALLOWLIST": json.dumps([admin]),
        "SUNRISE_OWNER_EMAIL": admin,
        "OFFER_URL": "https://example.test/demo-offer",
        "OFFER_VERSION": "demo-test",
        "PRIVACY_URL": "https://example.test/demo-privacy",
        "PRIVACY_VERSION": "demo-test",
    }
    settings = settings_for_demo(values, schema, 3102, 3202)
    settings.mail_dir = str(tmp_path)
    module = importlib.import_module("scripts.run_demo_instance")
    db = Database(settings.database_url)
    app = None
    try:
        module.prepare_database(settings, custom_seed())
        app = create_app(settings, redis_client=fakeredis.FakeRedis())
        for email, expected_admin in [(customer, False), (admin, True)]:
            with TestClient(app, base_url="http://localhost:3202", client=("127.0.0.1", 50000)) as client:
                documents = client.get("/api/v1/legal").json()["documents"]
                requested = client.post(
                    "/api/v1/auth/email/request", json={"email": email, "turnstile_token": "local-test"}
                )
                assert requested.status_code == 200
                challenge_id = requested.json()["challenge_id"]
                message = json.loads((tmp_path / (challenge_id + ".json")).read_text(encoding="utf-8"))
                assert message["to"] == email
                verified = client.post(
                    "/api/v1/auth/email/verify",
                    json={
                        "challenge_id": challenge_id,
                        "code": message["code"],
                        "legal_accepted": True,
                        "documents": documents,
                    },
                )
                assert verified.status_code == 200
                me = client.get("/api/v1/auth/me").json()
                assert me["account"]["email"] == email and me["account"]["email_verified"] is True
                if expected_admin:
                    # Email verification alone never admits staff; this fixture
                    # explicitly supplies an existing canonical Telegram binding.
                    assert me["is_admin"] is False
                    assert client.get("/api/v1/admin/access").status_code == 404
                    with db.transaction() as session:
                        account = session.get(Account, int(me["account"]["id"]))
                        assert account.telegram_id is None
                        account.telegram_id = 730000000000001
                    settings.admin_telegram_allowlist = ["730000000000001"]
                    me = client.get("/api/v1/auth/me").json()
                assert me["is_admin"] is expected_admin
                assert me["staff_role"] == ("admin" if expected_admin else None)
                assert me["mfa_verified"] is False
                assert client.get("/api/v1/dashboard").status_code == 200
                assert client.get("/api/v1/admin/access").status_code == (200 if expected_admin else 404)
                assert client.get("/api/v1/admin/overview").status_code == (403 if expected_admin else 404)
                if expected_admin:
                    headers = {"X-CSRF-Token": me["csrf_token"]}
                    # The confirmation was issued after the initial frozen clock.
                    moment = now()
                    pending = client.post("/api/v1/admin/mfa/enroll", headers=headers)
                    assert pending.status_code == 200
                    confirmed = client.post(
                        "/api/v1/admin/mfa/enroll/confirm",
                        headers=headers,
                        json={"code": pyotp.TOTP(pending.json()["secret"]).at(moment)},
                    )
                    assert confirmed.status_code == 200
                    assert client.get("/api/v1/admin/overview").status_code == 200
        assert settings.smtp_test_authorized is False and settings.owner_id is None
    finally:
        if app:
            app.state.commerce.db.engine.dispose()
        with db.engine.begin() as connection:
            if connection.scalar(
                text("SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=:schema)"), {"schema": schema}
            ):
                assert schema.startswith("demo_instance_") and len(schema) == 46
                connection.execute(DropSchema(schema, cascade=True))
        db.engine.dispose()
