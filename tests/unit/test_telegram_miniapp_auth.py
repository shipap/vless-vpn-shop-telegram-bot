import hashlib
import hmac
import json
from urllib.parse import urlencode

import pytest
from sunrise_core.errors import DomainError

TOKEN = "123456789:synthetic-test-token-without-live-access"  # noqa: S105 -- synthetic fixture, no live access
NOW = 1800000000


def signed_init(fields=None, *, token=TOKEN):
    fields = fields or {
        "auth_date": str(NOW),
        "user": json.dumps({"id": 70000000000000001, "username": "example_user"}),
    }
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    check = "\n".join(key + "=" + fields[key] for key in sorted(fields))
    signature = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields | {"hash": signature})


def test_signed_miniapp_identity_is_lossless_and_replay_key_is_independent_of_query_order():
    from sunrise_infrastructure.telegram_auth import validate_miniapp

    original = signed_init()
    first = validate_miniapp(original, TOKEN, NOW)
    second = validate_miniapp("&".join(reversed(original.split("&"))), TOKEN, NOW)
    assert first.telegram_id == 70000000000000001
    assert first.username == "example_user"
    assert first.replay_key == second.replay_key


@pytest.mark.parametrize(
    "mode", ["bad_hash", "duplicate", "expired", "future", "user_duplicate", "bad_escape"]
)
def test_invalid_miniapp_signature_structure_or_date_never_proves_an_identity(mode):
    from sunrise_infrastructure.telegram_auth import validate_miniapp

    fields = {"auth_date": str(NOW), "user": '{"id":70000000000000001}'}
    if mode == "expired":
        fields["auth_date"] = str(NOW - 301)
    if mode == "future":
        fields["auth_date"] = str(NOW + 31)
    if mode == "user_duplicate":
        fields["user"] = '{"id":70000000000000001,"id":70000000000000002}'
    value = signed_init(fields)
    if mode == "bad_hash":
        value = value[:-1] + ("0" if value[-1] != "0" else "1")
    if mode == "duplicate":
        value += "&auth_date=" + str(NOW)
    if mode == "bad_escape":
        value += "&unexpected=%QQ"
    with pytest.raises(DomainError) as exc:
        validate_miniapp(value, TOKEN, NOW)
    assert exc.value.code == "telegram_auth_invalid"


@pytest.mark.parametrize("identity", [True, -1, 0, "123", 1.5, 9223372036854775808])
def test_signed_telegram_id_must_be_a_positive_integer_in_bigint_range(identity):
    from sunrise_infrastructure.telegram_auth import validate_miniapp

    value = signed_init({"auth_date": str(NOW), "user": json.dumps({"id": identity})})
    with pytest.raises(DomainError) as exc:
        validate_miniapp(value, TOKEN, NOW)
    assert exc.value.code == "telegram_auth_invalid"
