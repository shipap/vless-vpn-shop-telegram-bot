import importlib
import sys

import pytest
from sunrise_bot.runtime import RuntimeConfig
from test_presentation import TOKEN


def test_runtime_rejects_missing_approved_lists_before_composition():
    with pytest.raises(ValueError):
        RuntimeConfig.from_env({"SUNRISE_BOT_TEST_TOKEN": TOKEN})


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://localhost/production",
        "postgresql://example.com/sunrise_test",
        "sqlite:///live.db",
        "postgresql://localhost:55439/sunrise_test_production",
        "postgresql://localhost:5432/sunrise_test",
    ],
)
def test_runtime_rejects_non_synthetic_or_remote_db(url):
    with pytest.raises(ValueError):
        RuntimeConfig.from_env(
            {
                "SUNRISE_BOT_TEST_TOKEN": TOKEN,
                "SUNRISE_TEST_BOT_IDS": "700001",
                "SUNRISE_TEST_TELEGRAM_RECIPIENTS": "101",
                "SUNRISE_TEST_DATABASE_URL": url,
            }
        )


def test_runtime_import_has_no_polling_or_database_connection(monkeypatch):
    from aiogram import Dispatcher

    def deny(*args, **kwargs):
        raise AssertionError("Import attempted a runtime action")

    monkeypatch.setattr(Dispatcher, "start_polling", deny)
    monkeypatch.setattr("sqlalchemy.create_engine", deny)
    sys.modules.pop("sunrise_bot.runtime", None)
    importlib.import_module("sunrise_bot.runtime")


def test_premium_capability_is_off_until_explicitly_verified():
    env = {
        "SUNRISE_BOT_TEST_TOKEN": TOKEN,
        "SUNRISE_TEST_BOT_IDS": "700001",
        "SUNRISE_TEST_TELEGRAM_RECIPIENTS": "101",
        "SUNRISE_TEST_DATABASE_URL": "postgresql://localhost:55439/sunrise_test_bot",
    }
    assert RuntimeConfig.from_env(env).custom_emoji_verified is False
    assert (
        RuntimeConfig.from_env(env | {"SUNRISE_TELEGRAM_CUSTOM_EMOJI_VERIFIED": "true"}).custom_emoji_verified
        is True
    )
