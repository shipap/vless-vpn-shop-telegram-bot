from types import SimpleNamespace

import httpx
import pytest
from sunrise_infrastructure.notifications import NotificationService


@pytest.mark.parametrize("dashboard_url", [None, "https://account.example.test"])
def test_telegram_security_action_is_url_button_without_mutation(monkeypatch, dashboard_url):
    sent = []

    def handle(request):
        import json

        if request.url.path.endswith("/getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"id": 123456}})
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    client = httpx.Client(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client)
    config = SimpleNamespace(
        bot_test_token="123456:synthetic",  # noqa: S106 -- MockTransport-only synthetic bot identity
        test_bot_ids="123456",
        dashboard_url=dashboard_url,
    )
    service = NotificationService(None, config)
    service._deliver(
        "telegram",
        "123",
        {
            "title": "Аккаунты связаны",
            "body": "Подтвердите отвязку, если это были не вы.",
            "nodes": [],
            "actions": [{"label": "Отвязать Telegram", "target": "/#account"}],
        },
        "notice-1",
    )
    if dashboard_url is None:
        assert "reply_markup" not in sent[0]
    else:
        assert sent[0]["reply_markup"] == {
            "inline_keyboard": [[{"text": "Отвязать Telegram", "url": "https://account.example.test/#account"}]]
        }
    assert sent[0]["link_preview_options"]["is_disabled"] is True


@pytest.mark.parametrize("dashboard_url", [None, False, 123, [], "", "javascript:alert(1)"])
def test_optional_or_invalid_dashboard_keeps_only_verified_checkout(dashboard_url):
    service = NotificationService(None, SimpleNamespace(dashboard_url=dashboard_url))
    payment = "https://pay.rollypay.io/synthetic-checkout"
    assert service._action_links({
        "checkout_url": payment,
        "actions": [{"label": "К подпискам", "target": "/#subscriptions"}],
    }) == [{"label": "Оплатить", "url": payment}]


def test_missing_dashboard_does_not_add_relative_action_urls():
    service = NotificationService(None, SimpleNamespace())
    assert service._action_links({"actions": [{"label": "Аккаунт", "target": "/#account"}]}) == []
