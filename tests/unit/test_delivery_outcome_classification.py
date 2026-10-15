import smtplib
from types import SimpleNamespace

import pytest
from sunrise_core.errors import DomainError
from sunrise_infrastructure.mail import SMTPTestMail
from sunrise_infrastructure.notifications import NotificationService


@pytest.mark.parametrize(
    "exception,code",
    [
        (smtplib.SMTPRecipientsRefused({"to@example.test": (550, b"rejected")}), "mail_rejected"),
        (smtplib.SMTPDataError(554, b"rejected"), "mail_rejected"),
        (TimeoutError("ambiguous outcome"), "mail_delivery_unknown"),
    ],
)
def test_smtp_preserves_definitive_refusal_without_claiming_timeout_rejected(exception, code):
    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def ehlo(self):
            pass

        def starttls(self, **kwargs):
            pass

        def send_message(self, *args, **kwargs):
            raise exception

    mail = SMTPTestMail(
        host="smtp.example.test",
        port=587,
        mode="starttls",
        sender="from@example.test",
        allowed_hosts=["smtp.example.test"],
        allowed_recipients=["to@example.test"],
        authorized=True,
        factory=lambda **kwargs: Connection(),
    )
    with pytest.raises(DomainError) as caught:
        mail.deliver("to@example.test", "Сообщение", "Текст", "classification")
    assert caught.value.code == code


@pytest.mark.parametrize(
    "status,data,code",
    [
        (400, {"ok": False, "error_code": 400}, "telegram_rejected"),
        (403, {"ok": False, "error_code": 403}, "telegram_forbidden"),
        (502, {"ok": False, "error_code": 502}, "telegram_delivery_unknown"),
    ],
)
def test_telegram_preserves_explicit_refusal_and_ambiguous_server_failure(monkeypatch, status, data, code):
    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, *args):
            return SimpleNamespace(json=lambda: {"ok": True, "result": {"id": 123}})

        def post(self, *args, **kwargs):
            return SimpleNamespace(status_code=status, json=lambda: data)

    monkeypatch.setattr("sunrise_infrastructure.notifications.httpx.Client", Client)
    service = NotificationService(None, SimpleNamespace(bot_test_token="123:synthetic", test_bot_ids="123"))  # noqa: S106 -- transport-double fixture only
    with pytest.raises(DomainError) as caught:
        service._deliver("telegram", "456", {"title": "Заголовок", "body": "Текст", "nodes": []}, "outcome")
    assert caught.value.code == code
