import asyncio

from aiogram.methods import AnswerCallbackQuery
from test_dispatcher import callback_update, find_button, setup

METHODS = [
    {
        "id": "crypto",
        "label": "Криптовалюта",
        "discount_percent": "7.5",
        "available": True,
        "unavailable_reason": None,
    },
    {
        "id": "xrocket",
        "label": "xRocket",
        "discount_percent": "3",
        "available": False,
        "unavailable_reason": "Временно недоступен",
    },
    {"id": "sbp", "label": "СБП", "discount_percent": "0", "available": True, "unavailable_reason": None},
]


def test_bot_requires_method_before_quote_and_uses_authoritative_discounted_total():
    async def run():
        app, service, session = setup()
        catalog = service.catalog
        service.catalog = lambda: catalog() | {"payment_methods": METHODS}
        original_quote = service.quote
        service.quote = lambda aid, **kwargs: (
            original_quote(aid, **kwargs)
            | {"total": "183.17", "payment_method": METHODS[0], "payment_method_discount_amount": "16.83"}
        )
        data = {"product_id": "standard", "period_days": 30, "intent": "purchase"}
        await app.execute("101", "9223372036854775700", "quote", data, None, "")
        assert not [call for call in service.calls if call[0] == "quote"]
        assert "Криптовалюта (−7,5%)" in str(session.calls[-1].reply_markup)
        assert "Временно недоступен" in session.calls[-1].text
        buttons = [button for row in session.calls[-1].reply_markup.inline_keyboard for button in row]
        assert not any("xRocket" in button.text for button in buttons)
        selected = next(
            app.actions[button.callback_data] for button in buttons if "Криптовалюта" in button.text
        )
        await app.execute("101", "9223372036854775700", selected.action, selected.data, None, "")
        call = [call for call in service.calls if call[0] == "quote"][-1]
        assert call[2]["payment_method"] == "crypto"
        assert "183,17" in session.calls[-1].text or "183.17" in session.calls[-1].text
        assert "Криптовалюта" in session.calls[-1].text

    asyncio.run(run())


def test_method_becoming_unavailable_and_empty_catalog_never_request_quote():
    async def run():
        app, service, session = setup()
        catalog = service.catalog
        service.catalog = lambda: catalog() | {"payment_methods": METHODS}
        data = {
            "product_id": "standard",
            "period_days": 30,
            "intent": "purchase",
            "payment_method": "xrocket",
        }
        await app.execute("101", "9223372036854775700", "quote", data, None, "")
        assert not [call for call in service.calls if call[0] == "quote"]
        service.catalog = lambda: catalog() | {"payment_methods": []}
        await app.execute("101", "9223372036854775700", "quote", data, None, "")
        assert not [call for call in service.calls if call[0] == "quote"]
        assert "Способы оплаты временно недоступны" in session.calls[-1].text

    asyncio.run(run())


def test_bot_method_change_requotes_same_product_period_and_bonus_source():
    async def run():
        app, service, session = setup()
        catalog = service.catalog
        service.catalog = lambda: catalog() | {"payment_methods": METHODS}
        data = {
            "product_id": "standard",
            "period_days": 30,
            "intent": "purchase",
            "payment_method": "crypto",
            "bonus_amount": "12.34",
        }
        await app.execute("101", "9223372036854775700", "quote", data, None, "")
        buttons = [button for row in session.calls[-1].reply_markup.inline_keyboard for button in row]
        change = next(
            app.actions[button.callback_data] for button in buttons if "Изменить способ оплаты" in button.text
        )
        await app.execute("101", "9223372036854775700", change.action, change.data, None, "")
        buttons = [button for row in session.calls[-1].reply_markup.inline_keyboard for button in row]
        selected = next(app.actions[button.callback_data] for button in buttons if "СБП" in button.text)
        await app.execute("101", "9223372036854775700", selected.action, selected.data, None, "")
        call = [call for call in service.calls if call[0] == "quote"][-1]
        assert call[2]["payment_method"] == "sbp"
        assert call[2]["product_id"] == "standard"
        assert call[2]["period_days"] == 30
        assert call[2]["bonus_amount"] == "12.34"

    asyncio.run(run())


def checkout_fixture():
    app, service, session = setup()
    catalog = service.catalog
    service.catalog = lambda: catalog() | {"payment_methods": METHODS}
    data = {"product_id": "standard", "period_days": 30, "intent": "purchase", "payment_method": "crypto"}
    return app, service, session, data


def test_old_confirm_cannot_create_order_after_failed_method_change_but_fresh_confirm_can():
    async def run():
        app, service, session, data = checkout_fixture()
        await app.execute("101", "9223372036854775700", "quote", data, None, "")
        old_confirm = find_button(session, "Создать заказ")
        await app.dispatcher.feed_update(
            app.bot, callback_update(find_button(session, "Изменить способ оплаты"))
        )
        quote = service.quote

        def failing_quote(aid, **kwargs):
            raise ValueError("synthetic quote failure")

        service.quote = failing_quote
        await app.dispatcher.feed_update(app.bot, callback_update(find_button(session, "СБП")))
        await app.dispatcher.feed_update(app.bot, callback_update(old_confirm))
        assert not [call for call in service.calls if call[0] == "create"]
        service.quote = quote
        await app.execute("101", "9223372036854775700", "quote", {**data, "payment_method": "sbp"}, None, "")
        fresh_confirm = find_button(session, "Создать заказ")
        await app.dispatcher.feed_update(app.bot, callback_update(fresh_confirm))
        assert len([call for call in service.calls if call[0] == "create"]) == 1
        # A created order remains reachable even after later checkout-generation changes.
        await app.execute("101", "9223372036854775700", "quote", data, None, "")
        await app.dispatcher.feed_update(app.bot, callback_update(fresh_confirm))
        assert len(service.orders) == 1
        assert len([call for call in service.calls if call[0] == "create"]) == 1
        assert "Оплата получена" in session.calls[-1].text

    asyncio.run(run())


def test_old_callback_queued_during_failed_requote_rechecks_generation_inside_lock():
    async def run():
        app, service, session, data = checkout_fixture()
        await app.execute("101", "9223372036854775700", "quote", data, None, "")
        old_confirm = find_button(session, "Создать заказ")
        await app.dispatcher.feed_update(
            app.bot, callback_update(find_button(session, "Изменить способ оплаты"))
        )
        quote_started, quote_release, old_answered = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original_call = app.call

        async def delayed_call(method, *args, **kwargs):
            if method == "quote" and kwargs.get("payment_method") == "sbp":
                quote_started.set()
                await quote_release.wait()
                raise ValueError("synthetic quote failure")
            return await original_call(method, *args, **kwargs)

        app.call = delayed_call
        requote = asyncio.create_task(
            app.dispatcher.feed_update(app.bot, callback_update(find_button(session, "СБП")))
        )
        await asyncio.wait_for(quote_started.wait(), timeout=2)
        original_request = session.make_request

        async def observe_answer(bot, method, timeout=None):
            result = await original_request(bot, method, timeout)
            if isinstance(method, AnswerCallbackQuery):
                old_answered.set()
            return result

        session.make_request = observe_answer
        old_callback = asyncio.create_task(app.dispatcher.feed_update(app.bot, callback_update(old_confirm)))
        await asyncio.wait_for(old_answered.wait(), timeout=2)
        assert app.locks["101"].locked()
        quote_release.set()
        await asyncio.wait_for(asyncio.gather(requote, old_callback), timeout=2)
        assert not [call for call in service.calls if call[0] == "create"]

    asyncio.run(run())
