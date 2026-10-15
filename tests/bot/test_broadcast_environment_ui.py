import asyncio
from types import SimpleNamespace

import pytest
from test_admin_flow import flow


@pytest.mark.parametrize("local_test", [False, True])
def test_broadcast_review_offers_test_self_only_on_test_runtime(local_test):
    async def run():
        app, admin = flow()
        app.admin.bot_console.settings = SimpleNamespace(local_test=local_test)
        await admin.draft_start(
            "101", "a1", "broadcast", [], values={"nodes": [{"type": "text", "text": "Сообщение"}]}
        )
        actions = {action for _, action, _ in app.views[-1]["buttons"]}
        assert ("admin_broadcast_test" in actions) is local_test
        assert "admin_save" in actions

    asyncio.run(run())
