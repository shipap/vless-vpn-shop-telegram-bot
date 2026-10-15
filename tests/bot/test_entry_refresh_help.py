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
