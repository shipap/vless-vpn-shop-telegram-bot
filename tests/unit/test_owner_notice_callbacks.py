import pytest
from sunrise_core.notice_callbacks import owner_notice_callback, parse_owner_notice_callback


@pytest.mark.parametrize("operation", ["p", "w", "b"])
def test_owner_notice_callback_survives_restart_and_authenticates_event(operation):
    identifier = "12345678-1234-1234-1234-123456789abc"
    value = owner_notice_callback(operation, identifier, "s" * 48)
    assert len(value.encode()) <= 64
    assert parse_owner_notice_callback(value, "s" * 48) == (operation, identifier)
    assert parse_owner_notice_callback(value, "x" * 48) is None
    assert parse_owner_notice_callback(value.replace(":1234", ":9234"), "s" * 48) is None
    assert parse_owner_notice_callback(value[:-1] + ("a" if value[-1] != "a" else "b"), "s" * 48) is None


def test_owner_notice_callback_cannot_embed_arbitrary_actions():
    with pytest.raises(ValueError):
        owner_notice_callback("admin", "12345678-1234-1234-1234-123456789abc", "s" * 48)
    assert parse_owner_notice_callback("hw1:admin:1234:abcd", "s" * 48) is None
