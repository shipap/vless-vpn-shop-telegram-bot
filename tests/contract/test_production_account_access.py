from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sunrise_core.errors import DomainError
from sunrise_infrastructure.accounts import Accounts
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import Account, Alias, Session, now
from sunrise_infrastructure.security import digest


@pytest.fixture
def access_context():
    policy = SimpleNamespace(production=True, require_current=lambda *args: None, require=lambda *args: None)
    account = Account(
        id=9007199254740993,
        email="normal@synthetic.test",
        email_verified=True,
        telegram_id=123456,
        legacy_consent={
            "production_access": {
                "version": 1,
                "account_id": "9007199254740993",
                "state": "disabled",
                "source": "legacy_user_is_active",
                "source_account_id": "9007199254740993",
            }
        },
    )
    login = Session(
        id=digest("synthetic-session-token"), account_id=account.id, expires_at=now() + timedelta(days=1)
    )
    alias = Alias(old_id=9007199254740995, canonical_id=account.id)

    def scalar(query, *args):
        if getattr(query, "column_descriptions", [{}])[0].get("entity") is Account:
            return account
        return None

    tx = SimpleNamespace(
        scalar=scalar,
        get=lambda model, key, **_: (
            account
            if model is Account and key == account.id
            else login
            if model is Session and key == login.id
            else alias
            if model is Alias and key == alias.old_id
            else None
        ),
    )

    @contextmanager
    def transaction():
        yield tx

    db = SimpleNamespace(transaction=transaction)
    commerce = Commerce(db, local_test=True)
    commerce.runtime_policy = policy
    accounts = Accounts(db, None, SimpleNamespace(runtime_mode="production", _runtime_policy=policy))
    return account, alias, tx, commerce, accounts


def test_disabled_imported_account_cannot_use_existing_session(access_context):
    _, _, _, _, accounts = access_context
    with pytest.raises(DomainError) as failure:
        accounts.session("synthetic-session-token")
    assert failure.value.status == 404


def test_disabled_account_customer_lookup_denied_but_preserved_financial_lookup_available(access_context):
    account, _, tx, commerce, _ = access_context
    with pytest.raises(DomainError) as failure:
        commerce._account(tx, str(account.id))
    assert failure.value.status == 404
    assert commerce._account(tx, str(account.id), customer=False) is account


def test_proven_alias_resolves_to_active_canonical_without_transferring_source_disabled_flag(access_context):
    account, alias, tx, commerce, _ = access_context
    account.legacy_consent["production_access"]["state"] = "active"
    account.legacy_consent["merged:" + str(alias.old_id)] = {"production_access": {"state": "disabled"}}
    assert commerce._account(tx, str(alias.old_id)).id == account.id


@pytest.mark.parametrize("block", [None, {"version": True}, {"version": 1, "state": "active"}])
def test_missing_or_malformed_imported_access_proof_does_not_reenable_account(access_context, block):
    account, _, tx, commerce, _ = access_context
    account.legacy_consent = {"legacy_sources": {"synthetic": {}}}
    if block is not None:
        account.legacy_consent["production_access"] = block
    with pytest.raises(DomainError) as failure:
        commerce._account(tx, str(account.id))
    assert failure.value.status == 404


@pytest.mark.parametrize("channel", ["site", "bot"])
def test_authorized_staff_can_read_disabled_customer_history_without_reenabling_customer(
    access_context, monkeypatch, channel
):
    from sunrise_infrastructure.admin import Admin
    from sunrise_infrastructure.bot_admin import BotAdmin
    from sunrise_infrastructure.moderation import DeviceModeration

    account, _, tx, commerce, accounts = access_context
    from decimal import Decimal

    account.credit_balance = account.credit_reserved = Decimal(0)
    account.bonus_balance = account.bonus_reserved = Decimal(0)
    tx.scalars = lambda *args: []
    original_scalar = tx.scalar
    tx.scalar = lambda query, *args: (
        original_scalar(query, *args)
        if getattr(query, "column_descriptions", [{}])[0].get("entity") is Account
        else 0
    )
    monkeypatch.setattr(DeviceModeration, "summary", lambda *args: {})
    monkeypatch.setattr(DeviceModeration, "can_moderate", lambda *args: False)
    admin = Admin(commerce, None, accounts=accounts)
    if channel == "site":
        result = admin.user_detail(str(account.id), actor_id="9007199254740997")
    else:
        bot = BotAdmin(admin, SimpleNamespace(test_telegram_recipients="", owner_id=None))
        authorized = []
        bot.authorize_admin = lambda actor: authorized.append(actor)
        result = bot.bot_user_detail("9007199254740997", str(account.id))
        assert authorized == ["9007199254740997"]
        bot.authorize_admin = lambda actor: (_ for _ in ()).throw(
            DomainError("not_found", "Не найдено.", 404)
        )
        with pytest.raises(DomainError):
            bot.bot_user_detail("foreign", str(account.id))
    assert result["account"]["id"] == str(account.id)
    assert account.legacy_consent["production_access"]["state"] == "disabled"
    with pytest.raises(DomainError):
        commerce._account(tx, str(account.id))


def test_new_operation_gate_is_fresh_after_advisory_wait(access_context):
    _, _, tx, commerce, _ = access_context
    permitted = True

    def require_current(*args):
        if not permitted:
            raise DomainError("production_stage_denied", "Операция недоступна.", 503)

    def execute(*args):
        nonlocal permitted
        permitted = False

    commerce.runtime_policy.require_current = require_current
    tx.execute = execute
    with pytest.raises(DomainError) as failure:
        commerce._assert_new_operations(tx)
    assert failure.value.code == "production_stage_denied"
