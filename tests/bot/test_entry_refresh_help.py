"""Проверки потребителей без БД, сети и отправки уведомлений."""

import asyncio
import html

import pytest
from test_dispatcher import setup

AID = "9223372036854775700"


def tracked_setup():
    app, service, session = setup()
    original = service.dashboard
    reads = []

    def dashboard(account_id, *, refresh_panel=False, refresh_subscription_id=None):
        reads.append((refresh_panel, refresh_subscription_id))
        return original(account_id)

    service.dashboard = dashboard
    return app, service, session, reads


def test_subscriptions_entry_refreshes_once_and_reuses_single_target():
    async def run():
        app, service, session, reads = tracked_setup()
        await app.execute("101", AID, "subscriptions", {}, None, "")
        assert reads == [(True, None)]
        assert html.escape(service.subscriptions[0]["name"]) in session.calls[-1].text

    asyncio.run(run())


def test_selected_subscription_entry_refreshes_only_that_target():
    async def run():
        app, service, _, reads = tracked_setup()
        sid = service.subscriptions[0]["id"]
        await app.execute("101", AID, "subscription", {"sid": sid}, None, "")
        assert reads == [(True, sid)]

    asyncio.run(run())


@pytest.mark.parametrize("action", ("history", "bonuses", "connect", "connection_platform"))
def test_other_customer_actions_read_local_projection_without_panel_refresh(action):
    async def run():
        app, service, _, reads = tracked_setup()
        sid = service.subscriptions[0]["id"]
        await app.execute("101", AID, action, {"sid": sid}, None, "")
        assert reads and all(refresh is False for refresh, _ in reads)

    asyncio.run(run())


def test_profile_entry_requests_authoritative_panel_refresh():
    async def run():
        app, _, _, reads = tracked_setup()
        await app.execute("101", AID, "profile", {}, None, "")
        assert reads == [(True, None)]

    asyncio.run(run())


@pytest.mark.parametrize(
    "action,intent",
    [("periods", "renew"), ("products", "upgrade"), ("products", "downgrade"), ("device_quote", "devices")],
)
def test_operation_entry_refreshes_selected_subscription_before_choices(action, intent):
    async def run():
        app, service, session, reads = tracked_setup()
        original = service.dashboard
        sid = service.subscriptions[0]["id"]

        def dashboard(account_id, **kwargs):
            result = original(account_id, **kwargs)
            if kwargs.get("refresh_panel"):
                service.subscriptions[0].update(product_id="universal", devices_limit=9)
            return result

        service.dashboard = dashboard
        await app.execute(
            "101", AID, action, {"sid": sid, "intent": intent, "product_id": "standard"}, None, ""
        )
        assert reads == ([(True, sid), (False, None)] if action == "device_quote" else [(True, sid)])
        if action == "periods":
            assert "Универсальный" in session.calls[-1].text
        if intent == "upgrade":
            assert "Стандартный" not in session.calls[-1].text
        if action == "device_quote":
            choices = [item.data for item in app.actions.values() if item.data.get("intent") == "devices"]
            assert choices and all(value["product_id"] == "universal" for value in choices)

    asyncio.run(run())


def test_operation_entry_rejects_failed_panel_refresh_without_stale_choices():
    from sunrise_core.errors import DomainError

    async def run():
        app, service, _, reads = tracked_setup()
        service.subscriptions[0].update(panel_sync_state="stale", renewal_pricing_state="blocked")
        sid = service.subscriptions[0]["id"]
        with pytest.raises(DomainError) as failure:
            await app.execute(
                "101", AID, "periods", {"sid": sid, "intent": "renew", "product_id": "standard"}, None, ""
            )
        assert failure.value.code == "panel_sync_required"
        assert reads == [(True, sid)]

    asyncio.run(run())


@pytest.mark.parametrize("due", (True, False, None))
def test_help_warning_uses_server_indicator_only(due):
    async def run():
        app, service, session, _ = tracked_setup()
        sub = service.subscriptions[0]
        sub.update(connection_help_due=due, expires_at="2037-01-01T00:00:00Z")
        await app.execute("101", AID, "subscription", {"sid": sub["id"]}, None, "")
        text = session.calls[-1].text
        assert ("первого подключения пока нет" in text) is (due is True)
        assert ("Откройте инструкцию подключения" in text) is (due is True)

    asyncio.run(run())


def test_start_reads_no_dashboard_or_push():
    async def run():
        app, service, _, reads = tracked_setup()
        await app.main("101", {"id": AID, "email_verified": False})
        assert reads == []
        assert not any(call[0] == "notify" for call in service.calls)

    asyncio.run(run())


def test_deleted_subscription_notice_and_archived_agreement_have_no_active_target_actions():
    async def run():
        app, service, session, _ = tracked_setup()
        service.subscriptions = []
        original = service.dashboard
        sid = "archived-test"
        service.dashboard = lambda *args, **kwargs: (
            original(*args, **kwargs)
            | {
                "subscription_deletion_notices": [
                    {
                        "subscription_id": sid,
                        "name": "Удалённая подписка",
                        "deleted_at": "2026-10-11T12:00:00Z",
                        "reason": "manual_panel",
                    }
                ],
                "archived_subscription_agreements": [
                    {
                        "subscription_id": sid,
                        "name": "Удалённая подписка",
                        "recurring": {
                            "state": "active",
                            "amount": "100.00",
                            "currency": "RUB",
                            "next_charge_at": None,
                        },
                    }
                ],
            }
        )
        await app.execute("101", AID, "subscriptions", {}, None, "")
        assert "Подписка была удалена" in session.calls[-1].text
        buttons = [b for row in session.calls[-1].reply_markup.inline_keyboard for b in row]
        assert any("Автопродление удалённых подписок" in b.text for b in buttons)
        await app.execute("101", AID, "archived_agreement", {"sid": sid}, None, "")
        assert "100,00" in session.calls[-1].text
        assert not any(
            "Продлить" in b.text or "Устройства" in b.text
            for row in session.calls[-1].reply_markup.inline_keyboard
            for b in row
        )
        await app.execute("101", AID, "cancel_confirm", {"sid": sid}, None, "")
        assert "Подтвердить отмену" in str(session.calls[-1].reply_markup)
        await app.execute("101", AID, "cancel", {"sid": sid}, None, "")
        assert ("cancel", AID, sid) in service.calls
        buttons = [b for row in session.calls[-1].reply_markup.inline_keyboard for b in row]
        destination = app.actions[next(b.callback_data for b in buttons if "Подписки" in b.text)]
        assert destination.action == "subscriptions"

    asyncio.run(run())


@pytest.mark.parametrize(
    "actions",
    [
        None,
        {},
        [{"target": "/#subscriptions", "operation": []}],
        [{"target": "/#subscriptions", "operation": {}}],
    ],
)
def test_malformed_notification_actions_are_ignored(actions):
    from sunrise_bot.copy import notification_actions

    assert notification_actions({"actions": actions}) == []


def test_node_news_only_renders_safe_feed_without_notification_delivery():
    async def run():
        app, service, session, reads = tracked_setup()
        calls = []

        def news(account_id, *, before=None, limit=20):
            calls.append((account_id, before, limit))
            return {
                "items": [
                    {
                        "id": "local-news",
                        "title": "Тестовый узел",
                        "state": "recovered",
                        "occurred_at": "2026-10-11T12:00:00Z",
                        "internal_uuid": "hidden-internal",
                        "ip": "hidden-ip",
                    }
                ],
                "next_cursor": "opaque-more",
            }

        service.node_news = news
        await app.execute("101", AID, "node_news", {}, None, "")
        assert calls == [(AID, None, 5)]
        assert "Тестовый узел" in session.calls[-1].text
        assert "Работа восстановлена" in session.calls[-1].text
        assert "hidden-" not in session.calls[-1].text
        assert reads == []

    asyncio.run(run())


def test_notification_renewal_action_uses_authoritative_offer_and_normal_order_confirmation(monkeypatch):
    async def run():
        from types import SimpleNamespace

        app, service, session, reads = tracked_setup()
        sid = service.subscriptions[0]["id"]
        item = {
            "id": "local-note",
            "title": "Срок подписки",
            "body": "Проверьте продление",
            "actions": [
                {
                    "label": "Продлить выбранную подписку",
                    "target": "/#subscriptions",
                    "operation": "renew",
                    "metadata": {"subscription_id": sid, "quote_id": "saved-quote"},
                }
            ],
        }

        class Inbox:
            def __init__(self, *_):
                pass

            def list(self, _, limit=5, *, offset=0):
                assert limit == 5 and offset == 0
                return {"items": [item], "unread_count": 1}

        monkeypatch.setattr("sunrise_infrastructure.notifications.NotificationService", Inbox)
        app.accounts = SimpleNamespace(db=None, settings=SimpleNamespace(admin_email_allowlist=[]))
        await app.execute("101", AID, "notification", {"id": item["id"]}, None, "")
        buttons = [b for row in session.calls[-1].reply_markup.inline_keyboard for b in row]
        action = next(
            (app.actions[b.callback_data] for b in buttons if "Продлить выбранную подписку" in b.text), None
        )
        assert action is not None
        assert action.action == "renewal_offer"
        assert action.data == {"sid": sid, "quote_id": "saved-quote"}
        offers = []

        def offer(account_id, target, *, quote_id=None):
            offers.append((account_id, target, quote_id))
            return service.quote(
                account_id, product_id="standard", action="renew", subscription_id=sid, bonus_amount="0.00"
            ) | {
                "period_days": 90,
                "total": "123.45",
                "devices_limit": 7,
                "selected_secondary": {"source": "renewal_campaign", "percent": "20"},
            }

        service.renewal_offer = offer
        await app.execute("101", AID, action.action, action.data, None, "")
        assert offers == [(AID, sid, "saved-quote")]
        assert "90 дней" in session.calls[-1].text
        assert "123,45" in session.calls[-1].text
        assert "Кампания продления: 20%" in session.calls[-1].text
        assert not any(call[0] == "create" for call in service.calls)
        assert all(refresh is False for refresh, _ in reads)

    asyncio.run(run())


def test_first_connection_and_finite_traffic_use_server_projection_and_clear_unknown_warning():
    async def run():
        app, service, session, _ = tracked_setup()
        sub = service.subscriptions[0]
        sub.update(
            connection_help_due=False,
            ever_connected=True,
            first_connected_at="2026-10-11T12:00:00Z",
            traffic_progress_percent="81.5",
            traffic_warning=True,
        )
        await app.execute("101", AID, "subscription", {"sid": sub["id"]}, None, "")
        assert "Первое подключение подтверждено" in session.calls[-1].text
        assert "81.5%" in session.calls[-1].text
        assert "Лимит трафика почти исчерпан" in session.calls[-1].text
        assert "первого подключения пока нет" not in session.calls[-1].text
        sub.update(
            ever_connected=False,
            first_connected_at=None,
            traffic_progress_percent=None,
            traffic_warning=False,
        )
        await app.execute("101", AID, "subscription", {"sid": sub["id"]}, None, "")
        assert "Первое подключение подтверждено" not in session.calls[-1].text
        assert "Лимит трафика почти исчерпан" not in session.calls[-1].text
        assert "81.5%" not in session.calls[-1].text

    asyncio.run(run())


@pytest.mark.parametrize(
    "code,expected",
    [
        ("panel_capacity_unpriced", "Лимит устройств требует проверки"),
        ("panel_product_unverified", "Тариф панели требует проверки"),
        ("panel_access_restricted", "Доступ к подписке ограничен"),
    ],
)
def test_blocked_operation_keeps_actual_cause_and_main_menu_usable(code, expected):
    from sunrise_core.errors import DomainError

    async def run():
        app, service, session, reads = tracked_setup()
        sub = service.subscriptions[0]
        sub.update(panel_sync_state="fresh", renewal_pricing_state="blocked", renewal_pricing_error=code)
        with pytest.raises(DomainError) as failure:
            await app.execute("101", AID, "periods", {"sid": sub["id"], "intent": "renew"}, None, "")
        assert failure.value.code == code
        assert expected in failure.value.message
        assert reads == [(True, sub["id"])]
        from test_dispatcher import message_update

        await app.dispatcher.feed_update(app.bot, message_update(text="/start"))
        assert session.calls[-1].reply_markup.inline_keyboard
        assert reads == [(True, sub["id"])]

    asyncio.run(run())


@pytest.mark.parametrize("reason", ["expiry_cleanup", "manual_panel", "verified_panel_absence"])
def test_deletion_notice_uses_exact_reason(reason):
    async def run():
        app, service, session, _ = tracked_setup()
        original = service.dashboard
        service.dashboard = lambda *args, **kwargs: (
            original(*args, **kwargs)
            | {
                "subscription_deletion_notices": [
                    {
                        "subscription_id": "removed",
                        "name": "Удалённая",
                        "reason": reason,
                        "deleted_at": "2026-10-22T00:00:00Z",
                    }
                ]
            }
        )
        await app.execute("101", AID, "subscriptions", {}, None, "")
        text = session.calls[-1].text
        assert (
            "Ваша подписка удалена, поскольку после окончания её срока действия прошло 7 дней." in text
        ) is (reason == "expiry_cleanup")
        assert ("Подписка была удалена. Если это ошибка" in text) is (reason != "expiry_cleanup")

    asyncio.run(run())


def test_server_authorized_finite_renewal_does_not_infer_device_count():
    async def run():
        app, service, session, reads = tracked_setup()
        sub = service.subscriptions[0]
        sub.update(
            status="expired",
            devices_limit=None,
            renewal_pricing_state="blocked",
            renewal_pricing_error="panel_capacity_unpriced",
            renewal_device_limit_required=True,
        )
        await app.execute("101", AID, "periods", {"sid": sub["id"], "intent": "renew"}, None, "")
        assert "Новый срок будет рассчитан с конечным лимитом устройств по тарифу" in session.calls[-1].text
        assert reads == [(True, sub["id"])]
        assert not any(call[0] == "create" for call in service.calls)

    asyncio.run(run())


@pytest.mark.parametrize("inbound", ["message", "callback", "foreign_chat", "group"])
def test_only_fresh_private_owner_activity_resumes_telegram_delivery(inbound):
    from test_dispatcher import callback_update, find_button, message_update

    async def run():
        app, service, session, _ = tracked_setup()
        resumed = []
        service.resume_telegram_notifications = lambda aid, telegram_id: resumed.append((aid, telegram_id))
        if inbound == "callback":
            await app.dispatcher.feed_update(app.bot, message_update())
            resumed.clear()
            update = callback_update(find_button(session, "Подписки"))
        else:
            update = message_update()
            if inbound == "foreign_chat":
                update = update.model_copy(
                    update={
                        "message": update.message.model_copy(
                            update={"chat": update.message.chat.model_copy(update={"id": 102})}
                        )
                    }
                )
            if inbound == "group":
                update = update.model_copy(
                    update={
                        "message": update.message.model_copy(
                            update={"chat": update.message.chat.model_copy(update={"type": "group"})}
                        )
                    }
                )
        await app.dispatcher.feed_update(app.bot, update)
        assert resumed == ([(AID, 101)] if inbound in {"message", "callback"} else [])

    asyncio.run(run())


def test_device_menu_entry_refreshes_owned_subscription_once_before_inventory():
    async def run():
        app, service, _, reads = tracked_setup()
        sid = service.subscriptions[0]["id"]
        await app.execute("101", AID, "devices", {"sid": sid}, None, "")
        assert reads == [(True, sid)]

    asyncio.run(run())


def test_blocked_rotation_hides_duplicate_controls_and_reports_review():
    from test_v7_device_parity import PanelOperations

    async def run():
        app, service, session, _ = tracked_setup()
        app.customer_operations = PanelOperations()
        sub = service.subscriptions[0]
        sub["credential_rotation_state"] = "blocked"
        await app.execute("101", AID, "devices", {"sid": sub["id"]}, None, "")
        assert "требуется ручная проверка" in session.calls[-1].text
        buttons = [b.text for row in session.calls[-1].reply_markup.inline_keyboard for b in row]
        assert not any("Перевыпустить ссылку" in label for label in buttons)

    asyncio.run(run())
