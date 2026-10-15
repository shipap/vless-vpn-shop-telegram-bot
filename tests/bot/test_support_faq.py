import asyncio
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from sunrise_infrastructure.instance_config import InstanceConfig
from test_dispatcher import setup

FAQ_ICONS = {
    "help_payment_access": ("faq_payment_access", "order", "order_heading"),
    "help_vpn_connection": ("faq_vpn_connection", "connect", "connect_heading"),
    "help_device_limit": ("faq_device_limit", "devices", "devices_heading"),
    "help_email_code": ("faq_email_code", "profile_email", "profile_heading"),
    "help_cancel_recurring": ("faq_cancel_recurring", "cancel_confirm", "recurring_heading"),
    "help_refund_request": ("faq_refund_request", "quote_credit", "history_heading"),
}


def themed_faq_app(icons):
    app, service, session = setup()
    app.presenter.custom_emoji = True
    original = service.catalog
    topics = json.loads(
        (Path(__file__).resolve().parents[2] / "packages/web-ui/practical-faq.example.json").read_text(
            encoding="utf-8"
        )
    )

    def catalog():
        result = original()
        result["content"]["telegram_theme"] = {"icons": icons, "styles": {}}
        result["content"]["help_center"]["custom_faq"] = topics
        return result

    service.catalog = catalog
    return app, service, session, topics


@pytest.mark.parametrize("dedicated", [False, True])
def test_practical_faq_premium_buttons_and_answer_headings_follow_distinct_configured_semantics(dedicated):
    async def run():
        icons, expected_buttons, expected_headings = {}, {}, {}
        for number, (topic, (key, fallback, heading_fallback)) in enumerate(FAQ_ICONS.items()):
            button_id, heading_id = str(9000000000000000000 + number), str(9000000000000000100 + number)
            icons[key if dedicated else fallback] = {"fallback": "🔹", "custom_emoji_id": button_id}
            icons[key + "_heading" if dedicated else heading_fallback] = {
                "fallback": "🔸",
                "custom_emoji_id": heading_id,
            }
            expected_buttons[topic], expected_headings[topic] = button_id, heading_id
        app, _, session, topics = themed_faq_app(icons)
        await app.execute("101", "9223372036854775700", "help", {}, None, "")
        choices = [button for row in session.calls[-1].reply_markup.inline_keyboard for button in row]
        for topic in topics:
            button = next(button for button in choices if topic["question"] in button.text)
            assert button.icon_custom_emoji_id == expected_buttons[topic["id"]]
        assert (
            len(
                {
                    button.icon_custom_emoji_id
                    for button in choices
                    if any(topic["question"] in button.text for topic in topics)
                }
            )
            == 6
        )
        for topic in topics:
            await app.execute("101", "9223372036854775700", "faq_" + topic["id"], {}, None, "")
            assert f'emoji-id="{expected_headings[topic["id"]]}"' in session.calls[-1].text
            navigation = session.calls[-1].reply_markup.inline_keyboard[-1]
            assert len(navigation) == 2
            assert [app.actions[button.callback_data].action for button in navigation] == ["help", "main"]

    asyncio.run(run())


def test_practical_faq_without_premium_capability_uses_semantic_unicode_fallback():
    async def run():
        app, _, session, topics = themed_faq_app({})
        app.presenter.custom_emoji = False
        await app.execute("101", "9223372036854775700", "help", {}, None, "")
        choices = [button for row in session.calls[-1].reply_markup.inline_keyboard for button in row]
        labels = [
            next(button for button in choices if topic["question"] in button.text)
            .text.removesuffix(topic["question"])
            .strip()
            for topic in topics
        ]
        assert len(set(labels)) == 6 and all(labels)
        assert all(button.icon_custom_emoji_id is None for button in choices)

    asyncio.run(run())


def test_custom_owner_faq_keeps_generic_theme_and_back_main_navigation():
    async def run():
        app, service, session, _ = themed_faq_app(
            {
                "help": {"fallback": "💬", "custom_emoji_id": "9000000000000000200"},
                "help_heading": {"fallback": "🛟", "custom_emoji_id": "9000000000000000201"},
            }
        )
        original = service.catalog

        def catalog():
            result = original()
            result["content"]["help_center"]["custom_faq"] = [
                {
                    "id": "owner_custom",
                    "question": "Вопрос владельца",
                    "answer": [
                        {"type": "paragraph", "children": [{"type": "text", "text": "Настроенный ответ"}]}
                    ],
                }
            ]
            return result

        service.catalog = catalog
        await app.execute("101", "9223372036854775700", "help", {}, None, "")
        button = next(
            button
            for row in session.calls[-1].reply_markup.inline_keyboard
            for button in row
            if "Вопрос владельца" in button.text
        )
        assert button.icon_custom_emoji_id == "9000000000000000200"
        await app.execute("101", "9223372036854775700", "faq_owner_custom", {}, None, "")
        assert 'emoji-id="9000000000000000201"' in session.calls[-1].text
        assert "Настроенный ответ" in session.calls[-1].text
        navigation = session.calls[-1].reply_markup.inline_keyboard[-1]
        assert [app.actions[button.callback_data].action for button in navigation] == ["help", "main"]

    asyncio.run(run())


def test_subscription_support_button_opens_readonly_draft_without_connection_credentials():
    async def run():
        app, service, session = setup()
        app.instance = InstanceConfig(support_url="https://t.me/synthetic_support")
        await app.execute(
            "101", "9223372036854775700", "subscription", {"sid": service.subscriptions[0]["id"]}, None, ""
        )
        buttons = [button for row in session.calls[-1].reply_markup.inline_keyboard for button in row]
        support = next((button for button in buttons if "Поддержка по подписке" in button.text), None)
        assert support is not None
        draft = parse_qs(urlsplit(support.url).query)["text"][0]
        assert "9007199254740997" in draft and "Статус: Действует" in draft
        assert "vpn.example.test" not in draft
        assert not any(call[0] in {"cancel", "revoke", "create"} for call in service.calls)

    asyncio.run(run())


@pytest.mark.parametrize("order_id", ["9007199254740993123", None, "vpn://secret"])
def test_saved_payment_history_support_uses_selected_order_only_or_plain_safe_fallback(order_id):
    async def run():
        app, service, session = setup()
        base = "https://t.me/synthetic_support"
        app.instance = InstanceConfig(support_url=base)
        original = service.dashboard

        def dashboard(account_id, **kwargs):
            result = original(account_id)
            payment = {
                "id": "provider-secret-marker",
                "amount": "75.00",
                "currency": "RUB",
                "status": "paid",
                "paid_at": "2026-10-09T12:00:00Z",
                "fee": None,
            }
            result["payments"] = [
                payment | {"order_id": "unselected-order"},
                payment | {"order_id": order_id},
            ]
            return result

        service.dashboard = dashboard
        await app.execute("101", "9223372036854775700", "history", {"page": 1}, None, "")
        buttons = [button for row in session.calls[-1].reply_markup.inline_keyboard for button in row]
        support = next((button for button in buttons if "Поддержка по заказу" in button.text), None)
        assert support is not None
        if order_id == "9007199254740993123":
            draft = parse_qs(urlsplit(support.url).query)["text"][0]
            assert draft == "Нужна помощь по заказу.\nID: 9007199254740993123\nСтатус: Оплачено"
            assert "provider-secret-marker" not in draft and "unselected-order" not in draft
            assert "75.00" not in draft
        else:
            assert support.url == base
        assert not any(call[0] in {"cancel", "revoke", "create"} for call in service.calls)

    asyncio.run(run())


def test_saved_order_support_draft_uses_only_id_and_known_status():
    async def run():
        app, service, session = setup()
        app.instance = InstanceConfig(support_url="https://t.me/synthetic_support")
        service.create_order("9223372036854775700", "q1", "synthetic", "bot")
        await app.execute("101", "9223372036854775700", "order", {"order_id": "o1"}, None, "")
        buttons = [button for row in session.calls[-1].reply_markup.inline_keyboard for button in row]
        support = next((button for button in buttons if "Поддержка по заказу" in button.text), None)
        assert support is not None
        draft = parse_qs(urlsplit(support.url).query)["text"][0]
        assert "o1" in draft and "Статус: Оплачено, подключаем" in draft
        assert "75.00" not in draft

    asyncio.run(run())


def test_bot_displays_database_faq_with_structured_answer_and_action():
    async def run():
        app, service, session = setup()
        original = service.catalog

        def catalog():
            result = original()
            result["content"]["help_center"]["custom_faq"] = [
                {
                    "id": "help_payment_access",
                    "question": "Списали деньги, но доступа нет",
                    "answer": [
                        {
                            "type": "paragraph",
                            "children": [
                                {
                                    "type": "bold",
                                    "children": [{"type": "text", "text": "Не оплачивайте повторно."}],
                                }
                            ],
                        }
                    ],
                }
            ]
            return result

        service.catalog = catalog
        await app.execute("101", "9223372036854775700", "help", {}, None, "")
        choices = [button for row in session.calls[-1].reply_markup.inline_keyboard for button in row]
        assert any("Списали деньги" in button.text for button in choices)
        await app.execute("101", "9223372036854775700", "faq_help_payment_access", {}, None, "")
        assert "Не оплачивайте повторно." in session.calls[-1].text
        assert any(
            "Платежи" in button.text
            for row in session.calls[-1].reply_markup.inline_keyboard
            for button in row
        )

    asyncio.run(run())
