import asyncio
from types import SimpleNamespace

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import GetMe, SendMessage
from sunrise_bot.safety import GuardedSession, ProductionBotSafety
from sunrise_core.errors import DomainError


def test_production_bot_customer_send_obeys_fresh_notice_gate_but_identity_get_is_readonly():
    allowed = [False]
    calls = []

    def require(capability=None):
        if capability == "notice_telegram" and not allowed[0]:
            raise DomainError("production_stage_denied", "Операция временно недоступна.", 503)

    policy = SimpleNamespace(production=True, scope=SimpleNamespace(bot_id="100001"), require=require)

    class Wrapped(BaseSession):
        async def close(self):
            pass

        async def stream_content(self, *args, **kwargs):
            yield b""

        async def make_request(self, bot, method, timeout=None):
            calls.append(method.__api_method__)
            return None

    session = GuardedSession(Wrapped(), ProductionBotSafety(policy))
    bot = Bot("100001:synthetic-token-value-123456789", session=session)

    async def run():
        await session.make_request(bot, GetMe())
        with pytest.raises(DomainError):
            await session.make_request(bot, SendMessage(chat_id=123456, text="Профиль"))
        assert calls == ["getMe"]
        allowed[0] = True
        await session.make_request(bot, SendMessage(chat_id=123456, text="Профиль"))
        allowed[0] = False
        with pytest.raises(DomainError):
            await session.make_request(bot, SendMessage(chat_id=123456, text="Профиль"))
        assert calls == ["getMe", "sendMessage"]

    asyncio.run(run())
