"""Owner catalog content and Telegram presentation, with synthetic transport only."""

import asyncio

import pytest
from test_dispatcher import setup
from test_period_upfront_price import priced_catalog


def test_periods_keep_two_columns_and_same_configured_icon():
    async def run():
        app, service, session = setup()
        app.presenter.custom_emoji = True
        priced_catalog(service)
        theme = {
            "icons": {
                "period_select": {"fallback": "📅", "custom_emoji_id": "91001"},
                "period_select_2": {"fallback": "⭐", "custom_emoji_id": "91002"},
            }
        }
        original = service.catalog
        service.catalog = lambda: original() | {"content": {"telegram_theme": theme}}
        await app.execute(
            "101",
            "9223372036854775700",
            "periods",
            {"intent": "purchase", "product_id": "standard"},
            None,
            "",
        )
        rows = session.calls[-1].reply_markup.inline_keyboard
        assert [len(row) for row in rows] == [2, 2, 2]
        assert [[app.actions[b.callback_data].data["period_days"] for b in row] for row in rows[:2]] == [
            [30, 90],
            [180, 365],
        ]
        assert [b.icon_custom_emoji_id for row in rows[:2] for b in row] == ["91001"] * 4
        assert [app.actions[b.callback_data].action for b in rows[-1]] == ["products", "main"]

    asyncio.run(run())


def test_owner_catalog_introduction_has_no_generated_product_text():
    async def run():
        app, service, session = setup()
        original = service.catalog

        def catalog():
            value = original()
            value["content"]["catalog_description"] = "<b>Авторский каталог</b>"
            return value

        service.catalog = catalog
        await app.execute("101", "9223372036854775700", "products", {"intent": "purchase"}, None, "")
        message = session.calls[-1]
        assert message.text == "<b>Авторский каталог</b>"
        assert len(message.reply_markup.inline_keyboard) == 3

    asyncio.run(run())


def test_product_name_and_description_keep_authored_repeated_custom_emoji():
    async def run():
        app, service, session = setup()
        app.presenter.custom_emoji = True
        original = service.catalog

        def catalog():
            value = original()
            product = value["products"][0]
            product["name"] = '<tg-emoji emoji-id="91001">⭐</tg-emoji> <b>Имя</b>'
            product["description"] = '<tg-emoji emoji-id="91001">⭐</tg-emoji> <b>Описание</b>'
            return value

        service.catalog = catalog
        await app.execute("101", "9223372036854775700", "products", {"intent": "purchase"}, None, "")
        button = session.calls[-1].reply_markup.inline_keyboard[0][0]
        assert button.icon_custom_emoji_id == "91001"
        assert button.text == "Имя"
        await app.execute(
            "101",
            "9223372036854775700",
            "periods",
            {"intent": "purchase", "product_id": "standard"},
            None,
            "",
        )
        text = session.calls[-1].text
        assert text.count('<tg-emoji emoji-id="91001">⭐</tg-emoji>') == 2
        assert "<b>Имя</b>" in text and "<b>Описание</b>" in text

    asyncio.run(run())


@pytest.mark.parametrize("environment", ["sandbox", "scoped_live"])
def test_payable_order_retains_green_configured_payment_button(environment):
    async def run():
        app, service, session = setup()
        app.presenter.custom_emoji = True
        theme = {
            "icons": {
                "checkout": {"fallback": "✅", "custom_emoji_id": "91001"},
                "order_heading": {"fallback": "✅", "custom_emoji_id": "91001"},
            }
        }
        original = service.catalog
        service.catalog = lambda: original() | {"content": {"telegram_theme": theme}}
        service.order = lambda aid, order_id: {
            "id": "o1",
            "status": "awaiting_payment",
            "total": "10.00",
            "currency": "RUB",
            "checkout_url": "https://example.test/pay",
            "environment": environment,
            "payment_expires_at": "2026-10-13T12:00:00Z",
        }
        await app.execute("101", "9223372036854775700", "order", {"order_id": "o1"}, None, "")
        button = session.calls[-1].reply_markup.inline_keyboard[0][0]
        assert button.url == "https://example.test/pay"
        assert button.style == "success" and button.icon_custom_emoji_id == "91001"
        assert "Оплатить до" in session.calls[-1].text
        assert "13.10.2026 15:00" in session.calls[-1].text
        assert "локальный тестовый заказ" not in session.calls[-1].text
        if environment == "scoped_live":
            assert "Обновить статус" not in session.calls[-1].text
            assert "Мы сообщим" in session.calls[-1].text

    asyncio.run(run())


@pytest.mark.parametrize("pending_status", ["awaiting_payment", "paid_provisioning_pending"])
def test_scoped_order_reentry_reads_same_order_without_manual_refresh_or_new_order(pending_status):
    async def run():
        app, service, session = setup()
        reads = []
        state = {
            "id": "owned-order",
            "status": pending_status,
            "environment": "scoped_live",
            "total": "10.00",
            "currency": "RUB",
            "checkout_url": None,
        }

        def order(account_id, order_id):
            reads.append((account_id, order_id))
            return dict(state)

        service.order = order
        await app.execute("101", "9223372036854775700", "order", {"order_id": "owned-order"}, None, "")
        message = session.calls[-1]
        assert "Обновить статус" not in message.text
        assert all(
            "Обновить статус" not in b.text for row in message.reply_markup.inline_keyboard for b in row
        )
        state.update(status="paid", provisioning_status="ready", subscription_id="owned-sub")
        await app.execute("101", "9223372036854775700", "order", {"order_id": "owned-order"}, 5, "")
        assert reads == [("9223372036854775700", "owned-order")] * 2
        assert not any(call[0] in {"quote", "create"} for call in service.calls)
        assert "Оплата получена" in session.calls[-1].text
        assert not any(
            "Обновить статус" in b.text for row in session.calls[-1].reply_markup.inline_keyboard for b in row
        )

    asyncio.run(run())


@pytest.mark.parametrize("custom_emoji", [False, True])
@pytest.mark.parametrize("explicit_icon", [False, True])
def test_lot_button_uses_first_authored_premium_emoji_unless_explicit_icon(custom_emoji, explicit_icon):
    async def run():
        app, service, session = setup()
        app.presenter.custom_emoji = custom_emoji
        original = service.catalog

        def catalog():
            value = original()
            product = value["products"][0]
            product["name"] = (
                '<b><tg-emoji emoji-id="91001">⬛</tg-emoji> Стандарт</b> <tg-emoji emoji-id="91002">⭐</tg-emoji>'
            )
            if explicit_icon:
                product["icon"] = {"fallback": "✅", "custom_emoji_id": "91003"}
            return value

        service.catalog = catalog
        await app.execute("101", "9223372036854775700", "products", {"intent": "purchase"}, None, "")
        button = session.calls[-1].reply_markup.inline_keyboard[0][0]
        assert button.icon_custom_emoji_id == (
            ("91003" if explicit_icon else "91001") if custom_emoji else None
        )
        assert "Стандарт" in button.text and "⭐" in button.text
        assert "tg-emoji" not in button.text and "<b>" not in button.text
        if not explicit_icon:
            assert button.text == ("Стандарт ⭐" if custom_emoji else "⬛ Стандарт ⭐")
        await app.execute(
            "101",
            "9223372036854775700",
            "periods",
            {"intent": "purchase", "product_id": "standard"},
            None,
            "",
        )
        if custom_emoji:
            assert 'emoji-id="91001"' in session.calls[-1].text
            assert 'emoji-id="91002"' in session.calls[-1].text

    asyncio.run(run())


def test_quote_itemizes_actual_promo_other_and_payment_discounts():
    async def run():
        app, service, session = setup()
        original = service.quote
        service.quote = lambda aid, **kwargs: (
            original(aid, **kwargs)
            | {
                "discount_amount": "22.00",
                "period_discount_amount": "10.00",
                "secondary_discount_amount": "2.00",
                "promo_discount_amount": "10.00",
                "promo_code": "OWNER",
                "payment_method_discount_amount": "3.00",
                "payment_method": {"id": "crypto", "label": "Криптовалюта", "discount_percent": "5"},
            }
        )
        await app.execute(
            "101",
            "9223372036854775700",
            "quote",
            {"intent": "purchase", "product_id": "standard", "period_days": 30},
            None,
            "",
        )
        text = session.calls[-1].text
        assert "Промокод OWNER" in text and "10,00 ₽" in text
        assert "Скидка за период" in text and "Другие скидки" in text and "2,00 ₽" in text
        assert "Скидка за способ оплаты: 3,00 ₽" in text

    asyncio.run(run())
