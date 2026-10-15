import json

import pytest
from sqlalchemy import select
from sunrise_infrastructure.db import Account, Session
from sunrise_infrastructure.security import digest

from tests.integration.test_telegram_auth_api import init_data
from tests.unit.test_telegram_miniapp_auth import TOKEN

pytest_plugins = ["tests.integration.test_v10_admin_mfa"]


@pytest.mark.parametrize("method", ["email", "miniapp"])
def test_auth_assigns_referrer_only_on_new_account_and_never_reassigns_existing(mfa_context, method):
    db, app, client, email, _, folder, documents = mfa_context
    with db.transaction() as tx:
        inviter = Account(email="inviter@example.test", email_verified=True)
        replacement = Account(email="replacement@example.test", email_verified=True)
        tx.add_all([inviter, replacement])
        tx.flush()
        inviter_id, replacement_id = inviter.id, replacement.id
    config = app.state.settings
    config.bot_test_token = TOKEN
    config.test_bot_ids = "123456789"
    config.test_telegram_recipients = "70000000000000001"
    config.telegram_miniapp_enabled = True

    def authenticate(referrer):
        if method == "email":
            app.state.accounts.guard.redis.delete("sunrise:resend:" + digest(email))
            requested = client.post(
                "/api/v1/auth/email/request", json={"email": email, "turnstile_token": "local-test"}
            )
            cid = requested.json()["challenge_id"]
            code = json.loads((folder / f"{cid}.json").read_text())["code"]
            return client.post(
                "/api/v1/auth/email/verify",
                json={
                    "challenge_id": cid,
                    "code": code,
                    "legal_accepted": True,
                    "documents": documents,
                    "referral_code": "ref_" + str(referrer),
                },
            )
        return client.post(
            "/api/v1/auth/telegram/miniapp",
            json={
                "init_data": init_data(),
                "legal_accepted": True,
                "documents": documents,
                "referral_code": "ref_" + str(referrer),
            },
        )

    first = authenticate(inviter_id)
    assert first.status_code == 200, first.text
    account_id = int(first.json()["account"]["id"])
    with db.transaction() as tx:
        assert tx.get(Account, account_id).referrer_id == inviter_id
    second = authenticate(replacement_id)
    assert second.status_code == 200, second.text
    assert int(second.json()["account"]["id"]) == account_id
    with db.transaction() as tx:
        assert tx.get(Account, account_id).referrer_id == inviter_id
        assert len(list(tx.scalars(select(Session)))) == 2


def test_referral_attribution_rolls_back_with_rejected_legal_consent(mfa_context):
    db, app, client, email, _, folder, _ = mfa_context
    with db.transaction() as tx:
        inviter = Account(email="inviter@example.test", email_verified=True)
        tx.add(inviter)
        tx.flush()
        inviter_id = inviter.id
    requested = client.post(
        "/api/v1/auth/email/request", json={"email": email, "turnstile_token": "local-test"}
    )
    cid = requested.json()["challenge_id"]
    code = json.loads((folder / f"{cid}.json").read_text())["code"]
    response = client.post(
        "/api/v1/auth/email/verify",
        json={"challenge_id": cid, "code": code, "referral_code": "ref_" + str(inviter_id)},
    )
    assert response.status_code == 409
    with db.transaction() as tx:
        assert tx.scalar(select(Account).where(Account.email == email)) is None
        assert tx.scalar(select(Session)) is None
