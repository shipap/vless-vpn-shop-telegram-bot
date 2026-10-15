import ssl

import pytest
from sunrise_core.errors import DomainError
from sunrise_infrastructure.mail import SMTPTestMail, configured_mail
from sunrise_infrastructure.settings import Settings

SYNTHETIC = "synthetic-password"


def test_smtp_approved_addresses_match_normalized_login_input():
    settings = smtp_settings(email_allowlist=["Owner@Example.invalid"])
    assert settings.email_allowlist == ["owner@example.invalid"]
    mail = configured_mail(settings, factory=lambda **_: pytest.fail("No network"))
    assert mail.recipients == {"owner@example.invalid"}


def smtp_settings(**overrides):
    values = dict(
        _env_file=None,
        database_url="postgresql+psycopg://synthetic:synthetic@127.0.0.1:55439/sunrise_test_backend",
        secret_key="s" * 48,
        local_test=True,
        email_allowlist=["payer@example.test", "owner@example.invalid"],
        smtp_test_authorized=True,
        smtp_host="smtp.example.invalid",
        smtp_port=587,
        smtp_mode="starttls",
        smtp_sender="shop@example.invalid",
        smtp_username="synthetic-user",
        smtp_password=SYNTHETIC,
        smtp_allowed_hosts=["smtp.example.invalid"],
        smtp_tunnel_port=None,
    )
    return Settings(**(values | overrides))


def test_smtp_local_real_recipient_requires_complete_explicit_authorization():
    settings = smtp_settings()
    assert settings.local_test and settings.smtp_test_authorized
    assert settings.smtp_tunnel_port is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"smtp_test_authorized": False},
        {"smtp_allowed_hosts": []},
        {"smtp_allowed_hosts": ["*", "smtp.example.invalid"]},
        {"smtp_allowed_hosts": ["*.example.invalid", "smtp.example.invalid"]},
        {"smtp_host": "other.example.invalid"},
        {"smtp_username": ""},
        {"smtp_password": ""},
        {"smtp_sender": ""},
        {"smtp_mode": "plain"},
        {"smtp_port": 25},
        {"email_allowlist": ["*@example.invalid"]},
        {"email_allowlist": ["owner@*.invalid"]},
        {"smtp_tunnel_port": 1023},
        {"smtp_tunnel_port": 65536},
        {"smtp_tunnel_port": 1587, "local_test": False},
        {"smtp_tunnel_port": 1465, "smtp_mode": "smtps", "smtp_port": 465},
    ],
)
def test_smtp_local_real_recipient_rejects_incomplete_or_broad_configuration(overrides):
    with pytest.raises(ValueError):
        smtp_settings(**overrides)


def test_smtp_configured_transport_excludes_synthetic_recipients():
    mail = configured_mail(smtp_settings(), factory=lambda **_: pytest.fail("No network"))
    assert mail.recipients == {"owner@example.invalid"}
    with pytest.raises(DomainError) as error:
        mail.deliver("payer@example.test", "Code", "123456", "synthetic-code")
    assert error.value.code == "mail_unavailable"
    assert configured_mail(smtp_settings(email_allowlist=["payer@example.test"])) is None


def test_smtp_disabled_synthetic_local_settings_stay_valid():
    settings = smtp_settings(
        smtp_test_authorized=False,
        email_allowlist=["payer@example.test"],
        smtp_tunnel_port=None,
    )
    assert configured_mail(settings) is None


def test_smtp_live_transport_rejects_synthetic_destination_without_connecting():
    with pytest.raises(ValueError):
        SMTPTestMail(
            host="smtp.example.invalid",
            port=587,
            mode="starttls",
            sender="shop@example.invalid",
            username="synthetic-user",
            password=SYNTHETIC,
            allowed_hosts=["smtp.example.invalid"],
            allowed_recipients=["payer@example.test"],
            authorized=True,
        )


def test_smtp_tunnel_keeps_original_verified_tls_hostname():
    calls = []

    class SMTP:
        _host = "127.0.0.1"

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def ehlo(self):
            calls.append("ehlo")

        def starttls(self, *, context):
            assert self._host == "smtp.example.invalid"
            assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
            calls.append("verified_tls")

        def login(self, username, password):
            assert (username, password) == ("synthetic-user", "synthetic-password")
            calls.append("login")

        def send_message(self, message, **kwargs):
            assert kwargs == {"from_addr": "shop@example.invalid", "to_addrs": ["owner@example.invalid"]}
            calls.append("send")
            return {}

    def factory(**kwargs):
        assert kwargs == {"host": "127.0.0.1", "port": 1587, "timeout": 10}
        return SMTP()

    mail = configured_mail(smtp_settings(smtp_tunnel_port=1587), factory=factory)
    mail.deliver("owner@example.invalid", "Code", "123456", "tunnel-code")
    assert calls == ["ehlo", "verified_tls", "ehlo", "login", "send"]
