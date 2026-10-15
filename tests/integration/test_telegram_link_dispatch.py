import asyncio

import pytest
from aiogram import Bot
from aiogram.types import Update
from sqlalchemy import select
from sunrise_bot.adapter import BotApplication
from sunrise_bot.safety import BotSafety, GuardedSession
from sunrise_infrastructure.db import Account, MergeProof

from tests.bot.test_presentation import TOKEN, FakeSession
from tests.integration.test_v10_admin_mfa import login

pytest_plugins = ["tests.integration.test_v10_admin_mfa"]


@pytest.mark.parametrize("supplied", ["synthetic_owner", "@SYNTHETIC_OWNER"])
def test_real_start_persists_trusted_username_before_api_link_proof(mfa_context, supplied):
    db, app, client, email, _, _, _ = mfa_context
    telegram_id = 70000000000000001
    target = app.state.commerce.resolve_telegram(telegram_id)
    with db.transaction() as tx:
        tx.add(Account(email=email))
    auth = login(mfa_context)
    assert auth["account"]["telegram_id"] is None and auth["is_admin"] is False
    source_id = int(auth["account"]["id"])
    headers = {"X-CSRF-Token": auth["csrf_token"]}
    payload = {"telegram_username": supplied}
    initial = client.post("/api/v1/account/link/request", json=payload, headers=headers)
    assert initial.status_code == 400
    assert initial.json()["error"]["code"] == "link_target_unavailable"
    with db.transaction() as tx:
        assert tx.get(Account, int(target["id"])).telegram_username is None
        assert tx.scalar(select(MergeProof)) is None

    async def receive_start(username):
        safety = BotSafety(frozenset({"700001"}), frozenset({str(telegram_id)}))
        transport = FakeSession()
        bot = Bot(TOKEN, session=GuardedSession(transport, safety))
        application = BotApplication(bot, app.state.commerce, safety, accounts=app.state.accounts)
        update = Update.model_validate(
            {
                "update_id": 1,
                "message": {
                    "message_id": 10,
                    "date": 1791384304,
                    "chat": {"id": telegram_id, "type": "private"},
                    "from": {
                        "id": telegram_id,
                        "is_bot": False,
                        "first_name": "Синтетический пользователь",
                        "username": username,
                    },
                    "text": "/start",
                },
            }
        )
        try:
            await application.dispatcher.feed_update(bot, update)
            assert transport.calls
        finally:
            await bot.session.close()

    asyncio.run(receive_start("synthetic_owner"))
    with db.transaction() as tx:
        current = tx.scalar(select(Account).where(Account.telegram_id == telegram_id))
        assert current.id == int(target["id"])
        assert current.telegram_username == "synthetic_owner"
    response = client.post("/api/v1/account/link/request", json=payload, headers=headers)
    assert response.status_code == 200, response.text
    with db.transaction() as tx:
        proof = tx.get(MergeProof, response.json()["proof"].split(".")[0])
        assert proof.source_id == source_id and proof.target_id == int(target["id"])
        assert not proof.used
        assert tx.get(Account, source_id).telegram_id is None
        assert tx.get(Account, int(target["id"])).email is None

    # A later trusted Telegram update removing the username invalidates name resolution.
    asyncio.run(receive_start(None))
    removed = client.post("/api/v1/account/link/request", json=payload, headers=headers)
    assert removed.status_code == 400
    assert removed.json()["error"]["code"] == "link_target_unavailable"
    with db.transaction() as tx:
        assert tx.get(Account, int(target["id"])).telegram_username is None
        assert len(list(tx.scalars(select(Account)))) == 2
