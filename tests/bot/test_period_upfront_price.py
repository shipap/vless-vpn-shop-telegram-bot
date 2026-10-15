"""Authoritative period prices and safe navigation with synthetic bot transport."""

import asyncio

import pytest
from sunrise_bot.adapter import money
from test_dispatcher import callback_update, find_button, message_update, setup


def priced_catalog(service, currency="RUB"):
    original = service.catalog
    periods = [
        {"days": 30, "months": 1, "discount_percent": "0", "price": "99.00"},
        {"days": 90, "months": 3, "discount_percent": "10", "price": "731.27"},
        {"days": 180, "months": 6, "discount_percent": "15", "price": "504.90"},
        {"days": 365, "months": 12, "discount_percent": "20", "price": "950.40"},
    ]

    def catalog():
        value = original()
        value["products"][0]["currency"] = currency
        value["products"][0]["periods"] = periods
        return value

    service.catalog = catalog
    return periods


@pytest.mark.parametrize("currency", ["RUB", "EUR"])
def test_buttons_show_exact_server_period_price_currency_and_unchanged_callbacks(currency):
    async def run():
        app, service, session = setup()
        periods = priced_catalog(service, currency)
        await app.execute(
            "101",
            "9223372036854775700",
            "periods",
            {"intent": "purchase", "product_id": "standard"},
            None,
            "",
        )
        buttons = [
            button
            for row in session.calls[-1].reply_markup.inline_keyboard
            for button in row
            if app.actions[button.callback_data].action == "bonus_choice"
        ]
        assert len(buttons) == 4
        for button, period in zip(buttons, periods, strict=True):
            assert money(period["price"], currency) in button.text
            assert f"{period['days']} дней" in button.text
            assert app.actions[button.callback_data].data["period_days"] == period["days"]
            assert not {"price", "currency", "period_price"} & app.actions[button.callback_data].data.keys()
        assert "Дополнительные скидки" in session.calls[-1].text
        assert "бонусы" in session.calls[-1].text and "перед подтверждением" in session.calls[-1].text
        assert not any(call[0] in {"quote", "create"} for call in service.calls)

    asyncio.run(run())


@pytest.mark.parametrize("price", [None, True, "NaN", "-1", "bad"])
def test_unknown_or_invalid_price_is_never_inferred_from_monthly_product(price):
    async def run():
        app, service, session = setup()
        periods = priced_catalog(service)
        periods[0]["price"] = price
        await app.execute(
            "101",
            "9223372036854775700",
            "periods",
            {"intent": "purchase", "product_id": "standard"},
            None,
            "",
        )
        button = next(
            button
            for row in session.calls[-1].reply_markup.inline_keyboard
            for button in row
            if app.actions[button.callback_data].action == "bonus_choice"
        )
        assert "цена уточняется в расчёте" in button.text
        assert "99,00" not in button.text
        assert "NaN" not in button.text
        assert not any(call[0] in {"quote", "create"} for call in service.calls)

    asyncio.run(run())


def test_real_callbacks_keep_prices_on_back_and_never_start_payment():
    async def run():
        app, service, session = setup()
        priced_catalog(service)
        await app.dispatcher.feed_update(app.bot, message_update())
        for label in ["Каталог", "Стандартный", "90 дней"]:
            await app.dispatcher.feed_update(app.bot, callback_update(find_button(session, label)))
        assert "731,27 ₽" not in session.calls[-1].text  # source choice has no invented price arithmetic
        await app.dispatcher.feed_update(app.bot, callback_update(find_button(session, "Назад")))
        assert "731,27 ₽" in next(
            button.text
            for row in session.calls[-1].reply_markup.inline_keyboard
            for button in row
            if "90 дней" in button.text
        )
        await app.dispatcher.feed_update(app.bot, callback_update(find_button(session, "Назад")))
        assert "Выберите тариф" in session.calls[-1].text
        await app.dispatcher.feed_update(app.bot, callback_update(find_button(session, "Главное меню")))
        assert "Меню" in session.calls[-1].text
        assert not any(call[0] in {"quote", "create"} for call in service.calls)

    asyncio.run(run())


@pytest.mark.parametrize("state", ["missing", "disabled", "hidden"])
def test_old_product_button_never_opens_periods_for_unavailable_lot(state):
    async def run():
        app, service, session = setup()
        priced_catalog(service)
        await app.dispatcher.feed_update(app.bot, message_update())
        await app.dispatcher.feed_update(app.bot, callback_update(find_button(session, "Каталог")))
        token = find_button(session, "Стандартный")
        original = service.catalog

        def current_catalog():
            value = original()
            if state == "missing":
                value["products"] = []
            else:
                value["products"][0]["available" if state == "disabled" else "visible_bot"] = False
            return value

        service.catalog = current_catalog
        await app.dispatcher.feed_update(app.bot, callback_update(token))
        assert "Тариф недоступен" in session.calls[-1].text
        actions = [
            app.actions[button.callback_data].action
            for row in session.calls[-1].reply_markup.inline_keyboard
            for button in row
        ]
        assert "products" in actions and "bonus_choice" not in actions
        assert not any(call[0] in {"quote", "create"} for call in service.calls)

    asyncio.run(run())
