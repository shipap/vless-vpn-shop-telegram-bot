import pytest
from pydantic import ValidationError
from sunrise_api.schemas import CodeRequest, LinkRequest
from sunrise_infrastructure.settings import Settings


def test_link_username_is_supported_as_an_alternative_to_lossless_numeric_id():
    assert LinkRequest(telegram_username="@Example_name").telegram_username == "@Example_name"
    assert LinkRequest(telegram_id="9007199254740993").telegram_id == "9007199254740993"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"telegram_id": "123", "telegram_username": "example"},
        {"telegram_username": "https://t.me/example"},
        {"telegram_username": "пример"},
        {"telegram_id": "9223372036854775808"},
        {"telegram_id": "0"},
        {"telegram_id": 9007199254740993},
        {"telegram_id": 9007199254740992.0},
        {"telegram_id": True},
    ],
)
def test_link_contract_rejects_ambiguous_or_invalid_identity(payload):
    with pytest.raises(ValidationError):
        LinkRequest(**payload)


@pytest.mark.parametrize("accepted", ["true", 1, None])
def test_login_consent_requires_a_json_boolean(accepted):
    with pytest.raises(ValidationError):
        CodeRequest(challenge_id="synthetic", code="123456", legal_accepted=accepted)


def test_payment_review_fixture_isolation_does_not_allow_a_nonlocal_smtp_tunnel():
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            database_url="postgresql+psycopg://synthetic@127.0.0.1:55439/sunrise_test_backend",
            secret_key="synthetic-settings-guard-key-" * 3,
            local_test=False,
            smtp_test_authorized=False,
            smtp_tunnel_port=1587,
            email_allowlist=[],
        )
