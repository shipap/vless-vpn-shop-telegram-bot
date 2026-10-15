import pytest
from sunrise_core.errors import DomainError
from sunrise_infrastructure import mail

from tests.contract.test_production_transports import SyntheticPolicy, SyntheticSMTP


def test_scoped_mail_accepts_any_auth_recipient_but_never_notifications():
    assert hasattr(mail, "SMTPScopedAuthMail")
    policy = SyntheticPolicy()
    policy.production = False
    policy.scoped_payments = True
    smtp = SyntheticSMTP()
    transport = mail.SMTPScopedAuthMail(
        runtime_policy=policy, username="synthetic", password="synthetic", factory=lambda **_: smtp  # noqa: S106 -- фиктивный SMTP
    )
    for email in ("one@domain.invalid", "two@other.invalid"):
        transport.deliver(
            email, "Код входа", "123456", "own-challenge", capability="auth_mail", before_send=lambda: None
        )
    assert len(smtp.messages) == 2
    with pytest.raises(DomainError):
        transport.deliver(
            "one@domain.invalid",
            "Рассылка",
            "Текст",
            "broadcast",
            capability="notice_email",
            before_send=lambda: None,
        )
    assert len(smtp.messages) == 2
    smtp.on_login = lambda: setattr(policy, "allowed", False)
    with pytest.raises(DomainError):
        transport.deliver(
            "one@domain.invalid",
            "Код",
            "123456",
            "own-challenge",
            capability="auth_mail",
            before_send=lambda: None,
        )
    assert len(smtp.messages) == 2
