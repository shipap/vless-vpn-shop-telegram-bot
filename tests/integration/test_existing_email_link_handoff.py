"""Existing email-login handoff proves both identities before the explicit bot merge."""

import json
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pyotp
import pytest
from sqlalchemy import select
from sunrise_core.errors import DomainError
from sunrise_infrastructure.admin_mfa import AdminMFA
from sunrise_infrastructure.commerce import bootstrap
from sunrise_infrastructure.db import (
    Account,
    AdminAuthenticator,
    Alias,
    CreditLedger,
    Ledger,
    MergeProof,
    Payment,
    Segment,
    Session,
    StaffRole,
    Subscription,
    now,
)
from sunrise_infrastructure.staff_permissions import StaffPermissions

from tests.integration.test_telegram_oidc import begin, callback, oidc_context  # noqa: F401
from tests.integration.test_v10_admin_mfa import mfa_context  # noqa: F401
from tests.legal_fixture import accept_test_legal


@pytest.fixture
def handoff(oidc_context):  # noqa: F811
    context, _ = oidc_context
    db, app, client, email, _, folder, _ = context
    bootstrap(db)
    assert callback(client, begin(oidc_context)).headers["location"] == "/"
    auth = client.get("/api/v1/auth/me").json()
    target_id = int(auth["account"]["id"])
    assert auth["account"]["email"] is None
    with db.transaction() as session:
        source = Account(
            email=email,
            email_verified=True,
            bonus_balance=Decimal("12.34"),
            credit_balance=Decimal("7"),
            credit_currency="RUB",
        )
        session.add(source)
        session.flush()
        source_id = source.id
        target = session.get(Account, target_id)
        target.bonus_balance, target.credit_balance, target.credit_currency = (
            Decimal("5"),
            Decimal("3"),
            "RUB",
        )
    return context, auth, target_id, source_id


def email_login(item):
    context, _, _, source_id = item
    _, _, client, email, _, folder, documents = context
    requested = client.post(
        "/api/v1/auth/email/request", json={"email": email, "turnstile_token": "local-test"}
    )
    assert requested.status_code == 200
    challenge = requested.json()["challenge_id"]
    code = json.loads((folder / (challenge + ".json")).read_text(encoding="utf-8"))["code"]
    verified = client.post(
        "/api/v1/auth/email/verify",
        json={
            "challenge_id": challenge,
            "code": code,
            "legal_accepted": True,
            "documents": documents,
        },
    )
    assert verified.status_code == 200
    auth = verified.json()
    assert auth["account"]["id"] == str(source_id) and auth["account"]["telegram_id"] is None
    return auth


def link_request(item, auth):
    context, original, _, _ = item
    client = context[2]
    response = client.post(
        "/api/v1/account/link/request",
        json={"telegram_id": original["account"]["telegram_id"]},
        headers={"X-CSRF-Token": auth["csrf_token"]},
    )
    assert response.status_code == 200
    offer = response.json()
    assert offer["requires_merge"] is True and offer["expires_in"] == 300
    return offer["proof"]


@pytest.mark.parametrize("owner_mode", ["admin", "configured", "granted", "granted_existing_admin"])
def test_occupied_email_uses_explicit_normal_login_then_correct_bot_merge_preserving_rights_and_only_email_session(
    handoff,
    owner_mode,
):
    context, tg_auth, target_id, source_id = handoff
    db, app, client, email, _, _, _ = context
    telegram_id = tg_auth["account"]["telegram_id"]
    app.state.settings.owner_id = telegram_id if owner_mode == "configured" else None
    app.state.settings.admin_telegram_allowlist = [telegram_id]
    with db.transaction() as session:
        grant = StaffRole(
            account_id=target_id,
            role="owner" if owner_mode.startswith("granted") else "admin",
            granted_by=target_id,
        )
        session.add(grant)
        if owner_mode == "granted_existing_admin":
            session.add(
                StaffRole(
                    account_id=source_id,
                    role="admin",
                    granted_by=source_id,
                    granted_at=now() - timedelta(days=1),
                )
            )
        session.flush()
        original_grant = grant.granted_at, grant.granted_by
    cookie = client.cookies.get("sunrise_session")
    conflict = client.post(
        "/api/v1/account/email/request",
        json={"email": email},
        headers={"X-CSRF-Token": tg_auth["csrf_token"]},
    )
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "email_identity_conflict"
    assert client.cookies.get("sunrise_session") == cookie
    unavailable = client.post(
        "/api/v1/account/link/email/request", json={}, headers={"X-CSRF-Token": tg_auth["csrf_token"]}
    )
    assert unavailable.status_code == 403 and unavailable.json()["error"]["code"] == "link_email_required"
    with db.transaction() as session:
        assert session.scalar(select(MergeProof.id)) is None
    for aid in (target_id, source_id):
        accept_test_legal(app.state.commerce, aid)
        quote = app.state.commerce.quote(aid, "standard", 30, "purchase")
        order = app.state.commerce.create_order(aid, quote["id"], str(uuid4()))
        assert app.state.commerce.test_pay(aid, order["id"])["status"] == "paid"
        with db.transaction() as session:
            session.add(Ledger(account_id=aid, operation="synthetic:" + str(aid), amount=Decimal("1")))
            session.add(
                CreditLedger(
                    account_id=aid,
                    original_account_id=aid,
                    operation="synthetic:" + str(aid),
                    amount=Decimal("2"),
                    currency="RUB",
                    provenance="synthetic",
                )
            )
    auth = email_login(handoff)
    with db.transaction() as session:
        kept = session.scalar(select(Session).where(Session.account_id == source_id))
        before_session = (kept.id, kept.csrf, kept.confirmed_at, kept.email_confirmed_at, kept.expires_at)
        rights = [
            (row.id, row.starts_at, row.ends_at, row.paid_value)
            for row in session.scalars(select(Segment).order_by(Segment.id))
        ]
        subscriptions = [
            (row.id, row.expires_at, row.devices_limit)
            for row in session.scalars(select(Subscription).order_by(Subscription.id))
        ]
        payments = [
            (row.id, row.provider_id, row.original_account_id, row.amount, row.currency, row.status)
            for row in session.scalars(select(Payment).order_by(Payment.id))
        ]
        ledger_ids = list(session.scalars(select(Ledger.id).order_by(Ledger.id)))
        credit_ids = list(session.scalars(select(CreditLedger.id).order_by(CreditLedger.id)))
    proof = link_request(handoff, auth)
    assert client.get("/api/v1/auth/me").json()["account"]["telegram_id"] is None
    with db.transaction() as session:
        assert session.get(Account, target_id) is not None
    # This is the same trusted service call made only after the target bot's explicit confirmation action.
    result = app.state.accounts.confirm_telegram_link(target_id, proof)
    assert result["id"] == str(source_id) and result["telegram_id"] == tg_auth["account"]["telegram_id"]
    assert client.get("/api/v1/auth/me").json()["account"]["id"] == str(source_id)
    with db.transaction() as session:
        canonical = session.get(Account, source_id)
        permissions = StaffPermissions(app.state.accounts)
        assert permissions.role_in(session, canonical) == ("admin" if owner_mode == "admin" else "owner")
        grant = session.get(StaffRole, source_id)
        assert session.get(StaffRole, target_id) is None
        if owner_mode.startswith("granted"):
            assert grant.role == "owner" and (grant.granted_at, grant.granted_by) == original_grant
        elif owner_mode == "admin":
            assert grant is None
        current = list(session.scalars(select(Session)))
        assert len(current) == 1
        kept = current[0]
        assert (
            kept.id,
            kept.csrf,
            kept.confirmed_at,
            kept.email_confirmed_at,
            kept.expires_at,
        ) == before_session
        assert kept.mfa_at is None
        assert (
            session.get(Account, target_id) is None
            and session.get(Alias, target_id).canonical_id == source_id
        )
        assert session.get(Account, source_id).bonus_balance == Decimal("17.34")
        assert session.get(Account, source_id).credit_balance == Decimal("10")
        assert [
            (row.id, row.starts_at, row.ends_at, row.paid_value)
            for row in session.scalars(select(Segment).order_by(Segment.id))
        ] == rights
        assert [
            (row.id, row.expires_at, row.devices_limit)
            for row in session.scalars(select(Subscription).order_by(Subscription.id))
        ] == subscriptions
        assert [
            (row.id, row.provider_id, row.original_account_id, row.amount, row.currency, row.status)
            for row in session.scalars(select(Payment).order_by(Payment.id))
        ] == payments
        assert list(session.scalars(select(Ledger.id).order_by(Ledger.id))) == ledger_ids
        assert list(session.scalars(select(CreditLedger.id).order_by(CreditLedger.id))) == credit_ids
        for model in (Subscription, Payment, Ledger, CreditLedger):
            assert all(row.account_id == source_id for row in session.scalars(select(model)))
    with pytest.raises(DomainError) as error:
        app.state.accounts.confirm_telegram_link(target_id, proof)
    assert error.value.code == "link_invalid"


@pytest.mark.parametrize("denial", ["unallowlisted_owner", "revoked_allowlist", "dormant_source_owner"])
def test_owner_merge_requires_pretransfer_owner_authority_and_rolls_back_entire_identity(handoff, denial):
    context, original, target_id, source_id = handoff
    db, app, client, _, _, _, _ = context
    app.state.settings.owner_id = None
    telegram_id = original["account"]["telegram_id"]
    app.state.settings.admin_telegram_allowlist = [] if denial == "unallowlisted_owner" else [telegram_id]
    grant_id = source_id if denial == "dormant_source_owner" else target_id
    with db.transaction() as session:
        session.add(StaffRole(account_id=grant_id, role="owner", granted_by=grant_id))
    auth = email_login(handoff)
    proof = link_request(handoff, auth)
    if denial == "revoked_allowlist":
        app.state.settings.admin_telegram_allowlist = []
    with db.transaction() as session:
        before_accounts = [
            (row.id, row.telegram_id, row.email, row.bonus_balance, row.credit_balance)
            for row in session.scalars(select(Account).order_by(Account.id))
        ]
        before_sessions = [
            (row.id, row.account_id, row.csrf, row.expires_at)
            for row in session.scalars(select(Session).order_by(Session.id))
        ]
        grant = session.get(StaffRole, grant_id)
        before_grant = grant.role, grant.granted_at, grant.granted_by
    with pytest.raises(DomainError) as failure:
        app.state.accounts.confirm_telegram_link(target_id, proof)
    assert failure.value.code == "owner_merge_review_required"
    with db.transaction() as session:
        assert [
            (row.id, row.telegram_id, row.email, row.bonus_balance, row.credit_balance)
            for row in session.scalars(select(Account).order_by(Account.id))
        ] == before_accounts
        assert [
            (row.id, row.account_id, row.csrf, row.expires_at)
            for row in session.scalars(select(Session).order_by(Session.id))
        ] == before_sessions
        grant = session.get(StaffRole, grant_id)
        assert (grant.role, grant.granted_at, grant.granted_by) == before_grant
        assert session.get(Alias, target_id) is None
        assert session.get(MergeProof, proof.split(".")[0]).used is False


@pytest.mark.parametrize("denial", ["wrong_telegram", "expired", "revoked_session"])
def test_existing_handoff_rejects_wrong_confirmation_expired_proof_and_revoked_proof_issuer(handoff, denial):
    context, _, target_id, source_id = handoff
    db, app, client, _, _, _, _ = context
    auth = email_login(handoff)
    if denial == "revoked_session":
        assert (
            client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": auth["csrf_token"]}).status_code
            == 200
        )
        response = client.post(
            "/api/v1/account/link/request",
            json={"telegram_id": "70000000000000001"},
            headers={"X-CSRF-Token": auth["csrf_token"]},
        )
        assert response.status_code == 401
    else:
        proof = link_request(handoff, auth)
        if denial == "expired":
            with db.transaction() as session:
                source_session = session.scalar(select(Session).where(Session.account_id == source_id))
                session.get(MergeProof, proof.split(".")[0]).expires_at = (
                    source_session.confirmed_at - timedelta(seconds=1)
                )
        with pytest.raises(DomainError) as error:
            app.state.accounts.confirm_telegram_link(
                source_id if denial == "wrong_telegram" else target_id, proof
            )
        assert error.value.code == "link_invalid"
    with db.transaction() as session:
        assert session.get(Account, source_id).telegram_id is None
        assert session.get(Account, target_id).email is None
        assert session.get(Alias, target_id) is None


def test_already_issued_portable_proof_can_merge_after_email_logout_but_preserves_no_revoked_session(handoff):
    context, _, target_id, source_id = handoff
    db, app, client, _, _, _, _ = context
    auth = email_login(handoff)
    proof = link_request(handoff, auth)
    assert client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": auth["csrf_token"]}).status_code == 200
    assert app.state.accounts.confirm_telegram_link(target_id, proof)["id"] == str(source_id)
    with db.transaction() as session:
        assert list(session.scalars(select(Session))) == []
        assert session.get(Alias, target_id).canonical_id == source_id
    assert client.get("/api/v1/auth/me").status_code == 401


def target_mfa(handoff, *, pending=False, owner=True):
    context, tg_auth, target_id, _ = handoff
    db, app, client, _, _, _, _ = context
    app.state.settings.owner_id = None
    app.state.settings.admin_telegram_allowlist = [tg_auth["account"]["telegram_id"]]
    if owner:
        with db.transaction() as tx:
            tx.add(StaffRole(account_id=target_id, role="owner", granted_by=target_id))
    headers = {"X-CSRF-Token": tg_auth["csrf_token"]}
    enrollment = client.post("/api/v1/admin/mfa/enroll", headers=headers)
    assert enrollment.status_code == 200
    secret = enrollment.json()["secret"]
    if not pending:
        confirmed = client.post(
            "/api/v1/admin/mfa/enroll/confirm", headers=headers, json={"code": pyotp.TOTP(secret).now()}
        )
        assert confirmed.status_code == 200
    return secret


@pytest.mark.parametrize("source_pending", [False, True])
def test_qualified_owner_merge_preserves_target_totp_with_new_binding_and_replay_guard(
    handoff, monkeypatch, source_pending
):
    context, _, target_id, source_id = handoff
    db, app, client, _, _, _, _ = context
    secret = target_mfa(handoff)
    mfa = AdminMFA(app.state.accounts)
    for aid in (source_id, target_id):
        accept_test_legal(app.state.commerce, aid)
        quote = app.state.commerce.quote(aid, "standard", 30, "purchase")
        order = app.state.commerce.create_order(aid, quote["id"], str(uuid4()))
        assert app.state.commerce.test_pay(aid, order["id"])["status"] == "paid"
    with db.transaction() as tx:
        if source_pending:
            tx.add(
                AdminAuthenticator(
                    account_id=source_id,
                    pending_ciphertext=mfa._encrypt(source_id, pyotp.random_base32()),
                    pending_session_id="synthetic-old-enrollment",
                    pending_expires_at=now() + timedelta(minutes=10),
                )
            )
        original = tx.get(AdminAuthenticator, target_id)
        ciphertext, activated, step = (
            original.secret_ciphertext,
            original.activated_at,
            original.last_totp_step,
        )
        rights = [
            (row.id, row.expires_at, row.devices_limit)
            for row in tx.scalars(select(Subscription).order_by(Subscription.id))
        ]
        payments = [
            (row.id, row.original_account_id, row.amount, row.currency, row.status)
            for row in tx.scalars(select(Payment).order_by(Payment.id))
        ]
    auth = email_login(handoff)
    proof = link_request(handoff, auth)
    assert app.state.accounts.confirm_telegram_link(target_id, proof)["id"] == str(source_id)
    status = client.get("/api/v1/admin/mfa/status")
    assert status.status_code == 200
    assert (
        status.json()["enrolled"]
        and not status.json()["enrollment_required"]
        and not status.json()["trusted"]
    )
    assert client.get("/api/v1/admin/overview").status_code == 403
    with db.transaction() as tx:
        assert [
            (row.id, row.expires_at, row.devices_limit)
            for row in tx.scalars(select(Subscription).order_by(Subscription.id))
        ] == rights
        assert [
            (row.id, row.original_account_id, row.amount, row.currency, row.status)
            for row in tx.scalars(select(Payment).order_by(Payment.id))
        ] == payments
        assert all(row.account_id == source_id for row in tx.scalars(select(Subscription)))
        assert all(row.account_id == source_id for row in tx.scalars(select(Payment)))
        account = tx.get(Account, source_id)
        assert (account.bonus_balance, account.credit_balance) == (Decimal("17.34"), Decimal("10"))
        canonical = tx.get(AdminAuthenticator, source_id)
        assert canonical.secret_ciphertext != ciphertext
        assert mfa._decrypt(source_id, canonical.secret_ciphertext) == secret
        assert (canonical.activated_at, canonical.last_totp_step) == (activated, step)
        assert canonical.trusted_ip_digest is canonical.trust_verified_at is None
        assert (
            canonical.pending_ciphertext
            is canonical.pending_session_id
            is canonical.pending_expires_at
            is None
        )
        assert tx.get(AdminAuthenticator, target_id) is None
        assert tx.scalar(select(Session).where(Session.account_id == source_id)).mfa_at is None
        with pytest.raises(DomainError) as mismatch:
            mfa._decrypt(target_id, canonical.secret_ciphertext)
        assert mismatch.value.code == "mfa_unavailable"
    headers = {"X-CSRF-Token": auth["csrf_token"]}
    replay = client.post(
        "/api/v1/admin/mfa/verify", headers=headers, json={"code": pyotp.TOTP(secret).at(activated)}
    )
    assert replay.status_code == 400 and replay.json()["error"]["code"] == "mfa_replay"
    next_moment = activated + timedelta(seconds=30)
    monkeypatch.setattr("sunrise_infrastructure.admin_mfa.now", lambda: next_moment)
    verified = client.post(
        "/api/v1/admin/mfa/verify", headers=headers, json={"code": pyotp.TOTP(secret).at(next_moment)}
    )
    assert verified.status_code == 200 and verified.json()["trusted"] is True


@pytest.mark.parametrize(
    "conflict",
    [
        "both_active",
        "configured_source",
        "corrupt_target",
        "unqualified_target",
        "empty_target",
        "empty_source",
    ],
)
def test_target_totp_merge_conflicts_roll_back_identity_roles_proof_sessions_and_money(handoff, conflict):
    context, _, target_id, source_id = handoff
    db, app, client, email, _, _, _ = context
    target_mfa(handoff, owner=conflict != "unqualified_target")
    mfa = AdminMFA(app.state.accounts)
    with db.transaction() as tx:
        tx.add(Ledger(account_id=target_id, operation="synthetic-mfa-merge", amount=Decimal("1")))
        if conflict in {"both_active", "empty_source"}:
            tx.add(
                AdminAuthenticator(
                    account_id=source_id,
                    secret_ciphertext=""
                    if conflict == "empty_source"
                    else mfa._encrypt(source_id, pyotp.random_base32()),
                    activated_at=now(),
                    last_totp_step=1,
                )
            )
        elif conflict == "corrupt_target":
            tx.get(AdminAuthenticator, target_id).secret_ciphertext = "synthetic-invalid-ciphertext"  # noqa: S105 -- некорректный синтетический шифр
        elif conflict == "empty_target":
            tx.get(AdminAuthenticator, target_id).secret_ciphertext = ""
    if conflict == "configured_source":
        app.state.settings.admin_totp_secrets = {email: pyotp.random_base32()}
    auth = email_login(handoff)
    proof = link_request(handoff, auth)
    models = (Account, StaffRole, AdminAuthenticator, Session, MergeProof, Ledger, Payment, Subscription)

    def snapshot():
        with db.transaction() as tx:
            return [
                [
                    tuple(getattr(row, col.name) for col in model.__table__.columns)
                    for row in tx.scalars(select(model).order_by(*model.__table__.primary_key.columns))
                ]
                for model in models
            ]

    before = snapshot()
    with pytest.raises(DomainError) as failure:
        app.state.accounts.confirm_telegram_link(target_id, proof)
    assert failure.value.code == (
        "mfa_unavailable" if conflict in {"corrupt_target", "empty_target"} else "mfa_merge_review_required"
    )
    assert failure.value.status == (503 if conflict in {"corrupt_target", "empty_target"} else 409)
    assert snapshot() == before
    with db.transaction() as tx:
        assert tx.get(Alias, target_id) is None


def test_target_pending_totp_is_not_promoted_during_owner_merge(handoff):
    context, _, target_id, source_id = handoff
    db, app, client, _, _, _, _ = context
    target_mfa(handoff, pending=True)
    auth = email_login(handoff)
    proof = link_request(handoff, auth)
    app.state.accounts.confirm_telegram_link(target_id, proof)
    status = client.get("/api/v1/admin/mfa/status")
    assert status.status_code == 200 and status.json()["enrollment_required"] is True
    with db.transaction() as tx:
        assert tx.get(AdminAuthenticator, target_id) is None
        record = tx.get(AdminAuthenticator, source_id)
        assert record is None or record.secret_ciphertext is None and record.pending_ciphertext is None
