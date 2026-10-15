"""Серверные права владельца и администратора, независимо от скрытых кнопок."""

import re

from sqlalchemy import select
from sunrise_core.errors import DomainError, missing

from .db import Account, Alias, StaffRole

ADMIN_CAPABILITIES = frozenset(
    {
        "statistics_read",
        "users_read",
        "catalog_read",
        "payments_read",
        "referrals_read",
        "promos_write",
        "messages_write",
        "broadcasts_write",
        "campaigns_read",
        "campaigns_write",
        "device_unlinks_manage",
    }
)
OWNER_CAPABILITIES = ADMIN_CAPABILITIES | frozenset(
    {
        "catalog_write",
        "settings_write",
        "security_manage",
        "wallet_write",
        "subscriptions_manage",
        "identity_review",
        "backups_manage",
        "operations_manage",
        "staff_manage",
        "content_write",
        "refunds_manage",
        "recurring_manage",
    }
)


def telegram_admin_ids(settings):
    allowed = getattr(settings, "admin_telegram_allowlist", ())
    if not isinstance(allowed, (list, tuple, set, frozenset)):
        allowed = ()
    return frozenset(
        int(value)
        for value in (getattr(settings, "owner_id", None), *allowed)
        if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,18}", value) and int(value) < 2**63
    )


def telegram_admin_allowed(account, settings):
    identifier = getattr(account, "telegram_id", None)
    return type(identifier) is int and identifier in telegram_admin_ids(settings)


class StaffPermissions:
    def __init__(self, accounts):
        self.accounts, self.db, self.settings = accounts, accounts.db, accounts.settings

    def _telegram_allowed(self, account):
        return telegram_admin_allowed(account, self.settings)

    def role_in(self, session, account, *, bot=False):
        if not self._telegram_allowed(account):
            return None
        grant = session.get(StaffRole, account.id)
        if (
            grant
            and grant.role == "owner"
            or account.telegram_id is not None
            and str(account.telegram_id) == str(getattr(self.settings, "owner_id", None))
        ):
            return "owner"
        return "admin"

    def ensure_owner_in(self, session, account):
        if (
            self._telegram_allowed(account)
            and account.telegram_id is not None
            and str(account.telegram_id) == str(getattr(self.settings, "owner_id", None))
        ):
            grant = session.get(StaffRole, account.id)
            if grant is None:
                session.add(StaffRole(account_id=account.id, role="owner", granted_by=account.id))
            elif grant.role != "owner":
                grant.role = "owner"

    def capabilities_in(self, session, account, *, bot=False):
        role = self.role_in(session, account, bot=bot)
        return sorted(
            OWNER_CAPABILITIES if role == "owner" else ADMIN_CAPABILITIES if role == "admin" else []
        )

    def require_in(self, session, actor, capability, *, bot=False):
        if isinstance(actor, Account):
            identifier = actor.id
        else:
            try:
                identifier = int(actor)
            except (ValueError, TypeError):
                raise missing() from None
            alias = session.get(Alias, identifier)
            identifier = alias.canonical_id if alias else identifier
        account = session.scalar(
            select(Account)
            .where(Account.id == identifier)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        session.scalar(
            select(StaffRole)
            .where(StaffRole.account_id == identifier)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if not account or not self.role_in(session, account, bot=bot):
            raise missing()
        if capability not in self.capabilities_in(session, account, bot=bot):
            raise DomainError("staff_permission_denied", "Это действие доступно только владельцу.", 403)
        return account

    def require(self, actor, capability, *, bot=False):
        with self.db.transaction() as session:
            return self.require_in(session, actor, capability, bot=bot)

    def describe(self, account_id, *, bot=False):
        with self.db.transaction() as session:
            account = session.get(Account, int(account_id))
            if not account:
                raise missing()
            return {
                "staff_role": self.role_in(session, account, bot=bot),
                "capabilities": self.capabilities_in(session, account, bot=bot),
            }


def route_capability(path, method):
    """Unknown administrative mutations default to owner; no wildcard staff write grant."""
    suffix = path.removeprefix("/api/v1/admin/")
    if suffix.startswith("mfa/") or suffix in {"mfa", "access"}:
        return None
    read = method in {"GET", "HEAD", "OPTIONS"}
    first = suffix.split("/")[0]
    if first in {"statistics", "stats", "charts", "overview", "analytics"}:
        return "statistics_read"
    if first in {"campaigns", "utm"}:
        return "campaigns_read" if read else "campaigns_write"
    if first == "promos":
        return "promos_write"
    if first == "discounts" and read:
        return "promos_write"
    if first in {"broadcasts", "messages"}:
        return "broadcasts_write" if first == "broadcasts" else "messages_write"
    if first == "referrals":
        return "referrals_read"
    if first == "users":
        if read:
            return "users_read"
        if suffix.endswith("/device-moderation"):
            return "device_unlinks_manage"
        if suffix.endswith("/message"):
            return "messages_write"
        if "/bonus" in suffix or "/credit" in suffix:
            return "wallet_write"
        return "security_manage"
    if first in {"payments", "orders"}:
        return "payments_read" if read else "refunds_manage"
    if first == "catalog":
        return "catalog_read" if read else "catalog_write"
    if first == "subscriptions":
        return "users_read" if read else "subscriptions_manage"
    if first in {"identity-requests", "identity_requests"}:
        return "identity_review"
    if first in {"backup", "backups"}:
        return "backups_manage"
    if first in {"health", "operations", "jobs", "nodes"}:
        return "operations_manage"
    if first in {"content", "registry", "telegram-theme"}:
        return "content_write"
    if first in {"security", "blocks", "logins"}:
        return "security_manage"
    if first in {"refunds", "refund-reviews"}:
        return "refunds_manage"
    if first in {"recurring", "agreements"}:
        return "recurring_manage"
    return "settings_write"
