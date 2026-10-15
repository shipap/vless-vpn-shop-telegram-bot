"""Компактная лента: только синтетический транспорт, без БД и Telegram-сети."""

import asyncio

import pytest
from test_dispatcher import callback_update, find_button, setup

AID = "9223372036854775700"


def event(index, title="Тестовый сервер", state="outage"):
    return {
        "id": f"news-{index}",
        "title": title,
        "state": state,
        "occurred_at": f"2026-10-14T12:{59 - index:02}:00Z",
        "internal_uuid": "hidden-node",
        "ip": "hidden-address",
    }


def news_setup(items, next_cursor=None):
    app, service, session = setup()
    reads = []

    def news(account_id, *, before=None, limit=20):
        reads.append((account_id, before, limit))
        return {"items": items, "next_cursor": next_cursor}

    service.node_news = news
    return app, service, session, reads


def test_repeated_interruptions_collapse_to_latest_event_per_server_on_page():
    async def run():
        app, _, session, reads = news_setup(
            [event(0, state="recovered"), event(1), event(2), event(3), event(4)]
        )
        await app.execute("101", AID, "node_news", {}, None, "")
        screen = session.calls[-1]
        assert screen.text.count("Тестовый сервер") == 1
        assert "Работа восстановлена" in screen.text
        assert "Перебои в работе" not in screen.text
        assert "14.10.2026 15:59 МСК" in screen.text
        assert "Событий на странице: 5" in screen.text
        assert "24 часа" in screen.text
        assert "hidden-" not in screen.text
        assert reads == [(AID, None, 5)]
        assert find_button(session, "История")
        assert not any(
            "Обновить" in button.text
            for row in screen.reply_markup.inline_keyboard
            for button in row
        )
        assert len(session.calls) == 1

    asyncio.run(run())


def test_history_reads_current_owned_feed_and_back_preserves_older_page():
    async def run():
        app, _, session, reads = news_setup([event(0), event(1, state="recovered")], "older")
        await app.execute("101", AID, "node_news", {"before": "page-cursor"}, 44, "")
        assert "Последнее событие на странице" in session.calls[-1].text
        assert find_button(session, "К новым")
        token = find_button(session, "История")
        assert len(token.encode()) <= 64
        await app.dispatcher.feed_update(app.bot, callback_update(token))
        screen = session.calls[-1]
        assert screen.text.count("Тестовый сервер") == 1
        assert "Перебои в работе" in screen.text
        assert "Работа восстановлена" in screen.text
        assert reads[-1] == (AID, "page-cursor", 5)
        back = app.actions[find_button(session, "Назад")]
        assert back.action == "node_news"
        assert back.data == {"before": "page-cursor"}
        assert "hidden-" not in screen.text

    asyncio.run(run())


def test_history_does_not_keep_expired_events_in_callback_payload():
    async def run():
        items = [event(0)]
        app, _, session, reads = news_setup(items)
        await app.execute("101", AID, "node_news", {}, None, "")
        token = find_button(session, "История")
        assert "items" not in app.actions[token].data
        items.clear()
        await app.dispatcher.feed_update(app.bot, callback_update(token))
        assert "Событий на этой странице больше нет" in session.calls[-1].text
        assert "Тестовый сервер" not in session.calls[-1].text
        assert len(reads) == 2

    asyncio.run(run())


@pytest.mark.parametrize("enabled", [False, True])
def test_news_uses_configured_pack_and_escapes_server_caption(enabled):
    async def run():
        app, service, session, _ = news_setup([event(0, "Сервер <VPN>&", "recovered")])
        app.presenter.custom_emoji = enabled
        original = service.catalog

        def catalog():
            value = original()
            value["content"]["telegram_theme"] = {
                "icons": {
                    key: {"fallback": fallback, "custom_emoji_id": identifier}
                    for key, fallback, identifier in (
                        ("node_news", "📊", "9007199254740993"),
                        ("success_heading", "☑️", "9007199254740995"),
                    )
                }
            }
            return value

        service.catalog = catalog
        await app.execute("101", AID, "node_news", {}, None, "")
        screen = session.calls[-1]
        assert "Сервер &lt;VPN&gt;&amp;" in screen.text
        assert ("9007199254740993" in screen.text) is enabled
        assert ("9007199254740995" in screen.text) is enabled
        history = next(
            button
            for row in screen.reply_markup.inline_keyboard
            for button in row
            if "История" in button.text
        )
        assert history.icon_custom_emoji_id == ("9007199254740993" if enabled else None)

    asyncio.run(run())


def test_empty_feed_keeps_navigation_and_no_history_button():
    async def run():
        app, _, session, _ = news_setup([])
        await app.execute("101", AID, "node_news", {}, None, "")
        assert "За последние 24 часа событий нет" in session.calls[-1].text
        assert find_button(session, "Главное меню")
        assert not any(action.data.get("server") for action in app.actions.values())

    asyncio.run(run())


def test_multiple_servers_remain_separate_and_cursor_is_kept_for_older_events():
    async def run():
        app, _, session, _ = news_setup(
            [event(0, "Сервер А", "recovered"), event(1, "Сервер Б"), event(2, "Сервер А")],
            "next-page",
        )
        await app.execute("101", AID, "node_news", {}, None, "")
        screen = session.calls[-1]
        assert screen.text.count("Сервер А") == screen.text.count("Сервер Б") == 1
        assert screen.text.index("Сервер А") < screen.text.index("Сервер Б")
        older = app.actions[find_button(session, "Более ранние события")]
        assert older.data["before"] == "next-page"
        detail = next(action for action in app.actions.values() if action.data.get("server") == "Сервер Б")
        await app.execute("101", AID, detail.action, detail.data, 44, "")
        assert "Сервер А" not in session.calls[-1].text
        assert "Сервер Б" in session.calls[-1].text

    asyncio.run(run())


def test_foreign_history_callback_cannot_read_the_owners_feed():
    async def run():
        app, _, session, reads = news_setup([event(0)])
        await app.execute("101", AID, "node_news", {}, None, "")
        token = find_button(session, "История")
        await app.dispatcher.feed_update(app.bot, callback_update(token, uid=102))
        assert reads == [(AID, None, 5)]

    asyncio.run(run())


@pytest.mark.parametrize("has_subscription", [False, True])
def test_empty_news_explains_subscription_requirement_only_for_non_subscriber(has_subscription):
    async def run():
        app, service, session, reads = news_setup([])
        dashboard_reads = []

        def dashboard(account_id, **kwargs):
            dashboard_reads.append((account_id, kwargs))
            return {"subscriptions": [{"id": "owned-subscription"}] if has_subscription else []}

        service.dashboard = dashboard
        await app.execute("101", AID, "node_news", {}, None, "")
        text = session.calls[-1].text
        if has_subscription:
            assert "За последние 24 часа событий нет" in text
            assert "нужна подписка" not in text
        else:
            assert "Для получения информации о серверах нужна подписка" in text
            assert "Новостей пока нет" not in text
        assert reads == [(AID, None, 5)]
        assert dashboard_reads == [(AID, {})]

    asyncio.run(run())
