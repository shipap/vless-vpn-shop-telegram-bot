import asyncio

from test_dispatcher import callback_update, find_button, message_update, setup


def test_first_use_exact_controls_and_stale_acceptance():
    async def run():
        app, service, session = setup()
        service.accepted = False
        await app.dispatcher.feed_update(app.bot, message_update())
        rows = session.calls[-1].reply_markup.inline_keyboard
        assert [b.text.split(" ", 1)[1] for row in rows for b in row] == [
            "Политика конфиденциальности",
            "Публичная оферта",
            "Принять",
        ]
        assert rows[-1][0].style == "success"
        token = find_button(session, "Принять")
        service.documents[0]["version"] = "v2"
        await app.dispatcher.feed_update(app.bot, callback_update(token))
        assert not service.accepted
        await app.dispatcher.feed_update(app.bot, callback_update(find_button(session, "Принять")))
        assert service.accepted
        assert len(session.calls[-1].reply_markup.inline_keyboard[1]) == 2

    asyncio.run(run())


def test_legal_gate_blocks_old_shopping_but_preserves_paid_access_and_cancel():
    async def run():
        app, service, session = setup()
        await app.dispatcher.feed_update(app.bot, message_update())
        token = app.button("101", "Старый заказ", "checkout", quote_id="q1", key="old").callback_data
        service.accepted = False
        await app.dispatcher.feed_update(app.bot, callback_update(token))
        assert not service.orders
        await app.execute(
            "101", "9223372036854775700", "connect", {"sid": service.subscriptions[0]["id"]}, None, ""
        )
        assert "личную ссылку" in session.calls[-1].text
        await app.execute(
            "101", "9223372036854775700", "cancel", {"sid": service.subscriptions[0]["id"]}, None, ""
        )
        assert any(c[0] == "cancel" for c in service.calls)

    asyncio.run(run())


def test_compact_menu_and_live_semantic_theme_with_string_icon():
    async def run():
        app, service, session = setup()
        original = service.catalog
        service.catalog = lambda: (
            original()
            | {
                "content": {
                    "telegram_theme": {
                        "icons": {"catalog": {"fallback": "🛍", "custom_emoji_id": "9007199254740993"}},
                        "styles": {"primary": "success"},
                    }
                }
            }
        )
        app.presenter.custom_emoji = True
        await app.dispatcher.feed_update(app.bot, message_update())
        rows = session.calls[-1].reply_markup.inline_keyboard
        assert [len(row) for row in rows] == [1, 2, 2, 2, 2]
        news = find_button(session, "Новости серверов")
        assert app.actions[news].action == "node_news"
        assert app.actions[find_button(session, "Уведомления")].action == "notifications"
        assert find_button(session, "Уведомления")
        assert rows[0][0].style == "success"
        assert rows[0][0].icon_custom_emoji_id == "9007199254740993"
        app.presenter.custom_emoji = False
        await app.dispatcher.feed_update(app.bot, message_update())
        assert session.calls[-1].reply_markup.inline_keyboard[0][0].icon_custom_emoji_id is None
        assert "🛍" in session.calls[-1].reply_markup.inline_keyboard[0][0].text

    asyncio.run(run())


def test_admin_requires_canonical_allowlisted_telegram_and_rechecks_old_button():
    from types import SimpleNamespace

    async def run():
        app, service, session = setup()
        app.accounts = SimpleNamespace(
            settings=SimpleNamespace(
                owner_id=None, admin_email_allowlist=["owner@example.test"], admin_telegram_allowlist=[]
            )
        )
        original = service.resolve_telegram
        account = original(101) | {"email": "owner@example.test", "email_verified": False, "is_admin": True}
        service.resolve_telegram = lambda _, **kwargs: dict(account)
        await app.dispatcher.feed_update(app.bot, message_update())
        assert all(
            "Админ-панель" not in b.text
            for row in session.calls[-1].reply_markup.inline_keyboard
            for b in row
        )
        app.accounts.settings.admin_telegram_allowlist = ["101"]
        await app.dispatcher.feed_update(app.bot, message_update())
        token = find_button(session, "Админ-панель")
        app.accounts.settings.admin_telegram_allowlist = []
        await app.dispatcher.feed_update(app.bot, callback_update(token))
        assert "404" in session.calls[-1].text

    asyncio.run(run())


def test_admin_catalog_order_visibility_highlight_and_price_without_local_math():
    async def run():
        app, service, session = setup()
        original = service.catalog

        def catalog():
            value = original()
            value["products"][0].update(visible_bot=False)
            value["products"][1].update(name="Переименован владельцем", price="451.25", highlighted=True)
            return value

        service.catalog = catalog
        await app.execute("101", "9223372036854775700", "products", {"intent": "purchase"}, None, "")
        assert "451,25" not in session.calls[-1].text
        assert "Стандартный" not in session.calls[-1].text
        assert session.calls[-1].reply_markup.inline_keyboard[0][0].style == "primary"
        assert "Переименован" in session.calls[-1].reply_markup.inline_keyboard[0][0].text

    asyncio.run(run())


def test_rejected_custom_emoji_strips_button_icons_without_repeating_business_action():
    async def run():
        app, _, session = setup()
        app.presenter.custom_emoji = True
        app.theme = {"icons": {"connect": {"fallback": "🔌", "custom_emoji_id": "9007199254740993"}}}
        button = app.button("101", "Подключить VPN", "connect")
        assert button.icon_custom_emoji_id
        session.fail_html = True
        await app.presenter.show("101", [{"type": "text", "text": "Подключение"}], [[button]])
        assert session.calls[-1].reply_markup.inline_keyboard[0][0].icon_custom_emoji_id is None
        assert "Подключить VPN" in session.calls[-1].reply_markup.inline_keyboard[0][0].text

    asyncio.run(run())
