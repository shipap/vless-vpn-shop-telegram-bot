from email import policy
from email.parser import BytesParser

import pytest
from sunrise_infrastructure.mail import SMTPTestMail


class FakeSMTP:
    def __init__(self):
        self.events = []
        self.messages = []
        self._host = "smtp.example.test"

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def ehlo(self):
        self.events.append("ehlo")

    def starttls(self, *, context):
        assert context.check_hostname and self._host == "smtp.example.test"
        self.events.append("tls")

    def login(self, username, password):
        assert username == "synthetic-user" and password == "synthetic-password"  # noqa: S105 -- fake SMTP only
        self.events.append("login")

    def send_message(self, message, **kwargs):
        self.messages.append((BytesParser(policy=policy.default).parsebytes(message.as_bytes()), kwargs))
        return {}


def transport():
    from sunrise_infrastructure.instance_config import InstanceConfig

    smtp = FakeSMTP()
    mail = SMTPTestMail(
        host="smtp.example.test",
        port=587,
        mode="starttls",
        sender="shop@example.test",
        username="synthetic-user",
        password="synthetic-password",  # noqa: S106 -- synthetic credentials for fake SMTP only
        allowed_hosts={"smtp.example.test"},
        allowed_recipients={"payer@example.test"},
        authorized=True,
        factory=lambda **_: smtp,
        instance=InstanceConfig(app_name="Северный VPN", visual_profile="private"),
    )
    return mail, smtp


def test_login_email_has_readable_plain_and_accessible_russian_html_with_same_code():
    mail, smtp = transport()
    mail.deliver(
        "payer@example.test",
        "Код входа Северный VPN",
        "Код входа: 123456\nСрок действия: 10 минут.",
        "challenge-template",
    )
    message, envelope = smtp.messages[0]
    assert message.get_content_type() == "multipart/alternative"
    plain = message.get_body(preferencelist=("plain",)).get_content()
    html = message.get_body(preferencelist=("html",)).get_content()
    assert "123456" in plain and "123456" in html
    assert "10 минут" in plain and "10 минут" in html
    assert "Никому не сообщайте" in plain and "Никому не сообщайте" in html
    assert "123456" not in str(message["Subject"])
    assert '<html lang="ru">' in html and "<code" in html
    assert 'role="presentation"' in html and "max-width:600px" in html
    assert message["From"].addresses[0].display_name == "Северный VPN"
    assert envelope == {"from_addr": "shop@example.test", "to_addrs": ["payer@example.test"]}
    assert smtp.events == ["ehlo", "tls", "ehlo", "login"]
    assert message.get_body(preferencelist=("html",)).get_content_charset() == "utf-8"


def test_payment_notification_keeps_business_text_without_invented_access_state_or_link():
    mail, smtp = transport()
    body = "Оплата получена. Состояние подписки доступно в личном кабинете."
    mail.deliver("payer@example.test", "Сообщение Северный VPN", body, "notify-template")
    message = smtp.messages[0][0]
    plain = message.get_body(preferencelist=("plain",)).get_content()
    html = message.get_body(preferencelist=("html",)).get_content()
    assert body in plain and body in html
    assert "Доступ выдан" not in html and "href=" not in html
    assert "Оплата получена" in html


def test_broadcast_dynamic_content_is_escaped_and_has_no_active_html_or_remote_assets():
    mail, smtp = transport()
    body = 'Первая строка\n\n<img src="https://attacker.test/pixel" onerror="alert(1)"><script>alert(1)</script> & данные'
    mail.deliver("payer@example.test", "Новость <unsafe> & Северный VPN", body, "broadcast-template")
    message = smtp.messages[0][0]
    html = message.get_body(preferencelist=("html",)).get_content()
    plain = message.get_body(preferencelist=("plain",)).get_content()
    assert plain.replace("\r\n", "\n").startswith(body)
    wire = message.as_bytes(policy=policy.SMTP)
    assert b"\n" not in wire.replace(b"\r\n", b"")
    assert "&lt;img" in html and "&lt;script&gt;" in html and "&amp; данные" in html
    assert "Новость &lt;unsafe&gt; &amp; Северный VPN" in html
    assert "<img" not in html and "<script" not in html
    assert "display:none" not in html and "@import" not in html and "<link" not in html


def test_mail_retry_keeps_one_stable_message_id_and_original_recipient():
    mail, smtp = transport()
    for _ in range(2):
        mail.deliver(
            "payer@example.test", "Сообщение Северный VPN", "Сервисное уведомление", "notification-retry"
        )
    assert [str(message["Message-ID"]) for message, _ in smtp.messages] == [
        "<notification-retry@example.test>",
        "<notification-retry@example.test>",
    ]
    assert all(str(message["To"]) == "payer@example.test" for message, _ in smtp.messages)


@pytest.mark.parametrize("subject", ["Тема\r\nBcc: attacker@example.test", "Тема\nНовая строка"])
def test_header_injection_is_rejected_before_smtp(subject):
    mail, smtp = transport()
    with pytest.raises(ValueError):
        mail.deliver("payer@example.test", subject, "Текст", "safe-delivery")
    assert smtp.events == [] and smtp.messages == []


def test_telegram_link_code_has_a_distinct_html_confirmation_template():
    from sunrise_infrastructure.mail_templates import render_mail

    mail = render_mail(
        "Подтверждение привязки Telegram",
        "Код подтверждения привязки Telegram: 123456\nСрок действия: 10 минут.",
        "no-reply@example.test",
    )
    assert '<code aria-label="Код подтверждения"' in mail.html
    assert "Введите этот код в окне привязки Telegram" in mail.html
    assert "Подтвердите привязку Telegram" in mail.html
    assert "Никому не сообщайте код" in mail.plain
    assert "странице входа" not in mail.html
