import asyncio
import re
from types import SimpleNamespace

import pytest
from test_dispatcher import callback_update, find_button, message_update, setup


def bind_verified_admin(app, service):
    settings = SimpleNamespace(admin_email_allowlist=["admin@example.test"], admin_telegram_allowlist=["101"])
    app.accounts = SimpleNamespace(settings=settings)
    original = service.resolve_telegram
    service.resolve_telegram = lambda uid, **kwargs: (
        original(uid, **kwargs) | {"email": "admin@example.test", "email_verified": True}
    )
    return settings


def test_telegram_admin_allowlist_rechecks_stale_callback_after_removal():
    async def run():
        app, service, session = setup()
        settings = bind_verified_admin(app, service)
        await app.main("101", {})
        token = find_button(session, "Админ-панель")
        settings.admin_telegram_allowlist = []
        await app.dispatcher.feed_update(app.bot, callback_update(token))
        assert "404" in session.calls[-1].text

    asyncio.run(run())


def test_free_promo_requires_explicit_target_selection_with_multiple_active_subscriptions():
    async def run():
        app, service, session = setup()
        service.subscriptions.append(
            service.subscriptions[0] | {"id": "9007199254740998", "name": "Другой VPN"}
        )
        service.active_promo = lambda aid: {
            "code": "GIFT",
            "state": "pending",
            "reward_type": "free_days",
            "reward_days": 7,
            "product_id": "standard",
        }
        claims = []

        def claim(aid, code, subscription_id=None):
            claims.append((aid, code, subscription_id))
            return {"status": "provisioning_pending", "subscription_id": subscription_id}

        service.claim_promo = claim
        await app.execute("101", "9223372036854775700", "promo", {}, None, "")
        await app.dispatcher.feed_update(app.bot, callback_update(find_button(session, "Получить доступ")))
        assert not claims
        await app.dispatcher.feed_update(app.bot, callback_update(find_button(session, "Другой VPN")))
        assert not claims
        await app.dispatcher.feed_update(app.bot, callback_update(find_button(session, "Подтвердить")))
        assert claims == [("9223372036854775700", "GIFT", "9007199254740998")]

    asyncio.run(run())


def test_admin_promo_wizard_infinity_requires_review_and_audited_shared_save():
    class FakeAdmin:
        def __init__(self):
            self.saved = []

        def bot_promos(self, aid):
            return []

        def save_bot_promo(self, aid, values, reason):
            self.saved.append((aid, dict(values), reason))
            return values

    async def run():
        app, service, session = setup()
        bind_verified_admin(app, service)
        app.admin = FakeAdmin()
        await app.execute("101", "9223372036854775700", "admin", {}, None, "")
        await app.dispatcher.feed_update(app.bot, callback_update(find_button(session, "Промокоды")))
        await app.dispatcher.feed_update(app.bot, callback_update(find_button(session, "Создать промокод")))
        await app.dispatcher.feed_update(app.bot, message_update(text="NEWYEAR"))
        await app.dispatcher.feed_update(app.bot, callback_update(find_button(session, "Скидка в рублях")))
        await app.dispatcher.feed_update(app.bot, message_update(text="120,50"))
        await app.dispatcher.feed_update(app.bot, callback_update(find_button(session, "Только новые")))
        await app.dispatcher.feed_update(app.bot, callback_update(find_button(session, "Без лимита")))
        assert app.admin_promos.drafts["101"].step == "review"
        assert "Причина" not in session.calls[-1].text
        assert "причину" not in session.calls[-1].text
        assert not app.admin.saved
        assert "120,50" in session.calls[-1].text
        assert "Один раз на аккаунт" in session.calls[-1].text
        await app.dispatcher.feed_update(app.bot, callback_update(find_button(session, "Сохранить промокод")))
        aid, values, reason = app.admin.saved[0]
        assert aid == "9223372036854775700"
        assert values["amount_rub"] == "120.50" and values["audience"] == "first_purchase"
        assert values["total_limit"] is None and values["per_account_limit"] == 1
        assert reason == "Создание промокода NEWYEAR через Telegram"

    asyncio.run(run())


def test_stale_admin_save_cannot_save_a_different_current_draft():
    async def run():
        app, service, session = setup()
        saved = []
        bind_verified_admin(app, service)
        app.admin = SimpleNamespace(
            bot_promos=lambda aid: [], save_bot_promo=lambda aid, values, reason: saved.append(dict(values))
        )

        async def prepare(code):
            await app.execute("101", "9223372036854775700", "admin_promo_create", {}, None, "")
            await app.dispatcher.feed_update(app.bot, message_update(text=code))
            await app.dispatcher.feed_update(
                app.bot, callback_update(find_button(session, "Скидка в рублях"))
            )
            await app.dispatcher.feed_update(app.bot, message_update(text="50"))
            await app.dispatcher.feed_update(app.bot, callback_update(find_button(session, "Все клиенты")))
            await app.dispatcher.feed_update(app.bot, callback_update(find_button(session, "Без лимита")))
            return find_button(session, "Сохранить промокод")

        old_save = await prepare("FIRST")
        current_save = await prepare("SECOND")
        await app.dispatcher.feed_update(app.bot, callback_update(old_save))
        assert saved == []
        assert app.admin_promos.drafts["101"].values["code"] == "SECOND"
        await app.dispatcher.feed_update(app.bot, callback_update(current_save))
        assert [value["code"] for value in saved] == ["SECOND"]

    asyncio.run(run())


@pytest.mark.parametrize("stage", ["type", "review"])
def test_stray_text_keeps_current_draft_buttons_usable(stage):
    async def run():
        app, service, session = setup()
        saved = []
        bind_verified_admin(app, service)
        app.admin = SimpleNamespace(
            bot_promos=lambda aid: [], save_bot_promo=lambda aid, values, reason: saved.append(dict(values))
        )
        await app.execute("101", "9223372036854775700", "admin_promo_create", {}, None, "")
        await app.dispatcher.feed_update(app.bot, message_update(text="STRAY"))
        type_button = find_button(session, "Скидка в рублях")
        if stage == "type":
            await app.dispatcher.feed_update(app.bot, message_update(text="Случайное сообщение"))
        await app.dispatcher.feed_update(app.bot, callback_update(type_button))
        assert app.admin_promos.drafts["101"].step == "value"
        await app.dispatcher.feed_update(app.bot, message_update(text="50"))
        await app.dispatcher.feed_update(app.bot, callback_update(find_button(session, "Все клиенты")))
        await app.dispatcher.feed_update(app.bot, callback_update(find_button(session, "Без лимита")))
        save_button = find_button(session, "Сохранить промокод")
        if stage == "review":
            await app.dispatcher.feed_update(app.bot, message_update(text="Случайное сообщение"))
        await app.dispatcher.feed_update(app.bot, callback_update(save_button))
        assert [value["code"] for value in saved] == ["STRAY"]

    asyncio.run(run())


def test_free_promo_targets_include_pending_and_follow_shared_eligibility_ids():
    async def run():
        app, service, session = setup()
        first = service.subscriptions[0]
        first["status"] = "provisioning_pending"
        first["name"] = "Первая подписка"
        second = first | {"id": "9007199254740998", "name": "Ожидающий VPN"}
        excluded = first | {"id": "9007199254740999", "name": "Истёкший VPN", "status": "expired"}
        service.subscriptions.extend([second, excluded])
        service.promo_claim_targets = lambda aid: [first["id"], second["id"]]
        service.active_promo = lambda aid: {
            "code": "FREE",
            "reward_type": "free_days",
            "reward_days": 7,
            "available": True,
        }
        await app.execute("101", "9223372036854775700", "promo_claim", {"code": "FREE"}, None, "")
        assert "Выберите подписку" in session.calls[-1].text
        assert find_button(session, "Ожидающий VPN")
        assert all(
            "Истёкший VPN" not in button.text
            for row in session.calls[-1].reply_markup.inline_keyboard
            for button in row
        )
        service.promo_claim_targets = lambda aid: [first["id"]]
        await app.execute("101", "9223372036854775700", "promo_claim", {"code": "FREE"}, None, "")
        assert first["name"] in session.calls[-1].text
        confirmation = app.actions[find_button(session, "Подтвердить")]
        assert confirmation.data["sid"] == first["id"]

    asyncio.run(run())


def test_catalog_primary_action_purchases_and_admin_has_separate_last_row():
    async def run():
        app, service, session = setup()
        app.accounts = SimpleNamespace(settings=SimpleNamespace(admin_telegram_allowlist=["101"]))
        service.account_info = lambda aid: {"id": aid, "telegram_id": "101"}
        await app.main("101", {"id": "9223372036854775700", "email_verified": False})
        rows = session.calls[-1].reply_markup.inline_keyboard
        assert [len(row) for row in rows] == [1, 2, 2, 2, 2, 1]
        assert find_button(session, "Уведомления")
        assert find_button(session, "Новости серверов")
        primary = rows[0][0]
        assert "Каталог" in primary.text
        assert app.actions[primary.callback_data].action == "products"
        assert app.actions[primary.callback_data].data["intent"] == "purchase"
        assert "Админ-панель" in rows[-1][0].text
        await app.dispatcher.feed_update(app.bot, callback_update(primary.callback_data))
        assert "99,00" not in session.calls[-1].text
        assert find_button(session, "Стандартный")

    asyncio.run(run())


def test_screen_uses_semantic_heading_back_main_and_preserves_configured_ids():
    async def run():
        app, _, session = setup()
        app.presenter.custom_emoji = True
        app.theme = {
            "icons": {
                "help_heading": {"custom_emoji_id": "91001", "fallback": "🛟"},
                "back": {"custom_emoji_id": "91002", "fallback": "⬅️"},
                "main": {"custom_emoji_id": "91003", "fallback": "🏠"},
                "help": {"custom_emoji_id": "91001", "fallback": "💬"},
            }
        }
        await app.show("101", "Помощь", "Инструкция", [("Поддержка", "help", {})])
        message = session.calls[-1]
        identifiers = re.findall(r'emoji-id="(\d+)"', message.text)
        identifiers += [
            b.icon_custom_emoji_id
            for row in message.reply_markup.inline_keyboard
            for b in row
            if b.icon_custom_emoji_id
        ]
        assert identifiers == ["91001", "91001", "91002", "91003"]
        assert message.reply_markup.inline_keyboard[0][0].text == "Поддержка"
        assert "☀" not in message.text

    asyncio.run(run())


def test_profile_displays_bonus_and_referral_and_keeps_email_plain_safe():
    async def run():
        app, service, session = setup()
        original = service.dashboard
        service.dashboard = lambda aid, **kwargs: (
            original(aid, **kwargs) | {"account": {"id": aid, "email": "<bad>&@example.test"}}
        )
        await app.execute("101", "9223372036854775700", "profile", {}, None, "")
        text = session.calls[-1].text
        assert "20,00 ₽" in text
        assert "&lt;bad&gt;&amp;@example.test" in text
        assert find_button(session, "Реферальная система")

    asyncio.run(run())


def test_standalone_promo_input_activates_for_account_without_creating_quote():
    async def run():
        app, service, session = setup()
        stored = {}
        service.active_promo = lambda aid: stored.get(aid)
        service.activate_promo = lambda aid, code: stored.setdefault(
            aid, {"code": code, "percent": "20", "state": "pending"}
        )
        await app.dispatcher.feed_update(app.bot, message_update())
        await app.dispatcher.feed_update(app.bot, callback_update(find_button(session, "Промокод")))
        await app.dispatcher.feed_update(app.bot, message_update(text="  OCTOBER  "))
        assert stored == {"9223372036854775700": {"code": "OCTOBER", "percent": "20", "state": "pending"}}
        assert "20%" in session.calls[-1].text
        assert not any(call[0] in {"quote", "create"} for call in service.calls)
        # The next screen reads durable account state, including after adapter restart.
        app2, _, session2 = setup()
        app2.commerce = service
        await app2.execute("101", "9223372036854775700", "promo", {}, None, "")
        assert "OCTOBER" in session2.calls[-1].text

    asyncio.run(run())


def test_promo_waiting_state_cannot_cross_account_change_or_navigation():
    async def run():
        app, service, session = setup()
        service.active_promo = lambda aid: None
        activations = []
        service.activate_promo = lambda *args: activations.append(args)
        await app.execute("101", "old-account", "promo", {}, None, "")
        await app.dispatcher.feed_update(app.bot, message_update(text="CROSS_ACCOUNT"))
        assert not activations
        await app.execute("101", "9223372036854775700", "promo", {}, None, "")
        await app.execute("101", "9223372036854775700", "main", {}, None, "")
        await app.dispatcher.feed_update(app.bot, message_update(text="CANCELLED"))
        assert not activations

    asyncio.run(run())


def test_unavailable_promo_preserves_code_and_allows_normal_catalog():
    async def run():
        app, service, session = setup()
        service.active_promo = lambda aid: {
            "code": "EXPIRED_GIFT",
            "state": "pending",
            "reward_type": "free_days",
            "reward_days": 7,
            "product_id": "standard",
            "available": False,
            "unavailable_reason": "Срок действия промокода истёк.",
        }
        await app.execute("101", "9223372036854775700", "promo", {}, None, "")
        message = session.calls[-1]
        assert "EXPIRED_GIFT" in message.text
        assert "Срок действия промокода истёк" in message.text
        assert all(
            "Получить доступ" not in button.text
            for row in message.reply_markup.inline_keyboard
            for button in row
        )
        assert find_button(session, "Открыть каталог")
        assert "101" not in app.promo_inputs

    asyncio.run(run())


def test_start_referral_forwarding_and_invite_uses_current_guarded_bot_username():
    async def run():
        app, service, session = setup()
        original = service.resolve_telegram
        referrals = []

        def resolve(uid, referral_code=None, username=None):
            referrals.append(referral_code)
            return original(uid)

        service.resolve_telegram = resolve
        service.referral_info = lambda aid: {
            "referral_code": "ref_9223372036854775700",
            "referral_url": None,
            "reward_percent": "17.5",
            "invited_count": 2,
            "rewarded_amount": "42.25",
            "currency": "RUB",
            "enrollment_available": True,
        }

        async def get_me():
            return SimpleNamespace(id=700001, username="CurrentSyntheticBot")

        app.bot.get_me = get_me
        await app.dispatcher.feed_update(app.bot, message_update(text="/start ref_9223372036854775701"))
        assert referrals[0] == "ref_9223372036854775701"
        await app.execute("101", "9223372036854775700", "referral", {}, None, "")
        text = session.calls[-1].text
        assert "https://t.me/CurrentSyntheticBot?start=ref_9223372036854775700" in text
        assert "17,5%" in text and "42,25 ₽" in text and "2" in text

    asyncio.run(run())
