"""Synthetic owner-requested navigation and native Telegram payload regressions."""

import asyncio
from types import SimpleNamespace

import pytest
from test_dispatcher import callback_update, find_button, setup

UID, AID, SID = "101", "9223372036854775700", "9007199254740997"


@pytest.mark.parametrize("action,icon_key", [("main", "checkout"), ("profile", "back")])
def test_navigation_color_cannot_be_overridden_by_custom_icon_or_semantic(action, icon_key):
    app, _, _ = setup()
    button = app.button(
        UID, "Назад" if icon_key == "back" else "Главное меню", action, icon_key=icon_key, semantic="success"
    )
    assert button.style == "danger"


async def press(app, session, label):
    await app.dispatcher.feed_update(app.bot, callback_update(find_button(session, label)))


def test_renewal_opens_current_product_periods_and_back_returns_subscription():
    async def run():
        app, _, session = setup()
        await app.execute(UID, AID, "subscription", {"sid": SID}, None, "")
        await press(app, session, "Продлить")
        assert "Выберите период" in session.calls[-1].text
        assert "Стандартный" in session.calls[-1].text
        assert "Универсальный" not in session.calls[-1].text
        await press(app, session, "Назад")
        assert "Мой &lt;VPN&gt;" in session.calls[-1].text

    asyncio.run(run())


@pytest.mark.parametrize(
    "intent,current,target",
    [("upgrade", "standard", "Универсальный"), ("downgrade", "universal", "Стандартный")],
)
def test_tier_change_skips_periods_and_quotes_remaining_term(intent, current, target):
    async def run():
        app, service, session = setup()
        service.subscriptions[0]["product_id"] = current
        await app.execute(UID, AID, "products", {"sid": SID, "intent": intent}, None, "")
        await press(app, session, target)
        assert "Выберите период" not in session.calls[-1].text
        await press(app, session, "Без бонусов")
        request = [call for call in service.calls if call[0] == "quote"][-1][2]
        assert request["action"] == intent
        assert request["period_days"] == 30
        assert request["subscription_id"] == SID
        assert "Остаток оплаченного срока" in session.calls[-1].text
        await press(app, session, "Назад")
        await press(app, session, "Назад")
        assert "Выберите тариф" in session.calls[-1].text

    asyncio.run(run())


def test_profile_devices_back_preserves_profile_origin():
    async def run():
        app, _, session = setup()
        await app.execute(UID, AID, "profile", {}, None, "")
        await press(app, session, "Устройства")
        assert "Лимит:" in session.calls[-1].text
        await press(app, session, "Назад")
        assert "Профиль" in session.calls[-1].text

    asyncio.run(run())


@pytest.mark.parametrize("has_notice", [False, True])
def test_profile_device_picker_keeps_origin_for_multiple_subscriptions(has_notice):
    async def run():
        app, service, session = setup()
        service.subscriptions.append({**service.subscriptions[0], "id": "9007199254740998", "name": "Вторая"})
        dashboard = service.dashboard
        service.dashboard = lambda aid, **kwargs: (
            dashboard(aid, **kwargs)
            | {
                "subscription_deletion_notices": [{"name": "Удалённая", "deleted_at": "2026-10-12T00:00:00Z"}]
                if has_notice
                else []
            }
        )
        await app.execute(UID, AID, "profile", {}, None, "")
        await press(app, session, "Устройства")
        second = next(
            b.callback_data
            for row in session.calls[-1].reply_markup.inline_keyboard
            for b in row
            if b.callback_data and app.actions[b.callback_data].data.get("sid") == "9007199254740998"
        )
        await app.dispatcher.feed_update(app.bot, callback_update(second))
        assert "Лимит:" in session.calls[-1].text
        await press(app, session, "Назад")
        assert "Профиль" in session.calls[-1].text

    asyncio.run(run())


def test_referral_copy_uses_native_copy_text_without_callback():
    async def run():
        app, service, session = setup()
        service.referral_info = lambda aid: {"referral_code": "ref_123456", "enrollment_available": True}

        async def get_me():
            return SimpleNamespace(id=700001, username="SyntheticBot")

        app.bot.get_me = get_me
        await app.execute(UID, AID, "referral", {}, None, "")
        buttons = [b for row in session.calls[-1].reply_markup.inline_keyboard for b in row]
        assert any("Скопировать" in b.text for b in buttons)
        button = next(b for b in buttons if "Скопировать" in b.text)
        assert button.copy_text.text == "https://t.me/SyntheticBot?start=ref_123456"
        assert button.callback_data is None and button.url is None

    asyncio.run(run())


@pytest.mark.parametrize("available", [True, False])
def test_bonus_menu_separates_fixed_coupon_from_wallet_and_reservation(available):
    async def run():
        app, service, session = setup()
        dashboard = service.dashboard
        service.dashboard = lambda aid, **kwargs: (
            dashboard(aid, **kwargs)
            | {
                "referral": {"rewarded_amount": "42.25", "currency": "RUB"},
                "active_promo": {
                    "code": "COUPON",
                    "reward_type": "fixed_rub",
                    "amount_rub": "2222.00",
                    "available": available,
                    "unavailable_reason": None if available else "Промокод ожидает оплаты другого заказа.",
                },
            }
        )
        await app.execute(UID, AID, "bonuses", {}, None, "")
        text = session.calls[-1].text
        assert "Бонусный баланс: 20,00 ₽" in text
        assert "От рефералов начислено всего: 42,25 ₽" in text
        assert "Промокод COUPON: 2 222,00 ₽ скидки" in text
        assert "Всего:" not in text
        if not available:
            assert "ожидает оплаты другого заказа" in text

    asyncio.run(run())


def test_bonus_menu_percent_coupon_is_concise_and_not_added_as_money():
    async def run():
        app, service, session = setup()
        dashboard = service.dashboard
        service.dashboard = lambda aid, **kwargs: (
            dashboard(aid, **kwargs)
            | {"active_promo": {"code": "FIVE", "reward_type": "percent", "percent": "5", "available": True}}
        )
        await app.execute(UID, AID, "bonuses", {}, None, "")
        assert "Промокод FIVE: (−5%)" in session.calls[-1].text
        assert "Всего:" not in session.calls[-1].text

    asyncio.run(run())


def test_bonus_menu_keeps_reserved_wallet_and_coupon_discount_separate():
    async def run():
        app, service, session = setup()
        dashboard = service.dashboard
        service.dashboard = lambda aid, **kwargs: (
            dashboard(aid, **kwargs)
            | {
                "bonus_reserved": "7.00",
                "active_promo": {
                    "code": "COUPON",
                    "reward_type": "fixed_rub",
                    "amount_rub": "2222.00",
                    "available": True,
                },
            }
        )
        await app.execute(UID, AID, "bonuses", {}, None, "")
        assert "Зарезервировано в заказах: 7,00 ₽" in session.calls[-1].text
        assert "Промокод COUPON: 2 222,00 ₽ скидки" in session.calls[-1].text
        assert "Всего:" not in session.calls[-1].text

    asyncio.run(run())


@pytest.mark.parametrize("kind,promo_percent,suffix", [("fixed_rub", "5", ""), ("percent", "5", " (−5%)")])
def test_quote_labels_wallet_spend_separately_from_named_coupon(kind, promo_percent, suffix):
    async def run():
        import re

        app, service, session = setup()
        quote = service.quote
        service.quote = lambda aid, **kwargs: (
            quote(aid, **kwargs)
            | {
                "promo_code": "COUPON",
                "promo_reward_amount_rub": "2222.00" if kind == "fixed_rub" else None,
                "promo_percent": promo_percent,
                "promo_discount_amount": "22.00",
                "bonus_amount": "0.00",
            }
        )
        await app.execute(
            UID, AID, "quote", {"product_id": "standard", "intent": "purchase", "period_days": 30}, None, ""
        )
        text = re.sub("<[^>]+>", "", session.calls[-1].text)
        assert "Бонусы со счёта: 0,00 ₽" in text
        assert f"Промокод COUPON{suffix}: 22,00 ₽" in text

    asyncio.run(run())


def test_payment_method_choices_are_unstyled():
    async def run():
        app, service, session = setup()
        catalog = service.catalog
        service.catalog = lambda: (
            catalog()
            | {"payment_methods": [{"id": "sbp", "label": "СБП", "discount_percent": "0", "available": True}]}
        )
        await app.execute(
            UID, AID, "quote", {"product_id": "standard", "intent": "purchase", "period_days": 30}, None, ""
        )
        button = next(
            b for row in session.calls[-1].reply_markup.inline_keyboard for b in row if "СБП" in b.text
        )
        assert button.style is None

    asyncio.run(run())


def test_connection_action_and_authoritative_guide_use_subscription_title():
    async def run():
        from test_connection_guide import context

        app, _, session = context()
        await app.execute(UID, AID, "subscription", {"sid": SID}, None, "")
        assert find_button(session, "Подключить подписку")
        await press(app, session, "Подключить подписку")
        assert "Подключить подписку" in session.calls[-1].text
        assert "https://vpn.example.test/synthetic" in session.calls[-1].text
        assert find_button(session, "iOS")

    asyncio.run(run())
