from types import SimpleNamespace

from sunrise_infrastructure.accounts import Accounts
from sunrise_infrastructure.instance_config import InstanceConfig
from sunrise_infrastructure.mail import SMTPTestMail
from sunrise_infrastructure.mail_templates import render_mail

from tests.contract.test_mail_templates import FakeSMTP
from tests.e2e.local_auth_server import SyntheticSettings


def test_default_login_template_is_neutral_in_subject_html_and_plain():
    result = render_mail(
        "Код входа VLESS VPN Shop", "Код входа: 123456\nСрок действия: 10 минут.", "sender@example.test"
    )
    assert result.subject == "Код входа VLESS VPN Shop"
    assert "VLESS VPN Shop" in result.html and "VLESS VPN Shop" in result.plain
    assert "sunrise" not in result.html + result.plain + result.subject
    assert "123456" in result.html and "123456" in result.plain


def test_otp_subject_uses_configured_brand_before_transport():
    config = SyntheticSettings(
        _env_file=None,
        instance_config_path=None,
        database_url="postgresql+psycopg://synthetic:synthetic@127.0.0.1:55439/sunrise_test_backend",
        secret_key="synthetic-instance-mail-key-" * 2,
        local_test=True,
        app_name="Другой магазин",
        email_allowlist=["payer@example.test"],
    )
    deliveries = []
    transport = SimpleNamespace(deliver=lambda *args: deliveries.append(args))
    Accounts(None, None, config, mail_transport=transport)._deliver_code(
        "synthetic-challenge", "123456", "payer@example.test"
    )
    assert deliveries == [
        (
            "payer@example.test",
            "Код входа Другой магазин",
            "Код входа: 123456\nСрок действия: 10 минут.",
            "synthetic-challenge",
        )
    ]


def test_second_brand_sender_and_template_are_escaped_in_real_mime_message():
    smtp = FakeSMTP()
    instance = InstanceConfig(app_name='Other <img src="bad">', sender_name="Другая поддержка")
    mail = SMTPTestMail(
        host="smtp.example.test",
        port=587,
        mode="starttls",
        sender="sender@example.test",
        allowed_hosts={"smtp.example.test"},
        allowed_recipients={"payer@example.test"},
        authorized=True,
        factory=lambda **_: smtp,
        instance=instance,
    )
    mail.deliver("payer@example.test", "Уведомление", "Сервисное сообщение", "synthetic-brand-mail")
    message = smtp.messages[0][0]
    assert message["From"].addresses[0].display_name == "Другая поддержка"
    html = message.get_body(preferencelist=("html",)).get_content()
    assert "Other &lt;img" in html
    assert "<img" not in html and "sunrise" not in html
    assert "Другая поддержка" in message.get_body(preferencelist=("plain",)).get_content()
