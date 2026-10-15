"""Синтетические потребители бота: без БД, провайдеров и Telegram-сети."""

import asyncio

import pytest
from test_dispatcher import setup

AID = "9223372036854775700"
STATES = ("pending", "in_flight", "unknown", "review", "archived")


def labels(screen):
    return [button.text for row in screen.reply_markup.inline_keyboard for button in row]


def test_server_cleanup_date_and_eligible_discount_are_displayed_without_clock_arithmetic():
    async def run():
        app, service, session = setup()
        sub = service.subscriptions[0]
        sub.update(
            retention_state="none",
            cleanup_at="2026-10-18T00:00:00Z",
            manual_renewal_discount_eligible=True,
            manual_renewal_discount_percent="10",
        )
        await app.execute("101", AID, "subscription", {"sid": sub["id"]}, None, "")
        assert (
            "Удаление VPN-подписки из панели запланировано через 7 дней после окончания срока при отсутствии препятствий"
            in session.calls[-1].text
        )
        assert "18.10.2026 03:00 МСК" in session.calls[-1].text
        assert "10%" in session.calls[-1].text
        sub.update(cleanup_at=None, manual_renewal_discount_eligible=False)
        await app.execute("101", AID, "subscription", {"sid": sub["id"]}, None, "")
        assert "запланировано через 7 дней" not in session.calls[-1].text
        assert "10%" not in session.calls[-1].text

    asyncio.run(run())


@pytest.mark.parametrize("state", STATES)
def test_held_subscription_hides_target_actions_but_keeps_purchase_and_agreement_cancel(state):
    async def run():
        app, service, session = setup()
        sub = service.subscriptions[0]
        sub.update(retention_state=state, archived_at="2026-10-18T00:00:00Z")
        await app.execute("101", AID, "subscription", {"sid": sub["id"]}, None, "")
        screen = session.calls[-1]
        buttons = labels(screen)
        assert not any(
            label in text
            for text in buttons
            for label in (
                "Подключить VPN",
                "Продлить",
                "Устройства",
                "Повысить тариф",
                "Включить автопродление",
            )
        )
        assert any("Добавить подписку" in text for text in buttons)
        assert any("Отключить автопродление" in text for text in buttons)
        assert "Автопродление:" in screen.text
        if state == "archived":
            assert "VPN-подписка удалена из панели" in screen.text
            assert "Можно оформить новую подписку" in screen.text
        elif state in {"unknown", "review"}:
            assert "требует проверки" in screen.text
            assert "VPN-подписка удалена из панели" not in screen.text
        await app.execute("101", AID, "cancel", {"sid": sub["id"]}, None, "")
        assert ("cancel", AID, sub["id"]) in service.calls

    asyncio.run(run())


@pytest.mark.parametrize(
    "action", ("connect", "connection_qr", "devices", "products", "device_quote", "enroll_confirm")
)
def test_old_target_action_cannot_expose_link_or_mutation_controls(action):
    async def run():
        app, service, session = setup()
        sub = service.subscriptions[0]
        sub.update(retention_state="unknown")
        await app.execute("101", AID, action, {"sid": sub["id"], "intent": "renew"}, None, "")
        screen = session.calls[-1]
        assert "требует проверки" in getattr(screen, "text", "")
        assert not any(
            getattr(button, "url", None) == sub["connection_url"]
            for row in screen.reply_markup.inline_keyboard
            for button in row
        )
        assert not any(call[0] in {"quote", "enroll", "revoke"} for call in service.calls)
        assert not any("Докупить" in text or "Подтвердить подключение" in text for text in labels(screen))

    asyncio.run(run())


@pytest.mark.parametrize(
    "source,percent,expected",
    (
        ("near_expiry_renewal", "10", "Скидка на продление: 10%"),
        ("renewal_campaign", "20", "Кампания продления: 20%"),
    ),
)
def test_quote_identifies_the_selected_discount_without_promising_other_candidate(source, percent, expected):
    async def run():
        app, service, session = setup()
        original = service.quote

        def quote(*args, **kwargs):
            return original(*args, **kwargs) | {
                "selected_secondary": {"source": source, "percent": percent},
                "discount_candidates": [{"source": "near_expiry_renewal", "percent": "10"}],
            }

        service.quote = quote
        sub = service.subscriptions[0]
        await app.execute(
            "101",
            AID,
            "quote",
            {"sid": sub["id"], "intent": "renew", "period_days": 30, "product_id": "standard"},
            None,
            "",
        )
        assert expected in session.calls[-1].text
        if source != "near_expiry_renewal":
            assert "Скидка на продление: 10%" not in session.calls[-1].text

    asyncio.run(run())


def test_held_target_never_offers_new_enrollment_even_when_enrollment_is_enabled():
    async def run():
        app, service, session = setup()
        service.enabled = True
        original = service.catalog
        service.catalog = lambda: original() | {"capabilities": {"recurring": True}}
        sub = service.subscriptions[0]
        sub.update(retention_state="review", recurring=None)
        await app.execute("101", AID, "subscription", {"sid": sub["id"]}, None, "")
        assert not any("автопродление" in text for text in labels(session.calls[-1]))

    asyncio.run(run())
