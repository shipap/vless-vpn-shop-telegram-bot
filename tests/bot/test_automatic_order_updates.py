import asyncio

import pytest
from sunrise_infrastructure.db import Order
from sunrise_infrastructure.notifications import NotificationService
from sunrise_infrastructure.scheduler import Scheduler
from test_shared_backend import shared_backend as shared_backend
from test_shared_backend import visible_message_text


def buttons(session):
    return [button for row in session.calls[-1].reply_markup.inline_keyboard for button in row]


@pytest.mark.parametrize("paid", [False, True])
def test_order_uses_automatic_updates_and_keeps_history_without_refresh(shared_backend, paid):
    async def run():
        app, service, _, session, aid, telegram = shared_backend
        service.sandbox_payments = not paid
        quote = service.quote(aid, "standard", 30, "purchase")
        order = service.create_order(aid, quote["id"], "automatic-order", source="bot")
        if paid:
            service.test_pay(aid, order["id"])
        await app.execute(str(telegram), str(aid), "order", {"order_id": order["id"]}, None, "")
        order_buttons = buttons(session)
        assert all("Проверить статус" not in button.text for button in order_buttons)
        assert any("История платежей" in button.text for button in order_buttons)
        text = visible_message_text(session)
        assert "обновите статус" not in text.casefold()
        assert "проверить ниже" not in text.casefold()
        if paid:
            assert "Оплата получена" in text and "сообщим" in text
        else:
            assert "ссылка" in text.casefold() and "сохранён" in text

    asyncio.run(run())


def test_payment_and_ready_notices_arrive_without_order_callback(shared_backend):
    async def run():
        app, service, accounts, session, aid, telegram = shared_backend
        settings = accounts.settings
        settings.test_telegram_recipients = str(telegram)
        service.public_settings = settings
        delivered = []
        notifications = NotificationService(
            service.db,
            settings,
            telegram_transport=lambda recipient, payload, identifier: delivered.append((recipient, payload)),
        )
        quote = service.quote(aid, "standard", 30, "purchase")
        order = service.create_order(aid, quote["id"], "automatic-notices", source="bot")
        service.test_pay(aid, order["id"])
        await app.execute(str(telegram), str(aid), "order", {"order_id": order["id"]}, None, "")
        assert all("Проверить статус" not in button.text for button in buttons(session))
        # No customer callback after the displayed paid state: only the existing worker services run.
        notifications.deliver_pending()
        service.run_jobs()
        scheduler = Scheduler(service, settings)
        scheduler.notify_ready()
        notifications.deliver_pending()
        scheduler.notify_ready()
        notifications.deliver_pending()
        assert [(recipient, payload["title"]) for recipient, payload in delivered] == [
            (str(telegram), "Заказ оплачен"),
        ]
        assert len(service.dashboard(aid)["payments"]) == 1
        assert service.dashboard(aid)["subscriptions"][0]["status"] == "active"

    asyncio.run(run())


@pytest.mark.parametrize("state", ["canceled", "expired", "reconciliation_required", "refund_review"])
@pytest.mark.parametrize("environment", ["sandbox", "local_test"])
def test_terminal_or_review_order_never_announces_future_checkout_link(shared_backend, state, environment):
    async def run():
        app, service, _, session, aid, telegram = shared_backend
        service.sandbox_payments = environment == "sandbox"
        quote = service.quote(aid, "standard", 30, "purchase")
        order = service.create_order(aid, quote["id"], "terminal-order", source="bot")
        with service.db.transaction() as tx:
            tx.get(Order, order["id"]).status = state
        await app.execute(str(telegram), str(aid), "order", {"order_id": order["id"]}, None, "")
        text = visible_message_text(session)
        assert "пришлём" not in text and "Готовим ссылку" not in text
        assert "Ссылка на оплату пока недоступна" not in text
        assert "локальный тестовый заказ" not in text
        assert not any(button.url and "Оплатить" in button.text for button in buttons(session))
        assert service.order(aid, order["id"])["status"] == state

    asyncio.run(run())
