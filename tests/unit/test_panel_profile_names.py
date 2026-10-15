from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sunrise_infrastructure.settings import Settings


def helper():
    from sunrise_infrastructure import panel_contacts

    return panel_contacts.panel_username


def test_prefix_setting_is_typed_and_rejects_bad_or_oversized_input():
    assert "panel_username_prefix" in Settings.model_fields
    assert Settings.model_fields["panel_username_prefix"].default == ""
    for value in ("bad.host", "x" * 13, "префикс", " sr", True):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, panel_username_prefix=value)


@pytest.mark.parametrize(
    "prefix,tg,username,email,expected",
    [
        ("sr", 12345, "Alice", "verified@example.test", "sr_Alice_12345_1"),
        ("", 12345, "Alice", None, "Alice_12345_1"),
        ("sr", 12345, None, "verified@example.test", "sr_12345_1"),
        ("sr", None, None, "a.b@example.test", "sr_a_b_example_test_1"),
        ("", None, None, "юзер@почта.рф", "email_1"),
    ],
)
def test_name_priority_and_sanitizing(prefix, tg, username, email, expected):
    actor = SimpleNamespace(telegram_id=tg, telegram_username=username, email=email, email_verified=True)
    assert helper()(prefix, actor, 1) == expected


def test_long_name_preserves_complete_real_id_and_sequence():
    actor = SimpleNamespace(telegram_id=4503599627370495, telegram_username="A" * 32)
    assert helper()("x" * 12, actor, 9999) == "x" * 12 + "_A_4503599627370495_9999"
    assert len(helper()("x" * 12, actor, 9999)) == 36


@pytest.mark.parametrize("tg,sequence", [(True, 1), (2**52, 1), (0, 1), (12, True), (12, 0), (12, 10000)])
def test_invalid_identity_and_sequence_deny(tg, sequence):
    from sunrise_infrastructure.panel import PanelReview

    with pytest.raises(PanelReview):
        helper()("", SimpleNamespace(telegram_id=tg, telegram_username="alice"), sequence)


def test_unverified_email_never_becomes_creation_contact_or_identity():
    from sunrise_infrastructure import panel_contacts
    from sunrise_infrastructure.panel import PanelReview

    actor = SimpleNamespace(
        id=9007199254740993, telegram_id=None, email="a@example.test", email_verified=False
    )
    assert panel_contacts.profile_contacts(actor) == {"telegramId": None}
    with pytest.raises(PanelReview):
        helper()("", actor, 1)
