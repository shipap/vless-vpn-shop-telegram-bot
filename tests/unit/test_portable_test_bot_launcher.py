import asyncio
import json
from types import SimpleNamespace

import pytest

from scripts import run_test_bot as launcher


def runtime_values(bot_id="700002", recipients="201,202", database="sunrise_test_backend"):
    return {
        "SUNRISE_BOT_TEST_TOKEN": bot_id + ":" + "x" * 35,
        "SUNRISE_TEST_BOT_IDS": bot_id,
        "SUNRISE_TEST_TELEGRAM_RECIPIENTS": recipients,
        "SUNRISE_DATABASE_URL": "postgresql://localhost:55439/" + database,
    }


def synthetic_launcher(monkeypatch, values, *, identity=700002, owner="202"):
    calls = []
    monkeypatch.setattr(launcher, "dotenv_values", lambda *args, **kwargs: dict(values))

    async def show(*args, **kwargs):
        return SimpleNamespace(reply_markup=SimpleNamespace(inline_keyboard=[]), entities=[])

    async def get_me():
        calls.append(("get_me",))
        return SimpleNamespace(id=identity)

    async def close():
        calls.append(("close",))

    async def call(method, uid):
        calls.append((method, uid))
        return {"id": "9007199254740993"}

    async def main(uid, account):
        calls.append(("main", uid, account["id"]))
        await app.presenter.show(uid)

    async def execute(uid, aid, action, *args):
        calls.append(("execute", uid, aid, action))
        await app.presenter.show(uid)

    started = asyncio.Event()

    async def alerts(app):
        calls.append(("alerts",))
        started.set()
        await asyncio.Event().wait()

    async def polling(*args, **kwargs):
        calls.append(("polling", kwargs))
        await asyncio.wait_for(started.wait(), timeout=1)

    app = SimpleNamespace(
        accounts=SimpleNamespace(settings=SimpleNamespace(owner_id=owner)),
        bot=SimpleNamespace(get_me=get_me, session=SimpleNamespace(close=close)),
        dispatcher=SimpleNamespace(start_polling=polling),
        commerce=SimpleNamespace(
            db=SimpleNamespace(engine=SimpleNamespace(dispose=lambda: calls.append(("dispose",))))
        ),
        presenter=SimpleNamespace(show=show, custom_emoji=True),
        call=call,
        main=main,
        execute=execute,
    )

    def compose(config):
        calls.append(("compose", config))
        return app

    monkeypatch.setattr(launcher, "compose", compose)
    monkeypatch.setattr(launcher, "deliver_pending_owner_alerts", alerts)
    return app, calls


@pytest.mark.parametrize("admin_preview", [False, True])
def test_second_configured_bot_greets_explicit_owner_not_first_recipient(
    tmp_path, monkeypatch, admin_preview
):
    app, calls = synthetic_launcher(monkeypatch, runtime_values())
    original_show = app.presenter.show
    asyncio.run(launcher.run(tmp_path, not admin_preview, admin_preview))
    assert ("resolve_telegram", 202) in calls
    expected = (
        ("execute", "202", "9007199254740993", "admin")
        if admin_preview
        else ("main", "202", "9007199254740993")
    )
    assert expected in calls
    assert all(201 not in call for call in calls)
    polling = next(call[1] for call in calls if call[0] == "polling")
    assert polling == {
        "allowed_updates": ["message", "callback_query"],
        "handle_as_tasks": False,
        "close_bot_session": False,
    }
    report = json.loads(next((tmp_path / ".artifacts").glob("*-bot-live.json")).read_text(encoding="utf-8"))
    assert report["bot_id"] == "700002" and report["recipient_id"] == "202"
    assert report["database"] == "sunrise_test_backend"
    assert app.presenter.show is original_show
    assert calls[-2:] == [("close",), ("dispose",)]


@pytest.mark.parametrize("owner", [None, "", "999", "-202", "not-an-id"])
def test_optional_delivery_requires_explicit_allowlisted_owner_before_getme(tmp_path, monkeypatch, owner):
    _, calls = synthetic_launcher(monkeypatch, runtime_values(), owner=owner)
    with pytest.raises(PermissionError):
        asyncio.run(launcher.run(tmp_path, True))
    assert not any(call[0] in {"get_me", "main", "execute", "alerts", "polling"} for call in calls)
    assert calls[-2:] == [("close",), ("dispose",)]


@pytest.mark.parametrize(
    "changes",
    [
        {"SUNRISE_TEST_BOT_IDS": ""},
        {"SUNRISE_TEST_TELEGRAM_RECIPIENTS": ""},
        {"SUNRISE_TEST_BOT_IDS": "700003"},
        {"SUNRISE_TEST_TELEGRAM_RECIPIENTS": "-201"},
        {"SUNRISE_DATABASE_URL": "postgresql://example.test:55439/sunrise_test_backend"},
        {"SUNRISE_DATABASE_URL": "postgresql://localhost:5432/sunrise_test_backend"},
        {"SUNRISE_DATABASE_URL": "postgresql://localhost:55439/sunrise_legacy"},
    ],
)
def test_shared_runtime_safety_rejects_invalid_inputs_before_composition(tmp_path, monkeypatch, changes):
    _, calls = synthetic_launcher(monkeypatch, runtime_values() | changes)
    with pytest.raises(ValueError):
        asyncio.run(launcher.run(tmp_path, False))
    assert calls == []


def test_verified_getme_mismatch_cleans_resources_without_polling_or_owner_delivery(tmp_path, monkeypatch):
    _, calls = synthetic_launcher(monkeypatch, runtime_values(), identity=700003)
    with pytest.raises(PermissionError):
        asyncio.run(launcher.run(tmp_path, True))
    assert ("get_me",) in calls
    assert not any(call[0] in {"main", "execute", "alerts", "polling"} for call in calls)
    assert calls[-2:] == [("close",), ("dispose",)]


def test_ordinary_polling_does_not_require_owner_or_send_optional_greeting(tmp_path, monkeypatch):
    _, calls = synthetic_launcher(monkeypatch, runtime_values(), owner=None)
    asyncio.run(launcher.run(tmp_path, False))
    assert ("get_me",) in calls and ("alerts",) in calls
    assert any(call[0] == "polling" for call in calls)
    assert not any(call[0] in {"resolve_telegram", "main", "execute"} for call in calls)
    assert calls[-2:] == [("close",), ("dispose",)]
