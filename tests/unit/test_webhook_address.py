import pytest
from sunrise_infrastructure.settings import Settings


def settings(**extra):
    return Settings(
        _env_file=None,
        database_url="postgresql+psycopg://test@127.0.0.1:55439/sunrise_test",
        secret_key="x" * 32,
        **extra,
    )


def test_webhook_base_derives_service_without_duplicate_configuration():
    config = settings(webhook_base_url="https://hooks.sunrise.test/")
    assert config.webhook_url("rollypay") == "https://hooks.sunrise.test/rollypay"


@pytest.mark.parametrize(
    "address",
    [
        "http://hooks.sunrise.test",
        "https://secret@hooks.sunrise.test",
        "https://hooks.sunrise.test?token=secret",
        "https://hooks.sunrise.test/path",
    ],
)
def test_webhook_base_rejects_insecure_or_credential_bearing_addresses(address):
    with pytest.raises(ValueError):
        settings(webhook_base_url=address)


def test_webhook_service_cannot_escape_configured_origin():
    with pytest.raises(ValueError):
        settings().webhook_url("../another")
