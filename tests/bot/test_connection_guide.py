"""Real dispatcher navigation with synthetic shared-service instructions only."""

import asyncio
import copy
from html.parser import HTMLParser

from test_dispatcher import callback_update, find_button, message_update, setup

GUIDE = {
    "source": "remnawave",
    "notice": "Используйте сервис в соответствии с офертой.",
    "platforms": [
        {
            "id": "ios",
            "name": "iOS",
            "apps": [
                {
                    "name": "Happ <VPN>",
                    "steps": [
                        {
                            "title": "Установите приложение",
                            "description": "Откройте магазин & установите клиент.",
                            "links": [{"text": "Скачать Happ", "url": "https://apps.example.test/happ"}],
                        },
                        {
                            "title": "Импортируйте подписку",
                            "description": "Скопируйте ссылку и добавьте её в приложение.",
                            "links": [],
                        },
                    ],
                },
                {
                    "name": "Другой клиент",
                    "steps": [
                        {"title": "Другой способ", "description": "Отдельная инструкция.", "links": []}
                    ],
                },
            ],
        },
        {
            "id": "android",
            "name": "Android",
            "apps": [
                {
                    "name": "Android-клиент",
                    "steps": [{"title": "Android", "description": "Своя инструкция.", "links": []}],
                }
            ],
        },
    ],
}


def context(guide=None, *, local=False):
    app, service, session = setup()
    catalog = service.catalog
    service.catalog = lambda: (
        catalog() | {"capabilities": {"provisioning": "local_test" if local else "test_panel"}}
    )

    def instructions(account_id, sid):
        service.calls.append(("instructions", account_id, sid))
        return copy.deepcopy(guide if guide is not None else GUIDE)

    service.connection_instructions = instructions
    return app, service, session


async def connect(app, session):
    await app.dispatcher.feed_update(app.bot, message_update())
    for label in ("Подписки", "Подключить подписку"):
        await press(app, session, label)


async def press(app, session, label, *, uid=101):
    await app.dispatcher.feed_update(app.bot, callback_update(find_button(session, label), uid=uid))


def buttons(session):
    return [button for row in session.calls[-1].reply_markup.inline_keyboard for button in row]


def test_connection_guide_selects_platform_app_and_keeps_subscription_copy_open_without_share_notice():
    async def run():
        app, service, session = context()
        await connect(app, session)
        assert ("instructions", "9223372036854775700", "9007199254740997") in service.calls
        assert "<code>https://vpn.example.test/synthetic</code>" in session.calls[-1].text
        assert GUIDE["notice"] not in session.calls[-1].text
        assert any(button.url == "https://vpn.example.test/synthetic" for button in buttons(session))
        await press(app, session, "iOS")
        assert find_button(session, "Happ <VPN>") and find_button(session, "Другой клиент")
        await press(app, session, "Happ <VPN>")
        assert "Happ &lt;VPN&gt;" in session.calls[-1].text
        assert "магазин &amp;" in session.calls[-1].text
        assert any(button.url == "https://apps.example.test/happ" for button in buttons(session))
        assert session.calls[-1].link_preview_options.is_disabled
        await press(app, session, "Далее")
        assert "Импортируйте подписку" in session.calls[-1].text
        await press(app, session, "Назад")
        assert find_button(session, "Happ <VPN>")
        await press(app, session, "Назад")
        assert find_button(session, "iOS") and find_button(session, "Android")
        assert find_button(session, "Главное меню")

    asyncio.run(run())


def test_connection_guide_callbacks_are_opaque_owner_bound_and_recheck_subscription():
    async def run():
        app, service, session = context()
        await connect(app, session)
        token = find_button(session, "iOS")
        assert len(token.encode()) <= 64 and "ios" not in token and "https" not in token
        before = len(service.calls)
        await app.dispatcher.feed_update(app.bot, callback_update(token, uid=102))
        assert len(service.calls) == before
        service.subscriptions = []
        await app.dispatcher.feed_update(app.bot, callback_update(token))
        assert "Скачать" not in session.calls[-1].text
        assert not any(button.url == "https://apps.example.test/happ" for button in buttons(session))

    asyncio.run(run())


def test_connection_guide_paginates_every_long_step_without_dropping_text_or_splitting_navigation():
    async def run():
        guide = copy.deepcopy(GUIDE)
        chunks = [f"Блок{index:02d} " + "🌞" * 50 for index in range(24)]
        guide["platforms"][0]["apps"][0]["steps"] = [
            {"title": "Все шаги", "description": "\n".join(chunks), "links": []},
            {"title": "Последний шаг", "description": "Завершите подключение. " + "🌙" * 500, "links": []},
        ]
        app, _, session = context(guide)
        await connect(app, session)
        await press(app, session, "iOS")
        await press(app, session, "Happ <VPN>")
        texts = []
        for _ in range(15):
            response = session.calls[-1]

            class Plain(HTMLParser):
                def __init__(self):
                    super().__init__()
                    self.parts = []

                def handle_data(self, value):
                    self.parts.append(value)

            plain = Plain()
            plain.feed(response.text)
            assert len("".join(plain.parts).encode("utf-16-le")) // 2 <= 4096
            texts.append(response.text)
            page_buttons = [button for button in buttons(session) if "Далее" in button.text]
            if not page_buttons:
                break
            if len(texts) > 1:
                assert any(
                    len(row) == 2 and "Предыдущая" in row[0].text and "Далее" in row[1].text
                    for row in response.reply_markup.inline_keyboard
                )
            await press(app, session, "Далее")
        else:
            raise AssertionError("Pagination never reaches the last page")
        assert len(texts) > 1
        assert all(f"Блок{index:02d}" in "".join(texts) for index in range(24))
        assert sum(text.count("🌞") for text in texts) == 1200
        assert "Последний шаг" in "".join(texts)
        assert sum(text.count("🌙") for text in texts) == 500
        assert find_button(session, "Предыдущая")

    asyncio.run(run())


def test_connection_guide_shared_fallback_keeps_existing_help_and_does_not_show_platforms():
    async def run():
        app, _, session = context({"source": "fallback", "platforms": [], "notice": "По оферте."})
        await connect(app, session)
        assert "https://vpn.example.test/synthetic" in session.calls[-1].text
        assert "VPN-клиент" in session.calls[-1].text
        assert not any("iOS" in button.text for button in buttons(session))

    asyncio.run(run())


def test_connection_guide_local_mode_never_requests_external_instructions():
    async def run():
        app, service, session = context(local=True)
        await connect(app, session)
        assert not any(call[0] == "instructions" for call in service.calls)
        assert "Локальный тестовый стенд" in session.calls[-1].text
        assert not any(button.url for button in buttons(session))

    asyncio.run(run())


def test_connection_guide_keeps_distinct_configured_premium_icons_for_choices_and_downloads():
    async def run():
        app, service, session = context()
        catalog = service.catalog
        keys = [
            "connect",
            "connection_qr",
            "connect_heading",
            "devices",
            "quote_devices",
            "device_add",
            "devices_heading",
            "product_select",
            "link",
            "link_heading",
            "quote_target",
            "back",
            "main",
        ]
        icons = {
            key: {"custom_emoji_id": str(7000000000000000100 + index), "fallback": "🔹"}
            for index, key in enumerate(keys)
        }
        service.catalog = lambda: catalog() | {"content": {"telegram_theme": {"icons": icons}}}
        app.presenter.custom_emoji = True

        def icon_for(label):
            return next(button.icon_custom_emoji_id for button in buttons(session) if label in button.text)

        def unique_icons():
            ids = [button.icon_custom_emoji_id for button in buttons(session) if button.icon_custom_emoji_id]
            assert len(ids) == len(set(ids))

        await connect(app, session)
        assert icon_for("Открыть подписку") == icons["connect"]["custom_emoji_id"]
        assert icon_for("iOS") == icons["devices"]["custom_emoji_id"]
        assert icon_for("Android") == icons["quote_devices"]["custom_emoji_id"]
        unique_icons()
        await press(app, session, "iOS")
        assert icon_for("Happ <VPN>") == icons["product_select"]["custom_emoji_id"]
        assert icon_for("Другой клиент") == icons["link"]["custom_emoji_id"]
        unique_icons()
        await press(app, session, "Happ <VPN>")
        assert icon_for("Скачать Happ") == icons["link"]["custom_emoji_id"]
        unique_icons()

    asyncio.run(run())
