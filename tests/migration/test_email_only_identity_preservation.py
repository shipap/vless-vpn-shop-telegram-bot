"""Email-only legacy primary keys must never become Telegram delivery identities."""

from sunrise_infrastructure.legacy import legacy_account_values


def source_row(*, user_id, telegram_id):
    return {
        "user_id": user_id,
        "telegram_id": telegram_id,
        "email": "email-only@example.invalid",
        "hashed_password": "synthetic-password-hash",
        "balance": "12.50",
        "referral_balance": "3.25",
        "referrer_id": None,
        "is_web_user": 1,
    }


def test_email_only_random_63bit_primary_key_stays_internal():
    source = source_row(user_id=8_123_456_789_012_345_678, telegram_id=None)
    values, _, _ = legacy_account_values(source)
    assert values["id"] == source["user_id"]
    assert values["telegram_id"] is None
    assert str(values["credit_balance"]) == "12.500000"
    assert str(values["bonus_balance"]) == "3.250000"
    assert source["telegram_id"] is None


def test_linked_email_account_preserves_separate_explicit_telegram():
    source = source_row(user_id=8_123_456_789_012_345_678, telegram_id=123456789)
    values, _, _ = legacy_account_values(source)
    assert values["id"] == source["user_id"]
    assert values["telegram_id"] == 123456789


def test_small_primary_key_also_does_not_prove_telegram_identity():
    values, _, _ = legacy_account_values(source_row(user_id=123456789, telegram_id=None))
    assert values["telegram_id"] is None
