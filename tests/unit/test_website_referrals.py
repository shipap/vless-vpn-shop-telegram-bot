from types import SimpleNamespace

import pytest
from sqlalchemy import event, func, select
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import Account, Alias, Database, Ledger, Setting


@pytest.fixture
def referral_db():
    db = Database("sqlite+pysqlite:///:memory:")
    with db.engine.connect() as connection:
        connection.connection.driver_connection.create_function(
            "split_part", 3, lambda value, separator, index: value.split(separator)[index - 1]
        )
        connection.connection.driver_connection.create_function("pg_advisory_xact_lock", 1, lambda _value: 0)
    for table in (Account, Alias, Ledger, Setting):
        table.__table__.create(db.engine)
    with db.transaction() as session:
        session.add(Account(id=9007199254740993, email="inviter@example.test"))

    def sqlite_identity(_mapper, connection, target):
        if connection.engine is db.engine and target.id is None:
            target.id = 9007199254740994

    event.listen(Account, "before_insert", sqlite_identity)
    yield db
    event.remove(Account, "before_insert", sqlite_identity)
    db.engine.dispose()


def test_local_website_referral_links_never_target_a_configured_production_domain(referral_db):
    service = Commerce(
        referral_db,
        local_test=True,
        referral_bot_url="https://t.me/SyntheticTestBot",
        public_settings=SimpleNamespace(landing_url="https://production.example/", landing_test_domain=None),
    )
    result = service.referral_info("9007199254740993")
    assert result.get("website_referral_url") == "http://127.0.0.1:3100/?ref=ref_9007199254740993"
    assert result["referral_url"] == "https://t.me/SyntheticTestBot?start=ref_9007199254740993"


def test_configured_website_referral_landing_preserves_its_path_and_bigint_identity(referral_db):
    service = Commerce(
        referral_db,
        public_settings=SimpleNamespace(
            landing_url="https://landing.example.test/shop", landing_test_domain=None
        ),
    )
    assert service.referral_info("9007199254740993").get("website_referral_url") == (
        "https://landing.example.test/shop?ref=ref_9007199254740993"
    )


def test_scoped_server_website_referral_uses_its_configured_public_landing(referral_db):
    service = Commerce(
        referral_db,
        local_test=True,
        public_settings=SimpleNamespace(landing_url="https://isolated.example.test/"),
        runtime_policy=SimpleNamespace(
            production=False, scoped_payments=True, require_account_in=lambda _s, _a: None
        ),
    )
    assert service.referral_info("9007199254740993")["website_referral_url"] == (
        "https://isolated.example.test/?ref=ref_9007199254740993"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://user:password@landing.example.test",
        "javascript:alert(1)",
        "https://landing.example.test/?token=private",
        "//evil.example/",
    ],
)
def test_unsafe_or_secret_bearing_referral_landing_configuration_is_not_published(referral_db, url):
    service = Commerce(
        referral_db, public_settings=SimpleNamespace(landing_url=url, landing_test_domain=None)
    )
    assert service.referral_info("9007199254740993").get("website_referral_url") is None


@pytest.mark.parametrize("suffix", ["+9007199254740993", "09007199254740993", "9007199254740993 "])
def test_bot_website_referral_attribution_rejects_noncanonical_invitation_codes(referral_db, suffix):
    service = Commerce(referral_db, local_test=True)
    account = service.resolve_telegram(700001, "ref_" + suffix)
    with referral_db.transaction() as session:
        assert session.get(Account, int(account["id"])).referrer_id is None


def test_website_referral_attribution_does_not_reassign_existing_or_already_referred_accounts(referral_db):
    from sunrise_infrastructure.referral_attribution import assign_new_referrer

    with referral_db.transaction() as session:
        existing = session.get(Account, 9007199254740993)
        assign_new_referrer(session, existing, "ref_9007199254740993")
        assert existing.referrer_id is None
        account = Account(id=20, email="new@example.test", referrer_id=99)
        session.add(account)
        assign_new_referrer(session, account, "ref_9007199254740993")
        assert account.referrer_id == 99
        assert session.scalar(select(func.count()).select_from(Ledger)) == 0


def test_website_referral_attribution_resolves_legacy_alias_without_new_money_effects(referral_db):
    from sunrise_infrastructure.referral_attribution import assign_new_referrer

    with referral_db.transaction() as session:
        session.add(Alias(old_id=7, canonical_id=9007199254740993))
    with referral_db.transaction() as session:
        account = Account(id=20, email="new@example.test")
        session.add(account)
        assign_new_referrer(session, account, "ref_7")
        session.flush()
        assert account.referrer_id == 9007199254740993
        assert account.bonus_balance == 0 and account.credit_balance == 0
        assert session.scalar(select(func.count()).select_from(Ledger)) == 0


@pytest.mark.parametrize("code", ["ref_20", "ref_999999", "ref_9223372036854775808", "ref_0", None])
def test_website_referral_attribution_ignores_self_missing_and_invalid_inviters(referral_db, code):
    from sunrise_infrastructure.referral_attribution import assign_new_referrer

    with referral_db.transaction() as session:
        account = Account(id=20, email="new@example.test")
        session.add(account)
        assign_new_referrer(session, account, code)
        session.flush()
        assert account.referrer_id is None


def test_website_referral_known_negative_legacy_alias_is_allowed_only_in_private_bot_path(referral_db):
    from sunrise_infrastructure.referral_attribution import assign_new_referrer

    with referral_db.transaction() as session:
        session.add(Alias(old_id=-7, canonical_id=9007199254740993))
    with referral_db.transaction() as session:
        website = Account(id=20, email="website@example.test")
        bot = Account(id=21, email="bot@example.test")
        session.add_all([website, bot])
        assign_new_referrer(session, website, "ref_-7")
        assign_new_referrer(session, bot, "ref_-7", allow_legacy_alias=True)
        session.flush()
        assert website.referrer_id is None
        assert bot.referrer_id == 9007199254740993
        assert session.scalar(select(func.count()).select_from(Ledger)) == 0


@pytest.mark.parametrize("code", ["ref_-8", "ref_-07", "ref_+7", "ref_-7 ", "ref_-9223372036854775809"])
def test_website_referral_private_bot_path_rejects_missing_negative_alias_or_malformed_code(
    referral_db, code
):
    from sunrise_infrastructure.referral_attribution import assign_new_referrer

    with referral_db.transaction() as session:
        session.add(Account(id=-8, email="raw-negative@example.test"))
        session.add(Alias(old_id=-7, canonical_id=9007199254740993))
    with referral_db.transaction() as session:
        account = Account(id=20, email="new@example.test")
        session.add(account)
        assign_new_referrer(session, account, code, allow_legacy_alias=True)
        session.flush()
        assert account.referrer_id is None


def test_website_referral_private_legacy_alias_cannot_target_negative_canonical_account(referral_db):
    from sunrise_infrastructure.referral_attribution import assign_new_referrer

    with referral_db.transaction() as session:
        session.add(Account(id=-8, email="raw-negative@example.test"))
        session.add(Alias(old_id=-7, canonical_id=-8))
    with referral_db.transaction() as session:
        account = Account(id=20, email="new@example.test")
        session.add(account)
        assign_new_referrer(session, account, "ref_-7", allow_legacy_alias=True)
        session.flush()
        assert account.referrer_id is None


@pytest.mark.parametrize("input_type", ["CodeRequest", "TelegramConsent", "MiniAppRequest"])
def test_website_referral_public_registration_inputs_reject_negative_legacy_codes(input_type):
    from pydantic import ValidationError
    from sunrise_api import schemas

    fields = {"referral_code": "ref_-7"}
    if input_type == "CodeRequest":
        fields |= {"challenge_id": "synthetic", "code": "123456"}
    if input_type == "MiniAppRequest":
        fields |= {"init_data": "synthetic"}
    with pytest.raises(ValidationError):
        getattr(schemas, input_type)(**fields)
