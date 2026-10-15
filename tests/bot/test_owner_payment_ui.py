"""Synthetic transport regressions for subscription Back and automatic order updates."""

import asyncio

import pytest
from test_dispatcher import callback_update, find_button, setup

UID, AID = "101", "9223372036854775700"


@pytest.mark.parametrize(
    "token,title", [("notice:subscriptions", "Выберите подписку"), ("notice:main", "Меню")]
)
def test_durable_notice_buttons_open_current_senders_navigation(token, title):
    async def run():
        app, _, session = setup()
        assert not app.actions
        await app.dispatcher.feed_update(app.bot, callback_update(token))
        assert title in session.calls[-1].text
        # Durable navigation never grants the identity of the original notice recipient.
        item = app.callback_action(token, "202")
        assert item.owner == "202" and "account_id" not in item.data
        assert app.callback_action("notice:admin", "202") is None

    asyncio.run(run())


def test_single_subscription_back_opens_list_then_exits_to_main():
    async def run():
        app, _, session = setup()
        await app.execute(UID, AID, "subscriptions", {}, None, "")
        await app.dispatcher.feed_update(app.bot, callback_update(find_button(session, "Назад")))
        assert "Выберите подписку" in session.calls[-1].text
        await app.dispatcher.feed_update(app.bot, callback_update(find_button(session, "Назад")))
        assert "Меню" in session.calls[-1].text

    asyncio.run(run())


def test_subscription_picker_and_detail_use_database_product_name_and_premium_emoji():
    async def run():
        app, service, session = setup()
        app.presenter.custom_emoji = True
        service.subscriptions.append({**service.subscriptions[0], "id": "second", "product_id": "universal"})
        catalog = service.catalog

        def product_catalog():
            result = catalog()
            result["products"][0]["content"] = {
                "name": [
                    {"type": "emoji", "id": "91001", "fallback": "⬛"},
                    {"type": "text", "text": " Купленный тариф"},
                ]
            }
            return result

        service.catalog = product_catalog
        await app.execute(UID, AID, "subscriptions", {}, None, "")
        button = session.calls[-1].reply_markup.inline_keyboard[0][0]
        assert "Купленный тариф" in button.text
        assert "(остал" in button.text
        assert button.icon_custom_emoji_id == "91001"
        await app.dispatcher.feed_update(app.bot, callback_update(button.callback_data))
        assert "Купленный тариф" in session.calls[-1].text
        assert 'emoji-id="91001"' in session.calls[-1].text

    asyncio.run(run())


@pytest.mark.parametrize(
    "status,url",
    [
        ("awaiting_payment", None),
        ("awaiting_payment", "https://example.test/pay"),
        ("paid_provisioning_pending", None),
    ],
)
def test_order_has_automatic_update_copy_and_no_manual_refresh(status, url):
    async def run():
        app, service, session = setup()
        service.order = lambda aid, oid: {
            "id": oid,
            "status": status,
            "total": "10.00",
            "currency": "RUB",
            "checkout_url": url,
            "environment": "scoped_live",
        }
        await app.execute(UID, AID, "order", {"order_id": "owned-order"}, None, "")
        message = session.calls[-1]
        assert "Обновить статус" not in message.text
        assert all(
            "Обновить статус" not in b.text for row in message.reply_markup.inline_keyboard for b in row
        )
        assert find_button(session, "Мои подписки")
        assert not any(call[0] in {"quote", "create"} for call in service.calls)

    asyncio.run(run())
