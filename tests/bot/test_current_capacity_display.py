import asyncio

import pytest
from sunrise_bot import copy
from test_dispatcher import setup


@pytest.mark.parametrize(
    "value, expected", [(None, "Лимит не задан в панели"), (7, "7"), (0, "Без ограничения")]
)
def test_current_capacity_copy_preserves_unknown_finite_and_explicit_zero(value, expected):
    assert copy.current_device_limit(value) == expected


@pytest.mark.parametrize("action", ["subscription", "devices"])
@pytest.mark.parametrize("panel_limit", [None, 0])
def test_current_subscription_screens_do_not_turn_unknown_capacity_into_unlimited(action, panel_limit):
    async def run():
        app, service, session = setup()
        sub = service.subscriptions[0]
        sub.update(devices_limit=None, panel_device_limit=panel_limit)
        await app.execute("101", "9223372036854775700", action, {"sid": sub["id"]}, None, "")
        assert "Лимит не задан в панели" in session.calls[-1].text
        assert "Без ограничения" not in session.calls[-1].text
        assert "None" not in session.calls[-1].text

    asyncio.run(run())
