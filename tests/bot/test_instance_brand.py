"""Consumer-visible instance isolation over real bot flows and synthetic transport."""

import asyncio
from html import escape
from types import SimpleNamespace

import pytest
from aiogram import Bot
from sunrise_bot.adapter import BotApplication
from sunrise_bot.safety import BotSafety
from sunrise_infrastructure.instance_config import InstanceConfig
from test_admin_flow import Console
from test_dispatcher import FakeCommerce, callback_update, find_button, message_update
from test_presentation import TOKEN, FakeSession


def branded_app(configured=None, *, custom_emoji=False, fail_html=False, **kwargs):
    service, session = FakeCommerce(), FakeSession(fail_html=fail_html)
    settings = SimpleNamespace(instance=configured or InstanceConfig(), admin_email_allowlist=[])
    app = BotApplication(
        Bot(TOKEN, session=session),
        service,
        BotSafety(frozenset({"700001"}), frozenset({"101"})),
        accounts=SimpleNamespace(settings=settings),
        custom_emoji=custom_emoji,
        **kwargs,
    )
    return app, service, session


@pytest.mark.parametrize("name", ["Южный VPN", "Север VPN", "A <VPN> & Shop", "Длинное имя проекта " * 5])
def test_configured_name_survives_legal_gate_and_opens_compact_menu(name):
    async def run():
        app, service, session = branded_app(InstanceConfig(app_name=name))
        service.accepted = False
        await app.dispatcher.feed_update(app.bot, message_update())
        legal = session.calls[-1]
        assert f"Добро пожаловать в {escape(name.strip())}" in legal.text
        assert len(legal.reply_markup.inline_keyboard) == 3
        await app.dispatcher.feed_update(app.bot, callback_update(find_button(session, "Принять")))
        assert service.accepted
        menu = session.calls[-1]
        assert f"Меню {escape(name.strip())}" in menu.text
        assert [len(row) for row in menu.reply_markup.inline_keyboard] == [1, 2, 2, 2, 2]
        assert find_button(session, "Новости серверов")
        assert all("Главное меню" not in b.text for row in menu.reply_markup.inline_keyboard for b in row)
        assert "<VPN>" not in menu.text

    asyncio.run(run())


def test_neutral_default_without_assets_has_clean_text_and_unicode_menu():
    async def run():
        app, _, session = branded_app()
        await app.dispatcher.feed_update(app.bot, message_update())
        menu = session.calls[-1]
        assert "Меню VLESS VPN Shop" in menu.text
        assert "sunrise" not in menu.text.casefold()
        assert "bububi" not in menu.text.casefold()
        assert "🌐" in menu.text
        assert all(b.icon_custom_emoji_id is None for row in menu.reply_markup.inline_keyboard for b in row)
        assert menu.reply_markup.inline_keyboard[0][0].callback_data

    asyncio.run(run())


def test_two_concurrent_objects_keep_frozen_brand_and_semantic_premium_headers():
    async def run():
        first, first_service, first_session = branded_app(
            InstanceConfig(app_name="Первый VPN"), custom_emoji=True
        )
        second, second_service, second_session = branded_app(
            InstanceConfig(app_name="Второй VPN"), custom_emoji=True
        )
        for service in (first_service, second_service):
            original = service.catalog
            service.catalog = lambda original=original: (
                original()
                | {
                    "content": {
                        "telegram_theme": {
                            "icons": {
                                "menu_heading": {"fallback": "🌐", "custom_emoji_id": "9007199254740993"},
                                "legal_heading": {"fallback": "⚖️", "custom_emoji_id": "9007199254740995"},
                            }
                        }
                    }
                }
            )
        first.accounts.settings.instance = InstanceConfig(app_name="Поздняя подмена")
        for accepted in (False, True, False, True):
            first_service.accepted = second_service.accepted = accepted
            await asyncio.gather(first.main("101", {}), second.main("101", {}))
            for session, own, foreign in (
                (first_session, "Первый VPN", "Второй VPN"),
                (second_session, "Второй VPN", "Первый VPN"),
            ):
                text = session.calls[-1].text
                identifier = "9007199254740993" if accepted else "9007199254740995"
                assert own in text and foreign not in text and "Поздняя подмена" not in text
                assert f'<tg-emoji emoji-id="{identifier}">' in text

    asyncio.run(run())


def test_explicit_instance_injection_overrides_account_settings():
    async def run():
        app, _, session = branded_app(
            InstanceConfig(app_name="Аккаунты"), instance=InstanceConfig(app_name="Явный VPN")
        )
        await app.main("101", {})
        assert "Меню Явный VPN" in session.calls[-1].text
        assert "Аккаунты" not in session.calls[-1].text

    asyncio.run(run())


def test_optional_missing_assets_do_not_trigger_media_delivery():
    async def run():
        app, _, session = branded_app(
            InstanceConfig(app_name="Без картинки", logo_asset="missing.svg", hero_asset="missing.webp")
        )
        await app.main("101", {})
        assert "Меню Без картинки" in session.calls[-1].text
        assert all(call.__api_method__ == "sendMessage" for call in session.calls)

    asyncio.run(run())


def test_format_failure_preserves_configured_brand_and_working_buttons():
    async def run():
        app, _, session = branded_app(
            InstanceConfig(app_name="Другой <VPN>"), custom_emoji=True, fail_html=True
        )
        await app.main("101", {})
        delivered = session.calls[-1]
        assert "Меню Другой <VPN>" in delivered.text
        assert delivered.parse_mode is None
        assert "<tg-emoji" not in delivered.text
        assert "🌐" in delivered.text
        assert delivered.reply_markup.inline_keyboard[0][0].callback_data

    asyncio.run(run())


def test_admin_console_uses_same_configured_brand():
    async def run():
        app, service, session = branded_app(InstanceConfig(app_name="Новый магазин"))
        app.admin_console = Console()
        app.accounts.settings.admin_email_allowlist = ["owner@example.test"]
        app.accounts.settings.admin_telegram_allowlist = ["101"]
        account = service.resolve_telegram(101) | {"email": "owner@example.test", "email_verified": True}
        service.account_info = lambda _: account
        await app.admin_flow.execute("101", account["id"], "admin", {}, None)
        assert "Новый магазин · управление" in session.calls[-1].text
        assert "sunrise" not in session.calls[-1].text.casefold()

    asyncio.run(run())


def test_owner_menu_content_keeps_configured_header_and_original_ast():
    async def run():
        app, service, session = branded_app(InstanceConfig(app_name="Настроенный VPN"), custom_emoji=True)
        nodes = [{"type": "paragraph", "children": [{"type": "text", "text": "Информация владельца"}]}]
        original = service.catalog
        service.catalog = lambda: (
            original()
            | {
                "content": {
                    "bot_menu": nodes,
                    "telegram_theme": {
                        "icons": {"menu_heading": {"fallback": "🌐", "custom_emoji_id": "9007199254740993"}}
                    },
                }
            }
        )
        await app.main("101", {})
        text = session.calls[-1].text
        assert "Меню Настроенный VPN" in text
        assert "Информация владельца" in text
        assert '<tg-emoji emoji-id="9007199254740993">🌐</tg-emoji>' in text
        assert nodes == [
            {"type": "paragraph", "children": [{"type": "text", "text": "Информация владельца"}]}
        ]

    asyncio.run(run())


@pytest.mark.parametrize(
    "alias,expected_id", [(None, None), ("sunrise", "9007199254740993"), ("missing", None)]
)
def test_private_registry_icon_requires_explicit_instance_alias(alias, expected_id):
    async def run():
        app, service, session = branded_app(
            InstanceConfig(app_name="Проект", telegram_menu_emoji_key=alias), custom_emoji=True
        )
        node = {"type": "emoji", "id": "9007199254740993", "fallback": "🌅", "label": "Частный значок"}
        service.content_registry = lambda: {"sunrise": node}
        await app.main("101", {})
        text = session.calls[-1].text
        assert "Меню Проект" in text
        if expected_id:
            assert f'<tg-emoji emoji-id="{expected_id}">🌅</tg-emoji>' in text
        else:
            assert "<tg-emoji" not in text
            assert "🌐" in text and "🌅" not in text
        assert node["id"] == "9007199254740993"

    asyncio.run(run())


def test_runtime_composition_uses_shared_instance_without_external_calls(monkeypatch):
    from sunrise_bot import runtime
    from sunrise_infrastructure.settings import Settings

    settings = Settings(
        _env_file=None,
        instance_config_path=None,
        app_name="Единая настройка",
        database_url="postgresql://localhost:55439/sunrise_test_bot",
        secret_key="synthetic-runtime-key-" * 2,
        local_test=True,
    )
    session = FakeSession()

    def deny(*args, **kwargs):
        raise AssertionError("Runtime composition attempted an external operation")

    monkeypatch.setattr("sunrise_infrastructure.settings.Settings", lambda **kwargs: settings)
    monkeypatch.setattr("sqlalchemy.engine.Engine.connect", deny)
    monkeypatch.setattr("redis.Redis.execute_command", deny)
    monkeypatch.setattr(runtime, "AiohttpSession", lambda: session)
    app = runtime.compose(
        runtime.RuntimeConfig(
            TOKEN, settings.database_url, BotSafety(frozenset({"700001"}), frozenset({"101"}))
        )
    )
    try:
        assert app.instance is app.accounts.settings.instance
        assert app.commerce.public_settings is app.accounts.settings
        assert app.admin_console.settings is app.accounts.settings
        assert session.calls == []
        app.commerce = FakeCommerce()
        asyncio.run(app.main("101", {}))
        assert "Меню Единая настройка" in session.calls[-1].text
    finally:
        app.accounts.db.engine.dispose()
