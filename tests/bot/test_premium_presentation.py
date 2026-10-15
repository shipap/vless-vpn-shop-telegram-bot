import asyncio

import pytest
from aiogram import Bot
from sunrise_bot.presentation import Presenter
from test_dispatcher import callback_update, message_update, setup
from test_presentation import TOKEN, FakeSession


def test_verified_emoji_status_does_not_claim_it_is_unconfirmed():
    presenter = Presenter(Bot(TOKEN, session=FakeSession()), custom_emoji=True)
    status = presenter.capability_status()
    assert status["custom_emoji"] is True
    assert status["rich_messages"] is False
    assert "не подтверждены" not in status["reason"]
    assert "custom emoji" in status["reason"]


@pytest.mark.parametrize("action", ["legal", "connect", "products", "quote", "checkout", "order"])
def test_static_cards_use_configured_premium_header(action):
    async def run():
        app, service, session = setup()
        app.presenter.custom_emoji = True
        original = service.catalog
        key = {
            "legal": "legal_heading",
            "connect": "connect_heading",
            "products": "catalog_heading",
            "quote": "quote_heading",
            "checkout": "order_heading",
            "order": "order_heading",
        }[action]
        service.catalog = lambda: (
            original()
            | {
                "content": {
                    "telegram_theme": {
                        "icons": {key: {"fallback": "☀️", "custom_emoji_id": "9007199254740993"}}
                    }
                }
            }
        )
        if action == "legal":
            await app.legal_gate("101", service.legal_status("9223372036854775700"))
            assert len(session.calls[-1].reply_markup.inline_keyboard) == 3
            assert session.calls[-1].reply_markup.inline_keyboard[-1][0].style == "success"
        else:
            data = {
                "sid": service.subscriptions[0]["id"],
                "intent": "purchase",
                "product_id": "standard",
                "period_days": 30,
                "quote_id": "q1",
                "key": "synthetic-premium-card",
                "order_id": "o1",
            }
            if action == "order":
                service.create_order("9223372036854775700", "q1", "synthetic-order", source="bot")
            await app.execute("101", "9223372036854775700", action, data, None, "")
        assert '<tg-emoji emoji-id="9007199254740993">☀️</tg-emoji>' in session.calls[-1].text
        assert session.calls[-1].parse_mode == "HTML"

    asyncio.run(run())


def test_shared_registry_header_overrides_theme_without_rewriting_owner_content():
    async def run():
        app, service, session = setup()
        app.presenter.custom_emoji = True
        app.theme = {"icons": {"main": {"fallback": "☀️", "custom_emoji_id": "9007199254740993"}}}
        service.content_registry = lambda: {
            "help_heading": {"type": "emoji", "id": "9007199254740995", "fallback": "🌅", "label": "Рассвет"}
        }
        await app.show("101", "Помощь", "Инструкция")
        assert '<tg-emoji emoji-id="9007199254740995">🌅</tg-emoji>' in session.calls[-1].text
        owner_nodes = [{"type": "heading", "children": [{"type": "text", "text": "Мой заголовок"}]}]
        await app.show("101", "Помощь", "", nodes=owner_nodes)
        assert session.calls[-1].text == "<b>Мой заголовок</b>"
        assert owner_nodes == [{"type": "heading", "children": [{"type": "text", "text": "Мой заголовок"}]}]

    asyncio.run(run())


@pytest.mark.parametrize("configured_back", [False, True])
def test_back_navigation_uses_configured_icon_without_changing_action(configured_back):
    async def run():
        app, _, session = setup()
        app.presenter.custom_emoji = True
        icons = {"main": {"fallback": "☀️", "custom_emoji_id": "9007199254740993"}}
        if configured_back:
            icons["back"] = {"fallback": "⬅️", "custom_emoji_id": "9007199254740995"}
        app.theme = {"icons": icons}
        await app.show("101", "Подписка", "Описание", back="subscription", back_data={"sid": "synthetic"})
        back, main = session.calls[-1].reply_markup.inline_keyboard[-1]
        assert back.icon_custom_emoji_id == ("9007199254740995" if configured_back else None)
        if not configured_back:
            assert "⬅️" in back.text
        assert main.icon_custom_emoji_id == "9007199254740993"
        assert app.actions[back.callback_data].action == "subscription"
        assert app.actions[back.callback_data].data == {"sid": "synthetic"}

    asyncio.run(run())


@pytest.mark.parametrize("stale_theme", [False, True])
@pytest.mark.parametrize("custom_emoji", [False, True])
def test_link_confirmation_loads_current_theme_and_keeps_explicit_owner_proof(stale_theme, custom_emoji):
    class FakeAccounts:
        def __init__(self):
            self.calls = []

        def confirm_telegram_link(self, account_id, proof):
            self.calls.append((str(account_id), proof))
            return {"id": "9007199254740999"}

    async def run():
        app, service, session = setup()
        app.accounts = FakeAccounts()
        app.presenter.custom_emoji = custom_emoji
        if stale_theme:
            app.theme = {"icons": {"link_confirm": {"custom_emoji_id": "123", "fallback": "❌"}}}
        original = service.catalog
        icons = {
            "link_confirm": {"custom_emoji_id": "9007199254740993", "fallback": "✅"},
            "back": {"custom_emoji_id": "9007199254740995", "fallback": "⬅️"},
            "main": {"custom_emoji_id": "9007199254740997", "fallback": "🏠"},
            "confirm_heading": {"custom_emoji_id": "9007199254740999", "fallback": "❔"},
        }
        service.catalog = lambda: original() | {"content": {"telegram_theme": {"icons": icons}}}
        await app.dispatcher.feed_update(app.bot, message_update(text="/link synthetic-proof.secret"))
        assert not app.accounts.calls
        markup = session.calls[-1].reply_markup.inline_keyboard
        confirm, back, main = markup[0][0], *markup[-1]
        for button, key, label in (
            (confirm, "link_confirm", "Объединить аккаунты"),
            (back, "back", "Назад"),
            (main, "main", "Главное меню"),
        ):
            assert button.icon_custom_emoji_id == (icons[key]["custom_emoji_id"] if custom_emoji else None)
            expected_text = label if custom_emoji else icons[key]["fallback"] + " " + label
            assert button.text == expected_text
        assert app.actions[confirm.callback_data].data == {"proof": "synthetic-proof.secret"}
        assert app.actions[back.callback_data].action == app.actions[main.callback_data].action == "main"
        assert back.style == main.style == "danger"
        await app.dispatcher.feed_update(app.bot, callback_update(confirm.callback_data, uid=102))
        assert not app.accounts.calls
        await app.dispatcher.feed_update(app.bot, callback_update(back.callback_data))
        assert not app.accounts.calls
        await app.dispatcher.feed_update(app.bot, callback_update(confirm.callback_data))
        assert app.accounts.calls == [("9223372036854775700", "synthetic-proof.secret")]

    asyncio.run(run())
