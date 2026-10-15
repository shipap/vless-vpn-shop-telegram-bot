"""Staff privileges depend on canonical Telegram IDs, never email or usernames."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sunrise_core.errors import DomainError
from sunrise_infrastructure.accounts import Accounts
from sunrise_infrastructure.admin_mfa import AdminMFA
from sunrise_infrastructure.db import now
from sunrise_infrastructure.moderation import DeviceModeration
from sunrise_infrastructure.staff_permissions import StaffPermissions


def context(telegram_id, *, owner="101", allowlist=("202",)):
    settings = SimpleNamespace(owner_id=owner, admin_telegram_allowlist=list(allowlist),
                               admin_email_allowlist=["admin@example.test"])
    account = SimpleNamespace(id=9007199254740993, telegram_id=telegram_id,
                              telegram_username="101", email="admin@example.test", email_verified=True)
    accounts = Accounts(None, None, settings)
    return accounts, account


@pytest.mark.parametrize("identifier,allowed", [(101, True), (202, True), (303, False),
                                              (None, False), (True, False), ("101", False),
                                              (0, False), (-101, False), (2**63, False)])
def test_admin_gate_uses_only_the_exact_positive_canonical_telegram_id(identifier, allowed):
    accounts, account = context(identifier)
    assert accounts.is_admin(account) is allowed
    assert accounts.is_bot_admin(account) is allowed
    account.email, account.email_verified = None, False
    assert accounts.is_admin(account) is allowed


def test_roles_keep_explicit_owner_grants_only_while_telegram_is_admitted():
    accounts, account = context(202)
    permissions = StaffPermissions(accounts)
    session = SimpleNamespace(get=lambda model, identifier: SimpleNamespace(role="owner"))
    assert permissions.role_in(session, account) == "owner"
    assert "wallet_write" in permissions.capabilities_in(session, account, bot=True)
    account.telegram_id = None
    assert permissions.role_in(session, account) is None
    assert permissions.capabilities_in(session, account) == []
    account.telegram_id = 303
    assert permissions.role_in(session, account) is None


def test_moderation_cannot_use_email_or_username_as_admin_authority():
    accounts, account = context(101)
    account.email, account.email_verified = None, False
    assert DeviceModeration.can_moderate(account, accounts.settings)
    account.telegram_id = 303
    account.email, account.email_verified = "admin@example.test", True
    assert not DeviceModeration.can_moderate(account, accounts.settings)


@pytest.mark.parametrize("method", ["telegram_oidc", "telegram_miniapp"])
def test_mfa_enrollment_accepts_recent_verified_telegram_session_from_same_ip(method):
    moment = now()
    row = SimpleNamespace(account_id=1, expires_at=moment + timedelta(days=7),
                          created_at=moment, confirmed_at=moment, email_confirmed_at=None,
                          auth_method=method, ip_address="127.0.0.1")
    session = SimpleNamespace(get=lambda *args: row)
    mfa = AdminMFA(SimpleNamespace(db=None, settings=None, guard=None))
    assert mfa._session(session, 1, "own-session", fresh=True, ip="127.0.0.1") is row


@pytest.mark.parametrize("change", ["unknown", "email_otp", "different_ip", "expired_proof", "future_proof"])
def test_mfa_enrollment_rejects_unproven_or_stale_telegram_session(change):
    moment = now()
    row = SimpleNamespace(account_id=1, expires_at=moment + timedelta(days=7),
                          created_at=moment, confirmed_at=moment, email_confirmed_at=None,
                          auth_method="telegram_oidc", ip_address="127.0.0.1")
    if change in {"unknown", "email_otp"}:
        row.auth_method = change
    elif change == "different_ip":
        row.ip_address = "127.0.0.2"
    else:
        row.created_at = moment + timedelta(minutes=1) if change == "future_proof" else moment - timedelta(minutes=6)
    session = SimpleNamespace(get=lambda *args: row)
    mfa = AdminMFA(SimpleNamespace(db=None, settings=None, guard=None))
    with pytest.raises(DomainError) as error:
        mfa._session(session, 1, "own-session", fresh=True, ip="127.0.0.1")
    assert error.value.code == "fresh_login_required"
