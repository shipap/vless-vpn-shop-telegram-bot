import asyncio

from aiogram import Bot
from aiogram.methods import AnswerCallbackQuery
from aiogram.types import Update
from sunrise_bot.adapter import BotApplication
from sunrise_bot.safety import BotSafety
from test_presentation import TOKEN, FakeSession


class FakeCommerce:
    def __init__(self):
        self.calls = []
        self.orders = {}
        self.enabled = False
        self.accepted = True
        self.documents = [
            {"document_type": "offer", "version": "v1", "url": "https://example.test/offer"},
            {"document_type": "privacy", "version": "v1", "url": "https://example.test/privacy"},
        ]
        self.subscriptions = [
            {
                "id": "9007199254740997",
                "product_id": "standard",
                "name": "Мой <VPN>",
                "status": "active",
                "expires_at": "2027-01-01T00:00:00Z",
                "devices_limit": 4,
                "version": 1,
                "connection_url": "https://vpn.example.test/synthetic",
                "devices": [{"id": "d1", "name": "Ноутбук", "last_seen_at": None}],
                "segments": [],
                "recurring": {"status": "active"},
            }
        ]

    def legal_status(self, account_id):
        from copy import deepcopy

        return {"configured": True, "documents": deepcopy(self.documents), "accepted": self.accepted}

    def accept_legal(self, account_id, documents, source, context):
        assert documents == self.documents
        assert source == "telegram" and context == "first_use"
        self.accepted = True
        return self.legal_status(account_id)

    def resolve_telegram(self, telegram_id, *, username=None):
        self.calls.append(("resolve", str(telegram_id)))
        return {
            "id": "9223372036854775700",
            "telegram_id": str(telegram_id),
            "email": None,
            "email_verified": False,
        }

    def catalog(self):
        from sunrise_infrastructure.service_content import help_center

        return {
            "content": {"help_center": help_center(None, {})},
            "products": [
                {
                    "id": "standard",
                    "code": "standard",
                    "tier_level": 1,
                    "name": "Стандартный",
                    "description": "Приватный доступ",
                    "price": "99.00",
                    "currency": "RUB",
                    "included_devices": 4,
                    "available": True,
                    "extra_device_price": "50.00",
                },
                {
                    "id": "universal",
                    "code": "universal",
                    "tier_level": 2,
                    "name": "Универсальный",
                    "description": "Дополнительные способы подключения",
                    "price": "249.00",
                    "currency": "RUB",
                    "included_devices": 4,
                    "available": True,
                    "extra_device_price": "50.00",
                },
            ],
            "periods": [{"days": 30, "months": 1, "discount_percent": "0"}],
            "recurring_enrollment_enabled": self.enabled,
            "capabilities": {"recurring": False},
        }

    def dashboard(self, account_id, *, refresh_panel=False, refresh_subscription_id=None):
        self.calls.append(("dashboard", str(account_id)))
        return {
            "account": {"id": str(account_id), "email": None},
            "subscriptions": self.subscriptions,
            "payments": [],
            "bonus_balance": "20.00",
            "recurring_enrollment_enabled": self.enabled,
        }

    def connection_instructions(self, account_id, sid):
        return {"source": "fallback", "platforms": [], "notice": "Используйте сервис по оферте."}

    def promo_claim_targets(self, account_id):
        return [
            sub["id"] for sub in self.subscriptions if sub["status"] in {"active", "provisioning_pending"}
        ]

    def quote(self, account_id, **kwargs):
        self.calls.append(("quote", str(account_id), kwargs))
        return {
            "id": "q1",
            "action": kwargs["action"],
            "product_id": kwargs["product_id"],
            "subscription_id": kwargs.get("subscription_id"),
            "base_amount": "75.00",
            "discount_amount": "0.00",
            "bonus_amount": kwargs.get("bonus_amount", "0.00"),
            "total": "75.00",
            "currency": "RUB",
            "resulting_expires_at": "2027-01-01T00:00:00Z",
            "devices_limit": 4,
            "breakdown": {},
            "expires_at": "2026-10-07T20:00:00Z",
            "version": 1,
        }

    def create_order(self, account_id, quote_id, idempotency_key, source):
        self.calls.append(("create", str(account_id), quote_id, idempotency_key, source))
        self.orders.setdefault(
            idempotency_key,
            {
                "id": "o1",
                "status": "paid_provisioning_pending",
                "total": "75.00",
                "currency": "RUB",
                "checkout_url": None,
                "environment": "local_test",
            },
        )
        return self.orders[idempotency_key]

    def order(self, account_id, order_id):
        return next(iter(self.orders.values()))

    def revoke_device(self, account_id, sid, device_id):
        self.calls.append(("revoke", str(account_id), sid, device_id))

    def cancel_agreement(self, account_id, sid):
        self.calls.append(("cancel", str(account_id), sid))
        return {"status": "cancel_pending"}

    def enroll(self, account_id, sid):
        self.calls.append(("enroll", str(account_id), sid))
        raise ValueError("disabled")


def message_update(uid=101, text="/start"):
    return Update.model_validate(
        {
            "update_id": 1,
            "message": {
                "message_id": 10,
                "date": 1791384304,
                "chat": {"id": uid, "type": "private"},
                "from": {"id": uid, "is_bot": False, "first_name": "Тест"},
                "text": text,
            },
        }
    )


def callback_update(data, uid=101):
    return Update.model_validate(
        {
            "update_id": 2,
            "callback_query": {
                "id": "cb-synthetic",
                "from": {"id": uid, "is_bot": False, "first_name": "Тест"},
                "chat_instance": "synthetic",
                "data": data,
                "message": {
                    "message_id": 1,
                    "date": 1791384304,
                    "chat": {"id": uid, "type": "private"},
                    "text": "Меню",
                },
            },
        }
    )


def find_button(session, label):
    for call in reversed(session.calls):
        if getattr(call, "reply_markup", None):
            for row in call.reply_markup.inline_keyboard:
                for button in row:
                    if label in button.text:
                        return button.callback_data
    raise AssertionError(f"Кнопка не найдена: {label}")


def setup():
    service = FakeCommerce()
    session = FakeSession()
    app = BotApplication(
        Bot(TOKEN, session=session), service, BotSafety(frozenset({"700001"}), frozenset({"101", "102"}))
    )
    return app, service, session


def test_real_dispatcher_resolves_canonical_id_and_single_subscription_directly():
    async def run():
        app, service, session = setup()
        await app.dispatcher.feed_update(app.bot, message_update())
        await app.dispatcher.feed_update(app.bot, callback_update(find_button(session, "Подписки")))
        assert ("dashboard", "9223372036854775700") in service.calls
        assert "Мой &lt;VPN&gt;" in session.calls[-1].text
        assert find_button(session, "Подключить подписку")
        assert find_button(session, "Главное меню")

    asyncio.run(run())


def test_foreign_callback_and_unknown_recipient_cannot_mutate():
    async def run():
        app, service, session = setup()
        await app.dispatcher.feed_update(app.bot, message_update())
        token = find_button(session, "Подписки")
        before = len(service.calls)
        await app.dispatcher.feed_update(app.bot, callback_update(token, uid=102))
        assert len(service.calls) == before
        assert any(isinstance(call, AnswerCallbackQuery) for call in session.calls)
        await app.dispatcher.feed_update(app.bot, message_update(uid=303))
        assert len(service.calls) == before

    asyncio.run(run())


def test_stale_callback_answers_spinner_and_returns_fresh_menu():
    async def run():
        app, _, session = setup()
        await app.dispatcher.feed_update(app.bot, callback_update("s:expired"))
        assert isinstance(session.calls[0], AnswerCallbackQuery)
        assert "Меню" in session.calls[-1].text

    asyncio.run(run())


def test_off_hides_enrollment_but_keeps_existing_cancellation():
    async def run():
        app, service, session = setup()
        await app.dispatcher.feed_update(app.bot, message_update())
        await app.dispatcher.feed_update(app.bot, callback_update(find_button(session, "Подписки")))
        token = find_button(session, "Отключить автопродление")
        assert all(
            "Включить автопродление" not in b.text
            for row in session.calls[-1].reply_markup.inline_keyboard
            for b in row
        )
        await app.dispatcher.feed_update(app.bot, callback_update(token))
        await app.dispatcher.feed_update(app.bot, callback_update(find_button(session, "Подтвердить отмену")))
        assert ("cancel", "9223372036854775700", "9007199254740997") in service.calls
        assert "запрос" in session.calls[-1].text.lower()

    asyncio.run(run())


def test_upgrade_quote_and_duplicate_checkout_use_same_shared_order_key():
    async def run():
        app, service, session = setup()
        await app.dispatcher.feed_update(app.bot, message_update())
        for label in ("Подписки", "Повысить тариф", "Универсальный", "Без бонусов"):
            await app.dispatcher.feed_update(app.bot, callback_update(find_button(session, label)))
        token = find_button(session, "Создать заказ")
        await app.dispatcher.feed_update(app.bot, callback_update(token))
        await app.dispatcher.feed_update(app.bot, callback_update(token))
        quotes = [call for call in service.calls if call[0] == "quote"]
        assert quotes[-1][2]["subscription_id"] == "9007199254740997"
        assert quotes[-1][2]["action"] == "upgrade"
        assert len(service.orders) == 1
        assert "Оплата получена" in session.calls[-1].text
        assert all(call[0] != "test-pay" for call in service.calls)

    asyncio.run(run())


def test_link_proof_requires_explicit_merge_confirmation_and_exact_canonical_owner():
    class FakeAccounts:
        def __init__(self):
            self.calls = []

        def confirm_telegram_link(self, account_id, proof):
            self.calls.append((str(account_id), proof))
            return {"id": "9007199254740999"}

    async def run():
        app, _, session = setup()
        accounts = FakeAccounts()
        app.accounts = accounts
        await app.dispatcher.feed_update(app.bot, message_update(text="/link synthetic-proof.secret"))
        assert not accounts.calls
        token = find_button(session, "Объединить аккаунты")
        await app.dispatcher.feed_update(app.bot, callback_update(token, uid=102))
        assert not accounts.calls
        await app.dispatcher.feed_update(app.bot, callback_update(token))
        assert accounts.calls == [("9223372036854775700", "synthetic-proof.secret")]
        assert "связаны" in session.calls[-1].text

    asyncio.run(run())


def test_editable_product_semantic_emoji_and_text_use_same_nodes():
    async def run():
        app, service, session = setup()
        app.presenter.custom_emoji = True
        original = service.catalog

        def catalog():
            value = original()
            value["products"][0]["content"] = {
                "name": [
                    {"type": "emoji", "id": "5368324170671202286", "fallback": "☀️", "label": "Солнце"},
                    {"type": "text", "text": "Новый <тариф>"},
                ],
                "description": [{"type": "bold", "children": [{"type": "text", "text": "Описание &"}]}],
            }
            return value

        service.catalog = catalog
        await app.dispatcher.feed_update(app.bot, message_update())
        await app.dispatcher.feed_update(app.bot, callback_update(find_button(session, "Подписки")))
        await app.dispatcher.feed_update(app.bot, callback_update(find_button(session, "Добавить подписку")))
        assert "<b>Описание &amp;</b>" not in session.calls[-1].text
        selected = find_button(session, "Новый <тариф>")
        await app.dispatcher.feed_update(app.bot, callback_update(selected))
        assert 'emoji-id="5368324170671202286"' in session.calls[-1].text
        assert "Новый &lt;тариф&gt;" in session.calls[-1].text
        assert "<b>Описание &amp;</b>" in session.calls[-1].text

    asyncio.run(run())


def test_cash_source_passes_server_available_decimal_to_shared_quote():
    async def run():
        app, service, session = setup()
        original_dashboard, original_quote = service.dashboard, service.quote

        def dashboard(account_id, **kwargs):
            return original_dashboard(account_id, **kwargs) | {
                "credit_balance": "150.005678",
                "credit_available": "150.00",
                "credit_currency": "RUB",
                "credit_provenance": "synthetic_test",
            }

        def quote(account_id, **kwargs):
            return original_quote(account_id, **kwargs) | {"credit_amount": "75.00", "total": "0.00"}

        service.dashboard, service.quote = dashboard, quote
        await app.dispatcher.feed_update(app.bot, message_update())
        for label in (
            "Подписки",
            "Добавить подписку",
            "Стандартный",
            "30 дней",
            "Использовать денежный остаток",
        ):
            await app.dispatcher.feed_update(app.bot, callback_update(find_button(session, label)))
        call = [call for call in service.calls if call[0] == "quote"][-1]
        assert call[1] == "9223372036854775700"
        assert call[2]["credit_amount"] == "150.00" and call[2]["bonus_amount"] == "0.00"
        assert "<b>Денежный остаток: </b>75,00 ₽" in session.calls[-1].text
        assert "<b>Итого: </b>0,00 ₽" in session.calls[-1].text
        assert not any(call[0] == "create" for call in service.calls)

    asyncio.run(run())


def test_unknown_cash_currency_is_visible_without_redemption_button():
    async def run():
        app, service, session = setup()
        original = service.dashboard
        service.dashboard = lambda account_id, **kwargs: (
            original(account_id, **kwargs)
            | {
                "credit_balance": "99.00",
                "credit_available": "0.00",
                "credit_currency": None,
            }
        )
        await app.dispatcher.feed_update(app.bot, message_update())
        for label in ("Подписки", "Добавить подписку", "Стандартный", "30 дней"):
            await app.dispatcher.feed_update(app.bot, callback_update(find_button(session, label)))
        assert "валюты" in session.calls[-1].text
        labels = [button.text for row in session.calls[-1].reply_markup.inline_keyboard for button in row]
        assert not any("Использовать денежный" in label for label in labels)
        assert find_button(session, "Без бонусов")

    asyncio.run(run())
