import asyncio

from test_dispatcher import setup


def test_return_from_skipped_sources_rechecks_balance_and_restores_source_navigation():
    async def run():
        app, service, session = setup()
        dashboard = service.dashboard
        service.dashboard = lambda aid: dashboard(aid) | {"bonus_balance": "0.00"}
        await app.execute("101", "9223372036854775700", "bonus_choice", checkout_data(), None, "")
        back = next(
            button
            for row in session.calls[-1].reply_markup.inline_keyboard
            for button in row
            if "Назад" in button.text
        )
        item = app.actions[back.callback_data]
        assert item.action == "periods"
        service.dashboard = dashboard
        await app.execute("101", "9223372036854775700", "bonus_choice", item.data, None, "")
        choice = next(
            button
            for row in session.calls[-1].reply_markup.inline_keyboard
            for button in row
            if "Без бонусов" in button.text
        )
        selected = app.actions[choice.callback_data]
        await app.execute("101", "9223372036854775700", selected.action, selected.data, None, "")
        back = next(
            button
            for row in session.calls[-1].reply_markup.inline_keyboard
            for button in row
            if "Назад" in button.text
        )
        assert app.actions[back.callback_data].action == "bonus_choice"

    asyncio.run(run())


def checkout_data():
    return {"product_id": "standard", "period_days": 30, "intent": "purchase"}


def test_no_bonus_or_credit_skips_source_question_and_uses_server_quote():
    async def run():
        app, service, session = setup()
        dashboard = service.dashboard
        service.dashboard = lambda aid: dashboard(aid) | {"bonus_balance": "0.00"}
        await app.execute("101", "9223372036854775700", "bonus_choice", checkout_data(), None, "")
        assert "Проверьте заказ" in session.calls[-1].text
        assert "Использовать допустимые бонусы" not in str(session.calls[-1].reply_markup)
        assert [call for call in service.calls if call[0] == "quote"][-1][2]["bonus_amount"] == "0.00"

    asyncio.run(run())


def test_no_bonus_preserves_credit_choice_without_bonus_buttons():
    async def run():
        app, service, session = setup()
        dashboard = service.dashboard
        service.dashboard = lambda aid: (
            dashboard(aid) | {"bonus_balance": "0.00", "credit_currency": "RUB", "credit_available": "100.00"}
        )
        await app.execute("101", "9223372036854775700", "bonus_choice", checkout_data(), None, "")
        labels = [button.text for row in session.calls[-1].reply_markup.inline_keyboard for button in row]
        assert any("Использовать денежный остаток" in label for label in labels)
        assert not any(
            "Бонусы и" in label or "допустимые бонусы" in label or "Без бонусов" in label for label in labels
        )

    asyncio.run(run())


def test_quote_dates_are_readable_moscow_including_day_rollover():
    async def run():
        app, service, session = setup()
        quote = service.quote
        service.quote = lambda aid, **kwargs: (
            quote(aid, **kwargs)
            | {
                "resulting_expires_at": "2026-11-06T23:15:45.283623+00:00",
                "expires_at": "2026-10-08T10:00:00Z",
            }
        )
        await app.execute("101", "9223372036854775700", "quote", checkout_data(), None, "")
        text = session.calls[-1].text
        assert "07.11.2026 02:15 МСК" in text
        assert "08.10.2026 13:00 МСК" in text
        assert "2026-11-06T" not in text

    asyncio.run(run())
