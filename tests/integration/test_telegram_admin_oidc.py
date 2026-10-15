"""Signed synthetic OIDC identity can enroll MFA without an email admin grant."""

from urllib.parse import unquote, urlsplit

from sqlalchemy import select
from sunrise_infrastructure.db import Session, now

from tests.integration.test_telegram_oidc import begin, callback

pytest_plugins = ["tests.integration.test_telegram_oidc"]


def test_signed_telegram_only_admin_enrolls_mfa_and_revoked_id_loses_access(oidc_context):
    (db, app, client, _, _, _, _), _ = oidc_context
    app.state.settings.owner_id = None
    app.state.settings.admin_telegram_allowlist = ["70000000000000001"]
    app.state.settings.admin_email_allowlist = []
    assert callback(client, begin(oidc_context)).status_code == 303
    me = client.get("/api/v1/auth/me").json()
    assert me["account"]["email"] is None
    assert me["is_admin"] is True and me["staff_role"] == "admin"
    assert client.get("/api/v1/admin/overview").status_code == 403  # MFA still required.
    enrolled = client.post("/api/v1/admin/mfa/enroll", headers={"X-CSRF-Token": me["csrf_token"]})
    assert enrolled.status_code == 200
    assert unquote(urlsplit(enrolled.json()["provisioning_uri"]).path).endswith(":70000000000000001")
    with db.transaction() as session:
        login = session.scalar(select(Session))
        assert login.auth_method == "telegram_oidc" and login.email_confirmed_at is None
        assert login.created_at <= now()
    app.state.settings.admin_telegram_allowlist = []
    assert client.get("/api/v1/admin/access").status_code == 404
    assert client.get("/api/v1/admin/mfa/status").status_code == 404
