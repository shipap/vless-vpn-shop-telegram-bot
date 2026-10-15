"""Production emoji composition with synthetic authority and Telegram transport."""

import asyncio
from types import SimpleNamespace

import pytest
from sunrise_bot.runtime import RuntimeConfig
from sunrise_infrastructure import settings
from test_dispatcher import message_update
from test_instance_brand import branded_app


def typed_config():
    return settings.Settings(
        _env_file=None,
        instance_config_path=None,
        runtime_mode="isolated",
        database_url="postgresql+psycopg://synthetic@127.0.0.1:55439/sunrise_test",
        secret_key="synthetic-emoji-runtime-key-1234567890",  # noqa: S106
    )


@pytest.mark.parametrize("flag", [None, "false", "true"])
def test_typed_verified_emoji_flag_remains_off_until_enabled(monkeypatch, flag):
    monkeypatch.delenv("SUNRISE_TELEGRAM_CUSTOM_EMOJI_VERIFIED", raising=False)
    if flag is not None:
        monkeypatch.setenv("SUNRISE_TELEGRAM_CUSTOM_EMOJI_VERIFIED", flag)
    config = typed_config()
    assert config.telegram_custom_emoji_verified is (flag == "true")


@pytest.mark.parametrize("flag", ["unknown", "", "not-verified"])
def test_typed_emoji_flag_rejects_malformed_values(monkeypatch, flag):
    monkeypatch.setenv("SUNRISE_TELEGRAM_CUSTOM_EMOJI_VERIFIED", flag)
    with pytest.raises(ValueError):
        typed_config()


@pytest.mark.parametrize("enabled", [False, True])
def test_production_runtime_passes_verified_flag_to_real_menu_rendering(monkeypatch, enabled):
    # The factory is synthetic; this does not attest a real production database.
    config = settings.Settings.model_construct(
        runtime_mode="production",
        database_url="postgresql+psycopg://synthetic@synthetic.invalid/synthetic",
        bot_token="700001:synthetic-token-1234567890",  # noqa: S106 -- synthetic token
        telegram_custom_emoji_verified=enabled,
    )
    policy = SimpleNamespace(production=True, scope=SimpleNamespace(bot_id="700001"))
    observed = []

    def policy_for(value):
        observed.append(value)
        return policy

    monkeypatch.setenv("SUNRISE_RUNTIME_MODE", "production")
    monkeypatch.setattr(settings, "Settings", lambda: config)
    monkeypatch.setattr(settings.RuntimePolicy, "for_settings", policy_for)
    current = RuntimeConfig.from_env()
    assert observed == [config]
    assert current.production_settings is config
    assert current.safety.runtime_policy is policy
    assert current.custom_emoji_verified is enabled
    with pytest.raises(ValueError, match="production_environment_override_denied"):
        RuntimeConfig.from_env({"SUNRISE_RUNTIME_MODE": "production"})

    async def run():
        app, service, session = branded_app(custom_emoji=current.custom_emoji_verified)
        original = service.catalog
        service.catalog = lambda: (
            original()
            | {
                "content": {
                    "telegram_theme": {
                        "icons": {
                            "menu_heading": {"fallback": "🌐", "custom_emoji_id": "9007199254740993"},
                            "catalog": {"fallback": "🛍", "custom_emoji_id": "9007199254740995"},
                        }
                    }
                }
            }
        )
        await app.dispatcher.feed_update(app.bot, message_update())
        menu = session.calls[-1]
        assert menu.parse_mode == "HTML"
        assert ('<tg-emoji emoji-id="9007199254740993">🌐</tg-emoji>' in menu.text) is enabled
        button = menu.reply_markup.inline_keyboard[0][0]
        assert button.icon_custom_emoji_id == ("9007199254740995" if enabled else None)
        assert button.callback_data

    asyncio.run(run())
