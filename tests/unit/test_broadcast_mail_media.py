"""Реальное MIME-письмо и локальный файл; заменяется только внешняя SMTP-сеть."""

import io
import smtplib
from email import policy
from pathlib import Path

import pytest
from PIL import Image
from sunrise_core.errors import DomainError
from sunrise_infrastructure.broadcast_media import BroadcastMediaStore, MediaLimits
from sunrise_infrastructure.mail import SMTPTestMail


class SMTPDouble:
    def __init__(self, *, size=None, failure=None, exit_failure=None):
        self.esmtp_features = {} if size is None else {"size": size}
        self.failure = failure
        self.exit_failure = exit_failure
        self.wire = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if self.exit_failure:
            raise self.exit_failure
        return False

    def ehlo(self):
        pass

    def starttls(self, **kwargs):
        pass

    def send_message(self, message, *, from_addr, to_addrs):
        assert from_addr == "from@example.test" and to_addrs == ["to@example.test"]
        self.wire.append(message.as_bytes(policy=policy.SMTP))
        if self.failure:
            raise self.failure
        return {}


def mail(smtp, **kwargs):
    return SMTPTestMail(
        host="smtp.example.test",
        port=587,
        mode="starttls",
        sender="from@example.test",
        allowed_hosts=["smtp.example.test"],
        allowed_recipients=["to@example.test"],
        authorized=True,
        factory=lambda **_: smtp,
        **kwargs,
    )


@pytest.fixture(params=["image/png", "image/jpeg", "video/mp4"])
def media(tmp_path, request):
    image = Image.new("RGB", (8, 8), "red")
    content = io.BytesIO()
    image.save(content, format="JPEG" if request.param == "image/jpeg" else "PNG")
    store = BroadcastMediaStore(tmp_path / "media", signing_key=b"synthetic-media-key-for-tests-only")
    encoded = (
        (Path(__file__).parents[1] / "fixtures/broadcast-chromium-synthetic.mp4").read_bytes()
        if request.param == "video/mp4"
        else content.getvalue()
    )
    reference = store.upload([encoded], mime=request.param)
    return store, reference


def test_smtp_timeout_after_send_is_not_automatically_retryable():
    smtp = SMTPDouble(failure=TimeoutError("uncertain transport outcome"))
    with pytest.raises(DomainError) as caught:
        mail(smtp).deliver("to@example.test", "Фото", "Текст", "uncertain")
    assert caught.value.code == "mail_delivery_unknown"
    assert caught.value.retryable is False
    assert len(smtp.wire) == 1


def test_confirmed_acceptance_survives_quit_connection_failure():
    smtp = SMTPDouble(exit_failure=smtplib.SMTPServerDisconnected("QUIT disconnected"))
    mail(smtp).deliver("to@example.test", "Сообщение", "Текст", "accepted")
    assert len(smtp.wire) == 1


def test_media_is_one_mime_attachment_without_losing_plain_html_or_headers(media):
    from email.parser import BytesParser

    store, reference = media
    smtp = SMTPDouble()
    mail(smtp, media_store=store).deliver("to@example.test", "Фото", "Текст", "photo", media=reference)
    message = BytesParser(policy=policy.default).parsebytes(smtp.wire[0])
    attachments = list(message.iter_attachments())
    assert len(attachments) == 1
    assert attachments[0].get_content_type() == reference["mime"]
    assert attachments[0].get_payload(decode=True) == store.read(reference)
    suffix = {"image/png": ".png", "image/jpeg": ".jpg", "video/mp4": ".mp4"}[reference["mime"]]
    assert attachments[0].get_filename().endswith(suffix)
    assert "Текст" in message.get_body(preferencelist=("plain",)).get_content()
    assert message.get_body(preferencelist=("html",)).get_content_type() == "text/html"
    assert message["Message-ID"] == "<photo@example.test>"


def test_missing_store_denies_media_instead_of_sending_text_only(media):
    _, reference = media
    smtp = SMTPDouble()
    with pytest.raises(DomainError) as caught:
        mail(smtp).deliver("to@example.test", "Фото", "Текст", "missing", media=reference)
    assert caught.value.code == "media_unavailable"
    assert smtp.wire == []


@pytest.mark.parametrize("advertised", ["10", "invalid", "-1", "999999999999999999999"])
def test_smtp_size_is_enforced_before_send(media, advertised):
    store, reference = media
    smtp = SMTPDouble(size=advertised)
    with pytest.raises(DomainError):
        mail(smtp, media_store=store).deliver("to@example.test", "Фото", "Текст", "size", media=reference)
    assert smtp.wire == []


def test_local_limit_applies_to_whole_encoded_email(media):
    store, reference = media
    store.limits = MediaLimits(email_bytes=200)
    smtp = SMTPDouble(size="999999")
    with pytest.raises(DomainError) as caught:
        mail(smtp, media_store=store).deliver("to@example.test", "Фото", "Текст", "local", media=reference)
    assert caught.value.code == "media_email_too_large"
    assert smtp.wire == []


def test_changed_file_never_sends_a_different_attachment(media):
    store, reference = media
    (store.root / reference["asset_id"]).write_bytes(b"changed")
    smtp = SMTPDouble()
    with pytest.raises(DomainError) as caught:
        mail(smtp, media_store=store).deliver("to@example.test", "Фото", "Текст", "changed", media=reference)
    assert caught.value.code == "media_unavailable"
    assert smtp.wire == []


def test_wire_overhead_cannot_bypass_smtp_limit_using_raw_attachment_size(media):
    store, reference = media
    assert reference["size_bytes"] < 1500
    smtp = SMTPDouble(size="1500")
    with pytest.raises(DomainError) as caught:
        mail(smtp, media_store=store).deliver("to@example.test", "Фото", "Текст", "overhead", media=reference)
    assert caught.value.code == "media_email_too_large"
    assert smtp.wire == []


def test_connection_failure_before_send_remains_retryable():
    smtp = SMTPDouble()
    transport = mail(smtp)

    def unavailable(**kwargs):
        raise TimeoutError("no connection established")

    transport.factory = unavailable
    with pytest.raises(DomainError) as caught:
        transport.deliver("to@example.test", "Сообщение", "Текст", "before")
    assert caught.value.code == "mail_unavailable"
    assert caught.value.retryable is True
    assert smtp.wire == []


def test_unapproved_recipient_cannot_send_media(media):
    store, reference = media
    smtp = SMTPDouble()
    with pytest.raises(DomainError):
        mail(smtp, media_store=store).deliver(
            "other@example.test", "Фото", "Текст", "denied", media=reference
        )
    assert smtp.wire == []


@pytest.mark.parametrize("revoked", [False, True])
def test_fresh_authorization_after_login_controls_actual_send(media, revoked):
    store, reference = media
    steps = []

    class LoginRace(SMTPDouble):
        def ehlo(self):
            steps.append("ehlo")

        def starttls(self, **kwargs):
            steps.append("tls")

        def login(self, username, password):
            assert (username, password) == ("synthetic", "synthetic-password")
            steps.append("login")

        def send_message(self, message, **kwargs):
            steps.append("send")
            return super().send_message(message, **kwargs)

    smtp = LoginRace()

    def authorize():
        assert steps == ["ehlo", "tls", "ehlo", "login"]
        steps.append("authorize")
        if revoked:
            raise DomainError("staff_permission_denied", "Доступ отозван.", 403)

    transport = mail(
        smtp,
        username="synthetic",
        password="synthetic-password",  # noqa: S106 -- только SMTPDouble, без сетевого соединения
        media_store=store,
    )
    if revoked:
        with pytest.raises(DomainError) as caught:
            transport.deliver(
                "to@example.test", "Фото", "Текст", "revoked", media=reference, before_send=authorize
            )
        assert caught.value.code == "staff_permission_denied"
        assert smtp.wire == []
        assert steps[-1] == "authorize"
    else:
        transport.deliver(
            "to@example.test", "Фото", "Текст", "authorized", media=reference, before_send=authorize
        )
        assert steps[-2:] == ["authorize", "send"]
        assert len(smtp.wire) == 1
