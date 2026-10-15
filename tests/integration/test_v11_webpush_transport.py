import base64
import secrets

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sunrise_infrastructure.notifications import NotificationService
from test_v11_notifications import notification_context as notification_context


def encoded(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def test_actual_webpush_encrypts_and_signs_without_redirect_or_proxy(notification_context, monkeypatch):
    db, settings, (owner, _, _) = notification_context
    private = ec.generate_private_key(ec.SECP256R1())
    settings.push_private_key = encoded(
        private.private_bytes(
            serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    settings.push_subject = "mailto:support@example.test"
    settings.push_test_authorized = True
    client_key = ec.generate_private_key(ec.SECP256R1())
    public = client_key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    calls = []

    def send(session, url, **kwargs):
        assert session.trust_env is False
        assert kwargs["allow_redirects"] is False
        assert url == "https://fcm.googleapis.com/synthetic-v11"
        calls.append(kwargs)
        result = requests.Response()
        result.status_code = 201
        result._content = b""
        return result

    monkeypatch.setattr(requests.Session, "post", send)
    service = NotificationService(db, settings, telegram_transport=lambda *args: None)
    service.register_push(
        owner,
        {
            "endpoint": "https://fcm.googleapis.com/synthetic-v11",
            "keys": {"p256dh": encoded(public), "auth": encoded(secrets.token_bytes(16))},
        },
    )
    with db.transaction() as session:
        service.emit_in(session, owner, "native-v11", "test", "Synthetic push", "Private sample body")
    assert service.deliver_pending() == 2
    assert len(calls) == 1
    headers = {key.lower(): value for key, value in calls[0]["headers"].items()}
    assert headers["content-encoding"] == "aes128gcm"
    assert headers["authorization"].startswith("vapid t=")
    assert b"Private sample body" not in calls[0]["data"]
    assert len(calls[0]["data"]) > 100
    assert service.deliver_pending() == 0
    assert len(calls) == 1
