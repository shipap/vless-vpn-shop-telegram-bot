"""Bot entry uses canonical account Telegram identity, like the shared server gate."""

from types import SimpleNamespace

import pytest
from sunrise_bot.adapter import BotApplication


@pytest.mark.parametrize(
    "identifier,allowed",
    [
        ("101", True),
        ("202", True),
        ("303", False),
        (None, False),
        ("0101", False),
        ("+101", False),
        (" 101", False),
        ("１０１", False),
        (True, False),
        ("9223372036854775808", False),
    ],
)
def test_admin_entry_requires_current_exact_telegram_identity_even_without_email(identifier, allowed):
    app = SimpleNamespace(
        accounts=SimpleNamespace(
            settings=SimpleNamespace(
                owner_id="101",
                admin_telegram_allowlist=["202"],
                admin_email_allowlist=["owner@example.test"],
            )
        )
    )
    account = {
        "id": "101",
        "telegram_id": identifier,
        "email": "owner@example.test" if not allowed else None,
        "email_verified": not allowed,
        "telegram_username": "101",
    }
    assert BotApplication.admin_allowed(app, account) is allowed


def test_telegram_admin_entry_rechecks_allowlist_revocation_without_email_fallback():
    settings = SimpleNamespace(
        owner_id=None, admin_telegram_allowlist=["202"], admin_email_allowlist=["owner@example.test"]
    )
    app = SimpleNamespace(accounts=SimpleNamespace(settings=settings))
    account = {"id": "999", "telegram_id": "202", "email": "owner@example.test", "email_verified": True}
    assert BotApplication.admin_allowed(app, account)
    settings.admin_telegram_allowlist = []
    assert not BotApplication.admin_allowed(app, account)
