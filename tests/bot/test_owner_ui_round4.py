import asyncio
from types import SimpleNamespace

from sunrise_bot.theme import presentation
from test_connection_guide import GUIDE, buttons, connect, context
from test_dispatcher import setup


def test_connection_qr_precedes_neutral_open_and_omits_share_notice():
    async def run():
        app, _, session = context()
        await connect(app, session)
        markup = buttons(session)
        qr = next(button for button in markup if "QR-код" in button.text)
        opened = next(button for button in markup if button.url == "https://vpn.example.test/synthetic")
        assert markup.index(qr) < markup.index(opened)
        assert qr.style == "primary"
        assert opened.style is None
        assert GUIDE["notice"] not in session.calls[-1].text

    asyncio.run(run())


def test_notification_pages_and_read_preserve_owner_bound_offset(monkeypatch):
    calls = []

    class Inbox:
        def __init__(self, *args):
            pass

        def list(self, account, limit=5, offset=0):
            calls.append(("list", account, limit, offset))
            return {
                "items": [
                    {"id": str(i), "title": f"Событие {i}", "body": "Текст"}
                    for i in range(offset, offset + 5)
                ],
                "unread_count": 10,
                "total_count": 10,
                "next_offset": 5 if not offset else None,
                "previous_offset": 0 if offset else None,
            }

        def mark_read(self, account, identifier):
            calls.append(("read", account, identifier))

    monkeypatch.setattr("sunrise_infrastructure.notifications.NotificationService", Inbox)

    async def run():
        app, _, session = setup()
        app.accounts = SimpleNamespace(db=None, settings=None)
        await app.execute("101", "own", "notifications", {}, None, "synthetic")
        actions = [app.actions[b.callback_data] for b in buttons(session) if b.callback_data]
        assert len([a for a in actions if a.action == "notification"]) == 5
        page = next(a for a in actions if a.action == "notifications")
        assert page.data["offset"] == 5
        await app.execute("101", "own", page.action, page.data, None, "synthetic")
        item = next(
            app.actions[b.callback_data]
            for b in buttons(session)
            if b.callback_data and app.actions[b.callback_data].action == "notification"
        )
        assert item.data["offset"] == 5
        await app.execute("101", "own", item.action, item.data, None, "synthetic")
        read = next(
            app.actions[b.callback_data]
            for b in buttons(session)
            if b.callback_data and app.actions[b.callback_data].action == "notification_read"
        )
        assert read.data["offset"] == 5
        await app.execute("101", "own", read.action, read.data, None, "synthetic")
        assert calls[-1] == ("list", "own", 5, 5)

    asyncio.run(run())


def test_news_has_no_refresh_and_profile_requests_panel_refresh():
    async def run():
        app, service, session = setup()
        service.node_news = lambda aid, **kwargs: {"items": [], "next_cursor": None}
        await app.execute("101", "own", "node_news", {}, None, "synthetic")
        assert not any("Обновить" in button.text for button in buttons(session))
        original = service.dashboard
        requested = []

        def dashboard(aid, **kwargs):
            requested.append(kwargs)
            return original(aid, **kwargs)

        service.dashboard = dashboard
        await app.execute("101", "own", "profile", {}, None, "synthetic")
        assert requested == [{"refresh_panel": True}]

    asyncio.run(run())


def test_notification_and_qr_use_configured_premium_icons():
    for key in ("notification", "connection_qr"):
        theme = {"icons": {key: {"custom_emoji_id": "123456", "fallback": "🔹"}}}
        assert presentation(theme, key, custom_emoji=True)[1]["icon_custom_emoji_id"] == "123456"
    assert (
        presentation({"styles": {"primary": "success"}}, "connection_qr", custom_emoji=False)[1]["style"]
        == "primary"
    )
    for key in ("connection_open", "payment_bank_card", "payment_sbp", "payment_crypto", "payment_xrocket"):
        assert (
            "style"
            not in presentation(
                {"styles": {"neutral": "primary"}}, key, custom_emoji=False, semantic="neutral"
            )[1]
        )


def test_fee_methods_are_neutral_with_matching_icons_and_xrocket():
    from test_admin_flow import flow

    async def run():
        app, admin = flow()
        await admin.execute("101", "a1", "admin_setting_fee", {}, None)
        methods = {data["method"]: data for _, _, data in app.views[-1]["buttons"]}
        assert set(methods) == {"bank_card", "sbp", "crypto", "xrocket"}
        assert all(
            data["icon_key"] == "payment_" + method and data["semantic"] == "neutral"
            for method, data in methods.items()
        )

    asyncio.run(run())


def test_verified_admin_can_remove_warning_without_owner_security_or_backups():
    from test_admin_flow import flow

    async def run():
        app, admin = flow()
        app.admin.bot_console.capabilities = lambda actor: {
            "staff_role": "admin",
            "capabilities": ["users_read", "device_unlinks_manage"],
        }
        app.admin.bot_console.bot_user_detail = lambda *args, **kwargs: {
            "account": {"id": "9"},
            "can_moderate": True,
            "moderation": {
                "current_warnings": 1,
                "total_warnings": 3,
                "unlink_individual_month": 2,
                "unlink_bulk_month": 1,
            },
        }
        await admin.execute("101", "a1", "admin_user", {"target_id": "9"}, None)
        controls = [data for _, action, data in app.views[-1]["buttons"] if action == "admin_user_moderate"]
        assert {data["moderation_action"] for data in controls} == {"warn", "block", "remove_warning"}
        assert "1/2" in app.views[-1]["body"]
        await admin.execute(
            "101",
            "a1",
            "admin_user_moderate",
            {"target_id": "9", "moderation_action": "remove_warning"},
            None,
        )
        assert admin.drafts["101"].context["action"] == "remove_warning"
        assert admin.drafts["101"].step == "review"
        assert "9" in admin.drafts["101"].reason
        assert "Укажите причину" not in app.views[-1]["body"]
        await admin.execute("101", "a1", "admin", {}, None)
        assert not any(action == "admin_backups" for _, action, _ in app.views[-1]["buttons"])

    asyncio.run(run())


def test_signed_owner_notice_resolves_saved_event_then_opens_confirmation_without_mutation():
    from sunrise_core.notice_callbacks import owner_notice_callback
    from test_dispatcher import callback_update

    async def run():
        app, _, _ = setup()
        app.accounts = SimpleNamespace(
            settings=SimpleNamespace(secret_key="synthetic-notice-secret-" + "x" * 32)
        )
        calls = []

        async def authorized(uid, aid):
            return True

        async def call(method, aid, event_id):
            calls.append((method, event_id))
            assert method == "bot_owner_device_notice"
            return {"account_id": "9", "event_id": event_id}

        async def execute(uid, aid, action, data, mid):
            calls.append((action, data))

        app.admin_flow.authorized, app.admin_flow.call, app.admin_flow.execute = authorized, call, execute
        event_id = "0123456789abcdef0123456789abcdef"
        for operation, action in (
            ("p", "admin_user"),
            ("w", "admin_user_moderate"),
            ("b", "admin_user_moderate"),
        ):
            token = owner_notice_callback(operation, event_id, app.accounts.settings.secret_key)
            await app.dispatcher.feed_update(app.bot, callback_update(token))
            assert calls[-2] == ("bot_owner_device_notice", "01234567-89ab-cdef-0123-456789abcdef")
            assert calls[-1][0] == action and calls[-1][1]["target_id"] == "9"
            if operation != "p":
                assert calls[-1][1]["moderation_action"] == ("warn" if operation == "w" else "block")
                assert calls[-1][1]["notice_event_id"] == "01234567-89ab-cdef-0123-456789abcdef"
        assert app.callback_action(token[:-1] + ("0" if token[-1] != "0" else "1"), "101") is None

    asyncio.run(run())


def test_regular_callback_does_not_require_notice_signing_settings():
    app, _, _ = setup()
    app.accounts = SimpleNamespace()
    button = app.button("101", "Профиль", "profile")
    assert app.callback_action(button.callback_data, "101").action == "profile"
    assert app.callback_action("unknown", "101") is None


def test_shared_default_connection_fallback_omits_removed_notice_only():
    from sunrise_infrastructure.service_content import CONNECTION_NOTICE, help_center

    content = help_center(None, {})
    assert CONNECTION_NOTICE not in content["connection_instructions"]
    assert "Откройте или скопируйте личную ссылку" in content["connection_instructions"]
    assert "Выберите локацию" in content["connection_instructions"]
