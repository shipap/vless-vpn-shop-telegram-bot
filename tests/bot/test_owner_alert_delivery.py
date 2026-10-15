import asyncio
from uuid import uuid4

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendMessage
from sunrise_bot.safety import BotSafety
from sunrise_infrastructure.db import Account, Job
from test_dispatcher import setup

from tests.integration.test_bot_admin_v7 import bot_admin_v7 as bot_admin_v7


def application(context):
    console, owner, customer = context
    app, commerce, transport = setup()
    app.accounts = console.admin.commerce.accounts
    app.admin_console = console
    uid = console.settings.owner_id
    app.safety = BotSafety(frozenset({"700001"}), frozenset({uid}))
    app.bot.session.safety = app.safety
    with console.db.transaction() as session:
        account = session.get(Account, owner)
        dto = {
            "id": str(owner),
            "telegram_id": str(account.telegram_id),
            "email": account.email,
            "email_verified": True,
        }
        job = Job(
            key="bot-claim:" + uuid4().hex,
            kind="owner_device_unlink",
            account_id=customer,
            payload={
                "target_id": uid,
                "account_id": str(customer),
                "month": "2026-10",
                "unlink_month": 2,
                "unlink_total": 7,
                "connection_url": "must-not-render",
                "hwid": "SYNTHETIC-OWNER-DEVICE",
                "device_id": "synthetic-device",
                "device_name": "Синтетическое устройство",
                "generation": 1,
                "telegram_username": "synthetic_customer",
            },
        )
        session.add(job)
        session.flush()
        identifier = job.id
    commerce.resolve_telegram = lambda *args, **kwargs: dto
    commerce.account_info = lambda *args: dto
    return app, console, owner, identifier, transport


@pytest.mark.parametrize("failure", ["timeout", "forbidden", "429"])
def test_transport_failure_persists_owner_delivery_outcome(bot_admin_v7, failure):
    async def run():
        app, console, _, identifier, _ = application(bot_admin_v7)
        method = SendMessage(chat_id=console.settings.owner_id, text="Синтетическое сообщение")
        error = {
            "timeout": TimeoutError(),
            "forbidden": TelegramForbiddenError(method, "Forbidden"),
            "429": TelegramRetryAfter(method, "Retry", 5),
        }[failure]

        async def send(*args, **kwargs):
            with console.db.transaction() as session:
                assert session.get(Job, identifier).state == "in_flight"
            raise error

        app.presenter.show = send
        with pytest.raises(type(error)):
            await app.deliver_owner_alerts()
        with console.db.transaction() as session:
            assert (
                session.get(Job, identifier).state
                == {"timeout": "review", "forbidden": "blocked", "429": "pending"}[failure]
            )
        assert await app.deliver_owner_alerts() == 0

    asyncio.run(run())


def test_send_then_ack_crash_does_not_resend_on_next_tick(bot_admin_v7, monkeypatch):
    async def run():
        app, console, _, identifier, transport = application(bot_admin_v7)

        def crash(*args):
            raise RuntimeError("synthetic persistence failure")

        monkeypatch.setattr(console, "acknowledge_owner_alert", crash)
        with pytest.raises(RuntimeError):
            await app.deliver_owner_alerts()
        assert len(transport.calls) == 1
        assert "must-not-render" not in transport.calls[0].text
        assert await app.deliver_owner_alerts() == 0
        assert len(transport.calls) == 1
        with console.db.transaction() as session:
            assert session.get(Job, identifier).state == "in_flight"

    asyncio.run(run())


def test_owner_success_notice_shows_device_identity_and_warning_threshold_without_credentials(bot_admin_v7):
    async def run():
        app, _, _, _, transport = application(bot_admin_v7)
        assert await app.deliver_owner_alerts() == 1
        message = transport.calls[-1].text
        assert "SYNTHETIC-OWNER-DEVICE" not in message
        assert "@synthetic_customer" in message
        assert "Синтетическое устройство" in message
        assert "Предупреждения: 0/2" in message
        assert "must-not-render" not in message

    asyncio.run(run())
