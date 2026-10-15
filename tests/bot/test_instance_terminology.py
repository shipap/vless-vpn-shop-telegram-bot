"""Configured service phrases reach real bot screens without rewriting owner content."""

import asyncio
from copy import deepcopy

import pytest
from sunrise_infrastructure.instance_config import InstanceConfig
from sunrise_infrastructure.service_content import help_center
from test_connection_guide import GUIDE
from test_dispatcher import callback_update, find_button
from test_instance_brand import branded_app


def private_instance():
    return InstanceConfig(
        app_name="Частный сервис",
        terminology={
            "service_label": "Защищённое подключение",
            "service_summary": "Защищённое подключение для ваших устройств",
            "connections_label": "Мои подключения",
            "connect_label": "Подключиться",
            "client_label": "приложение",
            "client_installation_step": "Установите совместимое приложение, например Incy или Happ.",
            "connection_label": "подключение",
            "hero_title": "Защищённое подключение.",
            "welcome_body": "Защищённое подключение, подписки и платежи — в одном месте.",
            "provisioning_pending_body": "Оплата получена, готовим подключение. Повторно оплачивать не нужно.",
            "provisioning_ready_body": "Оплата получена. Доступ выдан. Откройте подписку для подключения.",
        },
    )


def private_app(**kwargs):
    instance = private_instance()
    app, service, session = branded_app(instance, **kwargs)
    service.subscriptions[0]["name"] = "Семейная подписка"
    original = service.catalog

    def catalog():
        result = original()
        result["content"]["help_center"] = help_center(app.accounts.settings, {})
        result["content"]["telegram_theme"] = {
            "icons": {"connect_heading": {"fallback": "🌐", "custom_emoji_id": "9007199254740993"}}
        }
        return result

    service.catalog = catalog
    return app, service, session


@pytest.mark.parametrize("fail_html", [False, True])
def test_configured_connection_screen_preserves_action_and_semantic_header_on_format_fallback(fail_html):
    async def run():
        app, service, session = private_app(custom_emoji=True, fail_html=fail_html)
        await app.execute(
            "101", "9223372036854775700", "subscription", {"sid": service.subscriptions[0]["id"]}, None, ""
        )
        await app.dispatcher.feed_update(app.bot, callback_update(find_button(session, "Подключить подписку")))
        screen = session.calls[-1]
        assert "Подключить подписку" in screen.text
        assert "совместимое приложение" in screen.text
        assert "Добавьте её в приложение как подписку." in screen.text
        assert "VPN" not in screen.text
        assert any(
            button.url == "https://vpn.example.test/synthetic"
            for row in screen.reply_markup.inline_keyboard
            for button in row
        )
        if fail_html:
            assert screen.parse_mode is None and "🌐" in screen.text
        else:
            assert 'emoji-id="9007199254740993"' in screen.text

    asyncio.run(run())


def test_private_welcome_and_provisioning_status_use_configured_complete_phrases():
    async def run():
        app, service, session = private_app()
        await app.main("101", {})
        assert "Защищённое подключение, подписки и платежи — в одном месте." in session.calls[-1].text
        assert "Ваш VPN" not in session.calls[-1].text
        service.create_order("9223372036854775700", "q1", "synthetic", "bot")
        await app.execute("101", "9223372036854775700", "order", {"order_id": "o1"}, None, "")
        assert "Оплата получена, готовим подключение." in session.calls[-1].text
        service.orders["synthetic"].update(
            status="paid",
            environment="sandbox",
            subscription_id=service.subscriptions[0]["id"],
            provisioning_status="ready",
        )
        await app.execute("101", "9223372036854775700", "order", {"order_id": "o1"}, None, "")
        assert "Доступ выдан. Откройте подписку для подключения." in session.calls[-1].text
        assert "VPN" not in session.calls[-1].text

    asyncio.run(run())


def test_empty_configured_welcome_keeps_menu_and_actions_without_subtitle():
    async def run():
        instance = InstanceConfig(app_name="Частный сервис", terminology={"welcome_body": ""})
        app, service, session = branded_app(instance)
        await app.main("101", {})
        screen = session.calls[-1]
        assert "Частный сервис" in screen.text
        assert "в одном месте" not in screen.text
        assert find_button(session, "Каталог") is not None
        assert find_button(session, "Подписки") is not None

    asyncio.run(run())


def test_configured_structured_guide_uses_private_phrases_and_preserves_real_application_name():
    async def run():
        app, service, session = private_app()
        guide = deepcopy(GUIDE)
        service.connection_instructions = lambda aid, sid: deepcopy(guide)
        sid = service.subscriptions[0]["id"]
        await app.execute("101", "9223372036854775700", "connect", {"sid": sid}, None, "")
        assert "Подключить подписку" in session.calls[-1].text
        assert "VPN-приложение" not in session.calls[-1].text
        await app.dispatcher.feed_update(app.bot, callback_update(find_button(session, "iOS")))
        await app.dispatcher.feed_update(app.bot, callback_update(find_button(session, "Happ <VPN>")))
        assert "Happ &lt;VPN&gt;" in session.calls[-1].text
        assert "Добавьте её в приложение как подписку." in session.calls[-1].text
        assert guide == GUIDE

    asyncio.run(run())


def test_private_connection_keeps_database_instructions_without_blanket_word_replacement():
    async def run():
        app, service, session = private_app()
        original = service.catalog

        def catalog():
            result = original()
            result["content"]["help_center"]["connection_instructions"] = (
                "Инструкция владельца: приложение VPN Example."
            )
            return result

        service.catalog = catalog
        await app.execute(
            "101", "9223372036854775700", "connect", {"sid": service.subscriptions[0]["id"]}, None, ""
        )
        assert "Инструкция владельца: приложение VPN Example." in session.calls[-1].text
        assert "Подключить подписку" in session.calls[-1].text

    asyncio.run(run())
