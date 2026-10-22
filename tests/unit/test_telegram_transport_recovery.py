"""Telegram outcomes are classified without retaining provider descriptions."""

import json
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest
from sunrise_core.errors import DomainError
from sunrise_infrastructure.notifications import NotificationService


def deliver(monkeypatch, responses, payload=None):
    calls = []

    def handle(request):
        if request.url.path.endswith("/getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"id": 123}})
        calls.append(json.loads(request.content))
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return httpx.Response(response[0], json=response[1])

    original = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(handle)))
    service = NotificationService(
        None,
        SimpleNamespace(
            bot_test_token="123:synthetic",  # noqa: S106 -- synthetic MockTransport only
            test_bot_ids="123",
            dashboard_url="https://account.example.test",
        ),
    )
    service._deliver(
        "telegram", "456", payload or {"title": "Заголовок", "body": "Текст", "nodes": []}, "test"
    )
    return calls


@pytest.mark.parametrize(
    "status,description,code",
    [
        (403, "Forbidden: bot was blocked by the user", "telegram_recipient_blocked"),
        (403, "Forbidden: user is deactivated", "telegram_recipient_deactivated"),
        (403, "Forbidden: insufficient rights", "telegram_forbidden"),
        (400, "Bad Request: chat not found", "telegram_chat_not_found"),
        (401, "Unauthorized", "telegram_auth_failed"),
        (404, "Not Found", "telegram_endpoint_invalid"),
        (400, "Bad Request: invalid button", "telegram_rejected"),
    ],
)
def test_safe_specific_refusal(monkeypatch, status, description, code):
    with pytest.raises(DomainError) as error:
        deliver(monkeypatch, [(status, {"ok": False, "error_code": status, "description": description})])
    assert error.value.code == code
    assert description not in str(error.value)


@pytest.mark.parametrize(
    "status,data",
    [
        (502, {"ok": False, "error_code": 502}),
        (429, {"ok": False, "error_code": 429, "parameters": {"retry_after": 17}}),
    ],
)
def test_definite_temporary_failure_has_bounded_delay(monkeypatch, status, data):
    with pytest.raises(DomainError) as error:
        deliver(monkeypatch, [(status, data)])
    assert error.value.code in {"telegram_temporary_failure", "telegram_rate_limited"}
    assert 1 <= error.value.seconds <= 3600


@pytest.mark.parametrize(
    "status,data",
    [
        (502, {}),
        (403, {"ok": False, "error_code": 400}),
        (200, {"ok": True}),
        (200, []),
        (429, {"ok": False, "error_code": 429}),
    ],
)
def test_incomplete_or_inconsistent_outcome_is_unknown(monkeypatch, status, data):
    with pytest.raises(DomainError) as error:
        deliver(monkeypatch, [(status, data)])
    assert error.value.code == "telegram_delivery_unknown"


def test_format_rejection_fallback_keeps_links_and_buttons(monkeypatch):
    calls = deliver(
        monkeypatch,
        [
            (400, {"ok": False, "error_code": 400, "description": "Bad Request: can't parse entities"}),
            (200, {"ok": True, "result": {"message_id": 1}}),
        ],
        {
            "title": "Документы",
            "body": "Оферта",
            "nodes": [
                {
                    "type": "link",
                    "href": "https://example.test/offer",
                    "children": [{"type": "text", "text": "Оферта"}],
                },
            ],
            "actions": [{"label": "Открыть кабинет", "target": "/#account"}],
        },
    )
    assert len(calls) == 2
    assert "parse_mode" not in calls[1]
    assert "Оферта" in calls[1]["text"] and "https://example.test/offer" in calls[1]["text"]
    assert calls[1]["reply_markup"] == calls[0]["reply_markup"]


def test_timeout_does_not_trigger_plain_fallback(monkeypatch):
    responses = [httpx.ReadTimeout("unknown")]
    with pytest.raises(httpx.ReadTimeout):
        deliver(monkeypatch, responses)
    assert responses == []


@pytest.mark.parametrize(
    "current_id,alias,allowed", [("456", False, True), ("789", False, False), ("456", True, False)]
)
def test_resume_requires_current_canonical_identity(current_id, alias, allowed):
    account = SimpleNamespace(id=12, telegram_id=current_id, telegram_notifications_blocked=True)
    records = []

    class Session:
        def scalar(self, query):
            return account

        def get(self, model, identifier):
            return object() if alias and model.__name__ == "Alias" else None

        def add(self, record):
            records.append(record)

    class DB:
        @contextmanager
        def transaction(self):
            yield Session()

    service = NotificationService(DB(), SimpleNamespace())
    if allowed:
        assert service.resume_telegram(12, "456") == {"status": "resumed"}
        assert account.telegram_notifications_blocked is False
        assert records[0].key == "telegram-activity:12"
        assert len(records[0].value["generation"]) == 36
    else:
        with pytest.raises(DomainError) as error:
            service.resume_telegram(12, "456")
        assert error.value.status == 404
        assert account.telegram_notifications_blocked is True


def test_bot_identity_auth_failure_never_attempts_send(monkeypatch):
    calls = []

    def handle(request):
        calls.append(request.method)
        return httpx.Response(401, json={"ok": False, "error_code": 401, "description": "Unauthorized"})

    original = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(handle)))
    service = NotificationService(None, SimpleNamespace(bot_test_token="123:synthetic", test_bot_ids="123"))  # noqa: S106 -- synthetic transport only
    with pytest.raises(DomainError) as error:
        service._deliver("telegram", "456", {"title": "Текст", "body": "Текст", "nodes": []}, "auth")
    assert error.value.code == "telegram_auth_failed"
    assert calls == ["GET"]


def test_format_failure_after_fallback_does_not_send_a_third_time(monkeypatch):
    responses = [
        (400, {"ok": False, "error_code": 400, "description": "Bad Request: can't parse entities"})
    ] * 2
    with pytest.raises(DomainError) as error:
        deliver(monkeypatch, responses)
    assert error.value.code == "telegram_format_rejected"
    assert responses == []


def test_expanded_plain_links_are_never_truncated_to_fit(monkeypatch):
    responses = [(400, {"ok": False, "error_code": 400, "description": "Bad Request: can't parse entities"})]
    with pytest.raises(DomainError) as error:
        deliver(
            monkeypatch,
            responses,
            {
                "title": "Условия",
                "body": "Условия",
                "nodes": [
                    {
                        "type": "link",
                        "href": "https://example.test/" + "x" * 1000,
                        "children": [{"type": "text", "text": "Документы"}],
                    }
                    for _ in range(5)
                ],
            },
        )
    assert error.value.code == "telegram_format_rejected"
    assert responses == []
