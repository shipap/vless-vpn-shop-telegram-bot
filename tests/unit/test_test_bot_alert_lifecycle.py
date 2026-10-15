import asyncio
from types import SimpleNamespace

from scripts import run_test_bot as launcher


def test_guarded_launcher_starts_and_cleans_owner_alert_loop(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "SUNRISE_DATABASE_URL=postgresql://localhost/sunrise_test\n", encoding="utf-8"
    )
    delivered = asyncio.Event()
    cancelled = []
    closed = []
    safety = SimpleNamespace(
        bot_ids=frozenset({"700002"}), recipient_ids=frozenset({"202", "201"})
    )
    config = SimpleNamespace(safety=safety, database_url="postgresql://localhost/sunrise_test")
    monkeypatch.setattr(launcher.RuntimeConfig, "from_env", lambda values: config)

    async def get_me():
        return SimpleNamespace(id=700002)

    async def close():
        closed.append(True)

    async def poll(*args, **kwargs):
        await asyncio.wait_for(delivered.wait(), timeout=1)

    async def owner_alerts(app):
        delivered.set()
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.append(True)

    app = SimpleNamespace(
        bot=SimpleNamespace(get_me=get_me, session=SimpleNamespace(close=close)),
        dispatcher=SimpleNamespace(start_polling=poll),
        commerce=SimpleNamespace(db=SimpleNamespace(engine=SimpleNamespace(dispose=lambda: None))),
    )
    monkeypatch.setattr(launcher, "compose", lambda value: app)
    monkeypatch.setattr(launcher, "deliver_pending_owner_alerts", owner_alerts, raising=False)
    asyncio.run(launcher.run(tmp_path, False))
    assert delivered.is_set() and closed == [True] and cancelled == [True]


def test_wrong_bot_identity_never_starts_owner_alerts(tmp_path, monkeypatch):
    import pytest

    (tmp_path / ".env").write_text(
        "SUNRISE_DATABASE_URL=postgresql://localhost/sunrise_test\n", encoding="utf-8"
    )
    safety = SimpleNamespace(
        bot_ids=frozenset({"700002"}), recipient_ids=frozenset({"202", "201"})
    )
    config = SimpleNamespace(safety=safety, database_url="postgresql://localhost/sunrise_test")
    monkeypatch.setattr(launcher.RuntimeConfig, "from_env", lambda values: config)
    sends = []

    async def get_me():
        return SimpleNamespace(id=1)

    async def close():
        pass

    async def alerts(app):
        sends.append(True)

    app = SimpleNamespace(
        bot=SimpleNamespace(get_me=get_me, session=SimpleNamespace(close=close)),
        commerce=SimpleNamespace(db=SimpleNamespace(engine=SimpleNamespace(dispose=lambda: None))),
    )
    monkeypatch.setattr(launcher, "compose", lambda value: app)
    monkeypatch.setattr(launcher, "deliver_pending_owner_alerts", alerts)
    with pytest.raises(PermissionError):
        asyncio.run(launcher.run(tmp_path, False))
    assert not sends
