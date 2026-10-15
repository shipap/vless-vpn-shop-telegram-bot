import json
import os
from datetime import timedelta
from uuid import uuid4

import fakeredis
import pyotp
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema, DropSchema
from sunrise_api.main import create_app
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import Base, Database, Session, Setting, now
from sunrise_infrastructure.settings import Settings


@pytest.fixture
def mfa_context(tmp_path, monkeypatch, request):
    # Один момент для генерации и проверки TOTP: смена 30-секундного окна
    # во время SQL/HTTP не должна превращать позитивный сценарий в случайный отказ.
    moment = now()
    monkeypatch.setattr("sunrise_infrastructure.admin_mfa.now", lambda: moment)
    monkeypatch.setattr("sunrise_infrastructure.accounts.now", lambda: moment)
    monkeypatch.setattr(__name__ + ".now", lambda: moment)
    monkeypatch.setattr(pyotp.TOTP, "now", lambda self: self.at(moment))
    url = os.getenv("SUNRISE_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Synthetic PostgreSQL required")
    parsed = make_url(url)
    assert parsed.database == "sunrise_test_backend" and parsed.host in {"127.0.0.1", "localhost"}
    schema = "mfa_test_" + uuid4().hex
    base = Database(url)
    with base.engine.begin() as connection:
        connection.execute(CreateSchema(schema))
    scoped = parsed.set(query={"options": "-csearch_path=" + schema}).render_as_string(hide_password=False)
    db = Database(scoped)
    Base.metadata.create_all(db.engine)
    email = uuid4().hex + "@example.test"
    outsider = uuid4().hex + "@example.test"
    config = Settings(
        _env_file=None,
        instance_config_path=None,
        app_name=getattr(request, "param", "Северный VPN"),
        database_url=scoped,
        secret_key="m" * 48,
        local_test=True,
        turnstile_test_bypass=True,
        owner_id="202",
        email_allowlist=[email, outsider],
        admin_email_allowlist=[email],
        admin_telegram_allowlist=["202"],
        mail_dir=str(tmp_path),
        allowed_hosts=["testserver"],
        allowed_origins=["http://testserver"],
    )
    documents = [
        {"document_type": "offer", "version": "mfa-test", "url": "https://example.test/offer"},
        {"document_type": "privacy", "version": "mfa-test", "url": "https://example.test/privacy"},
    ]
    Commerce(db).legal.configure(None, documents, "Синтетические документы MFA")
    app = create_app(config, redis_client=fakeredis.FakeRedis())
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        yield db, app, client, email, outsider, tmp_path, documents
    app.state.commerce.db.engine.dispose()
    db.engine.dispose()
    with base.engine.begin() as connection:
        assert schema.startswith("mfa_test_") and len(schema) == 41
        connection.execute(DropSchema(schema, cascade=True))
    base.engine.dispose()


def login(context, client=None, email=None):
    db, app, original, admin_email, _, path, documents = context
    if (email or admin_email) == admin_email:
        from sunrise_infrastructure.db import Account

        with db.transaction() as session:
            actor = session.scalar(select(Account).where(Account.email == admin_email))
            if actor is None:
                session.add(Account(email=admin_email, email_verified=True, telegram_id=int(app.state.settings.owner_id)))
    client = client or original
    request = client.post(
        "/api/v1/auth/email/request", json={"email": email or admin_email, "turnstile_token": "local-test"}
    )
    assert request.status_code == 200, request.text
    cid = request.json()["challenge_id"]
    code = json.loads((path / f"{cid}.json").read_text())["code"]
    response = client.post(
        "/api/v1/auth/email/verify",
        json={
            "challenge_id": cid,
            "code": code,
            "legal_accepted": True,
            "documents": documents,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def enroll(context):
    _, _, client, _, _, _, _ = context
    auth = login(context)
    headers = {"X-CSRF-Token": auth["csrf_token"]}
    result = client.post("/api/v1/admin/mfa/enroll", headers=headers)
    assert result.status_code == 200, result.text
    return auth, headers, result.json()


@pytest.mark.parametrize("mfa_context", ["Другой MFA магазин"], indirect=True)
def test_new_mfa_enrollment_issuer_uses_configured_brand(mfa_context):
    from urllib.parse import parse_qs, urlsplit

    _, _, pending = enroll(mfa_context)
    assert parse_qs(urlsplit(pending["provisioning_uri"]).query)["issuer"] == ["Другой MFA магазин"]


def test_admin_enrollment_secret_is_pending_encrypted_and_activated_only_by_correct_code(mfa_context):
    db, _, client, _, _, _, _ = mfa_context
    auth, headers, pending = enroll(mfa_context)
    assert pending["provisioning_uri"].startswith("otpauth://totp/")
    assert (
        pyotp.TOTP(pending["secret"]).provisioning_uri(
            name=auth["account"]["email"], issuer_name="Северный VPN"
        )
        == pending["provisioning_uri"]
    )
    assert client.get("/api/v1/admin/mfa/status").json()["enrolled"] is False
    assert client.get("/api/v1/admin/overview").status_code == 403
    code = pyotp.TOTP(pending["secret"]).now()
    bad = "000000" if code != "000000" else "111111"
    failed = client.post("/api/v1/admin/mfa/enroll/confirm", headers=headers, json={"code": bad})
    assert failed.status_code == 400
    assert client.get("/api/v1/admin/mfa/status").json()["enrolled"] is False
    good = client.post("/api/v1/admin/mfa/enroll/confirm", headers=headers, json={"code": code})
    assert good.status_code == 200
    assert good.json()["enrolled"] is True and good.json()["trusted"] is True
    assert client.get("/api/v1/admin/overview").status_code == 200
    assert client.post("/api/v1/admin/mfa/enroll", headers=headers).status_code == 409
    assert "secret" not in client.get("/api/v1/admin/mfa/status").text
    assert "secret" not in client.get("/api/v1/auth/me").text
    from sunrise_infrastructure.db import AdminAuthenticator

    with db.transaction() as s:
        record = s.get(AdminAuthenticator, int(auth["account"]["id"]))
        assert pending["secret"] not in record.secret_ciphertext
        assert record.pending_ciphertext is None


def test_admin_trust_expires_at_fixed_48_hours_or_changed_direct_ip_and_ignores_forwarded_headers(
    mfa_context,
):
    db, app, client, _, _, _, _ = mfa_context
    auth, headers, pending = enroll(mfa_context)
    code = pyotp.TOTP(pending["secret"]).now()
    assert (
        client.post("/api/v1/admin/mfa/enroll/confirm", headers=headers, json={"code": code}).status_code
        == 200
    )
    from sunrise_infrastructure.db import AdminAuthenticator

    with db.transaction() as s:
        record = s.get(AdminAuthenticator, int(auth["account"]["id"]))
        verified = record.trust_verified_at
    assert client.get("/api/v1/admin/overview").status_code == 200
    with db.transaction() as s:
        assert s.get(AdminAuthenticator, int(auth["account"]["id"])).trust_verified_at == verified
    with TestClient(app, client=("127.0.0.2", 50001)) as changed:
        changed.cookies.update(client.cookies)
        assert (
            changed.get("/api/v1/admin/overview", headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 403
        )
        assert changed.get("/api/v1/auth/me").json()["mfa_verified"] is False
    with db.transaction() as s:
        s.get(AdminAuthenticator, int(auth["account"]["id"])).trust_verified_at = now() - timedelta(hours=48)
    assert client.get("/api/v1/admin/overview").status_code == 403
    assert client.get("/api/v1/auth/me").json()["mfa_verified"] is False


def test_pending_enrollment_is_session_bound_and_expires_without_creating_active_secret(mfa_context):
    db, app, client, _, _, _, _ = mfa_context
    auth, headers, pending = enroll(mfa_context)
    from sunrise_infrastructure.db import AdminAuthenticator

    with db.transaction() as s:
        original = s.scalar(select(Session).where(Session.account_id == int(auth["account"]["id"])))
        second = Session(
            id="synthetic-other-session",
            account_id=original.account_id,
            csrf="other",
            expires_at=now() + timedelta(hours=1),
        )
        s.add(second)
    with TestClient(app, client=("127.0.0.1", 50001)) as other:
        from sunrise_infrastructure.security import digest

        with db.transaction() as s:
            s.get(Session, "synthetic-other-session").id = digest("second-token")
        other.cookies.set("sunrise_session", "second-token")
        assert (
            other.post(
                "/api/v1/admin/mfa/enroll/confirm",
                headers={"X-CSRF-Token": "other"},
                json={"code": pyotp.TOTP(pending["secret"]).now()},
            ).status_code
            == 400
        )
    with db.transaction() as s:
        s.get(AdminAuthenticator, int(auth["account"]["id"])).pending_expires_at = now() - timedelta(
            seconds=1
        )
    assert (
        client.post(
            "/api/v1/admin/mfa/enroll/confirm",
            headers=headers,
            json={"code": pyotp.TOTP(pending["secret"]).now()},
        ).status_code
        == 400
    )
    assert client.get("/api/v1/admin/mfa/status").json()["enrolled"] is False


def test_enrollment_requires_fresh_email_and_all_mfa_routes_hide_from_outsiders(mfa_context):
    db, _, client, _, outsider, _, _ = mfa_context
    auth = login(mfa_context, email=outsider)
    for route in ["status", "enroll", "enroll/confirm", "verify"]:
        result = (
            client.get("/api/v1/admin/mfa/" + route)
            if route == "status"
            else client.post(
                "/api/v1/admin/mfa/" + route,
                headers={"X-CSRF-Token": auth["csrf_token"]},
                json={"code": "123456"},
            )
        )
        assert result.status_code == 404
    auth = login(mfa_context)
    with db.transaction() as s:
        session = s.scalar(select(Session).where(Session.account_id == int(auth["account"]["id"])))
        session.confirmed_at = now() - timedelta(minutes=6)
    response = client.post("/api/v1/admin/mfa/enroll", headers={"X-CSRF-Token": auth["csrf_token"]})
    assert response.status_code == 403


def test_active_totp_verification_rejects_replay_and_does_not_disclose_the_secret(mfa_context):
    _, _, client, _, _, _, _ = mfa_context
    _, headers, pending = enroll(mfa_context)
    code = pyotp.TOTP(pending["secret"]).now()
    assert (
        client.post("/api/v1/admin/mfa/enroll/confirm", headers=headers, json={"code": code}).status_code
        == 200
    )
    result = client.post("/api/v1/admin/mfa/verify", headers=headers, json={"code": code})
    assert result.status_code == 400 and result.json()["error"]["code"] == "mfa_replay"


def test_fixed_admin_trust_survives_a_new_email_session_but_dangerous_changes_require_fresh_totp(mfa_context):
    db, app, client, _, _, _, _ = mfa_context
    auth, headers, pending = enroll(mfa_context)
    code = pyotp.TOTP(pending["secret"]).now()
    assert (
        client.post("/api/v1/admin/mfa/enroll/confirm", headers=headers, json={"code": code}).status_code
        == 200
    )
    from sunrise_infrastructure.security import digest

    aid = int(auth["account"]["id"])
    with db.transaction() as s:
        from sunrise_infrastructure.db import Account
        from sunrise_infrastructure.staff_permissions import StaffPermissions

        # Этот сценарий проверяет MFA владельца; обычный администратор
        # не может изменять режим обслуживания даже с новым TOTP.
        account = s.get(Account, aid)
        account.telegram_id = int(app.state.settings.owner_id)
        StaffPermissions(app.state.accounts).ensure_owner_in(s, account)
        old = s.scalar(select(Session).where(Session.account_id == aid))
        old.mfa_at = now() - timedelta(minutes=6)
        s.add(
            Session(
                id=digest("fresh-email-token"),
                account_id=aid,
                csrf="fresh-csrf",
                expires_at=now() + timedelta(hours=1),
            )
        )
        setting = s.get(Setting, "maintenance")
        before = setting.value if setting else None
    assert client.get("/api/v1/admin/overview").status_code == 200
    changed = client.patch(
        "/api/v1/admin/operations/maintenance",
        headers=headers,
        json={
            "enabled": True,
            "confirmed": True,
            "reason": "Синтетическая опасная операция",
        },
    )
    assert changed.status_code == 403
    assert changed.json()["error"]["code"] == "mfa_required"
    with db.transaction() as s:
        setting = s.get(Setting, "maintenance")
        assert (setting.value if setting else None) == before
    with TestClient(app, client=("127.0.0.1", 50002)) as second:
        second.cookies.set("sunrise_session", "fresh-email-token")
        assert second.get("/api/v1/auth/me").json()["mfa_verified"] is True
        assert second.get("/api/v1/admin/overview").status_code == 200
        assert (
            second.patch(
                "/api/v1/admin/operations/maintenance",
                headers={"X-CSRF-Token": "fresh-csrf"},
                json={
                    "enabled": True,
                    "confirmed": True,
                    "reason": "Синтетическая опасная операция",
                },
            ).status_code
            == 403
        )


def test_wrong_mfa_budget_blocks_on_tenth_attempt_without_activation(mfa_context):
    _, _, client, _, _, _, _ = mfa_context
    _, headers, pending = enroll(mfa_context)
    code = pyotp.TOTP(pending["secret"]).now()
    bad = "000000" if code != "000000" else "111111"
    for attempt in range(10):
        response = client.post("/api/v1/admin/mfa/enroll/confirm", headers=headers, json={"code": bad})
        assert response.status_code == (429 if attempt == 9 else 400)
    assert (
        client.post("/api/v1/admin/mfa/enroll/confirm", headers=headers, json={"code": code}).status_code
        == 429
    )
    assert client.get("/api/v1/admin/mfa/status").json()["enrolled"] is False


def test_fresh_telegram_or_legacy_session_is_not_fresh_email_proof_for_mfa_enrollment(mfa_context):
    db, app, _, _, _, _, _ = mfa_context
    auth = login(mfa_context)
    from sunrise_infrastructure.security import digest

    with db.transaction() as s:
        s.add(
            Session(
                id=digest("telegram-session"),
                account_id=int(auth["account"]["id"]),
                csrf="telegram-csrf",
                confirmed_at=now(),
                expires_at=now() + timedelta(hours=1),
            )
        )
    with TestClient(app, client=("127.0.0.1", 50002)) as telegram:
        telegram.cookies.set("sunrise_session", "telegram-session")
        response = telegram.post("/api/v1/admin/mfa/enroll", headers={"X-CSRF-Token": "telegram-csrf"})
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "fresh_login_required"


def test_active_totp_at_new_ip_grants_a_new_fixed_trust_and_rechecks_allowlist(mfa_context, monkeypatch):
    db, app, client, email, _, _, _ = mfa_context
    auth, headers, pending = enroll(mfa_context)
    code = pyotp.TOTP(pending["secret"]).now()
    assert (
        client.post("/api/v1/admin/mfa/enroll/confirm", headers=headers, json={"code": code}).status_code
        == 200
    )
    moment = now() + timedelta(seconds=31)
    monkeypatch.setattr("sunrise_infrastructure.admin_mfa.now", lambda: moment)
    with TestClient(app, client=("127.0.0.2", 50001)) as changed:
        changed.cookies.update(client.cookies)
        assert changed.get("/api/v1/admin/overview").status_code == 403
        verified = changed.post(
            "/api/v1/admin/mfa/verify",
            headers=headers,
            json={
                "code": pyotp.TOTP(pending["secret"]).at(moment),
            },
        )
        assert verified.status_code == 200
        assert verified.json()["trusted"] is True
        assert verified.json()["trust_expires_at"] == (moment + timedelta(hours=48)).isoformat()
        assert changed.get("/api/v1/admin/overview").status_code == 200
    assert client.get("/api/v1/admin/overview").status_code == 403
    app.state.settings.owner_id = None
    app.state.settings.admin_telegram_allowlist = []
    assert client.get("/api/v1/admin/mfa/status").status_code == 404


def test_concurrent_legal_update_serializes_login_and_rejects_stale_consent_without_session(mfa_context):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from sunrise_infrastructure.db import Account, Challenge
    from sunrise_infrastructure.legal import LEGAL_LOCK

    db, app, client, email, _, path, documents = mfa_context
    request = client.post(
        "/api/v1/auth/email/request", json={"email": email, "turnstile_token": "local-test"}
    )
    cid = request.json()["challenge_id"]
    code = json.loads((path / f"{cid}.json").read_text())["code"]
    held, waiting, release = Event(), Event(), Event()

    def hold_configure_lock(_connection, _cursor, statement, parameters, _context, _many):
        if "pg_advisory_xact_lock" in statement and LEGAL_LOCK in parameters.values():
            held.set()
            if not release.wait(5):
                raise RuntimeError("synthetic-configure-lock-timeout")

    def observe_login_lock(_connection, _cursor, statement, parameters, _context, _many):
        if "pg_advisory_xact_lock" in statement and LEGAL_LOCK in parameters.values():
            waiting.set()

    event.listen(db.engine, "after_cursor_execute", hold_configure_lock)
    event.listen(app.state.accounts.db.engine, "before_cursor_execute", observe_login_lock)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            update = pool.submit(
                Commerce(db).legal.configure,
                None,
                [d | {"version": "new-current"} for d in documents],
                "Проверка конкурентного обновления",
            )
            try:
                assert held.wait(3)
                login_attempt = pool.submit(
                    client.post,
                    "/api/v1/auth/email/verify",
                    json={
                        "challenge_id": cid,
                        "code": code,
                        "legal_accepted": True,
                        "documents": documents,
                    },
                )
                assert waiting.wait(3)
            finally:
                release.set()
            assert update.result(timeout=5)["configured"] is True
            response = login_attempt.result(timeout=5)
    finally:
        release.set()
        event.remove(db.engine, "after_cursor_execute", hold_configure_lock)
        event.remove(app.state.accounts.db.engine, "before_cursor_execute", observe_login_lock)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "legal_versions_stale"
    with db.transaction() as s:
        assert s.get(Challenge, cid).used is False
        assert s.scalar(select(Account).where(Account.email == email)) is None
        assert s.scalar(select(Session)) is None
