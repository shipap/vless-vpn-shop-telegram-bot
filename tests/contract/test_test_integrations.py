import importlib
import json

import httpx
import pytest


def implementation(module, name):
    try:
        value = getattr(importlib.import_module(module), name, None)
    except ModuleNotFoundError:
        value = None
    assert value is not None, f"Missing bounded integration: {name}"
    return value


UUID = "10000000-0000-4000-8000-000000000001"
SYNTHETIC = "synthetic-credential-placeholder"
SQUAD = "20000000-0000-4000-8000-000000000002"
BINDING = {"uuid": UUID, "short_uuid": "synthetic_short", "username": "sunrise_test_user"}
TARGET = {"expires_at": "2027-01-01T00:00:00+00:00", "devices_limit": 6, "internal_squad_uuid": SQUAD}


def panel(handler, **overrides):
    cls = implementation("sunrise_infrastructure.panel", "RemnawaveTest")
    args = dict(
        base_url="https://panel.example.test",
        token=SYNTHETIC,
        contract="2.8_uuid",
        authorized=True,
        allowed_hosts={"panel.example.test"},
        allowed_uuids={UUID},
        allowed_short_uuids={"synthetic_short"},
        allowed_usernames={"sunrise_test_user"},
        allowed_squads={SQUAD},
        allowed_hwids={"synthetic_hwid"},
        bindings={"synthetic_subscription": BINDING},
        transport=httpx.MockTransport(handler),
        allowed_subscription_hosts={"connect.example.test"},
    )
    return cls(**(args | overrides))


def user(**values):
    return {
        "uuid": UUID,
        "shortUuid": BINDING["short_uuid"],
        "username": BINDING["username"],
        "expireAt": TARGET["expires_at"],
        "hwidDeviceLimit": 6,
        "activeInternalSquads": [{"uuid": SQUAD}],
        "status": "ACTIVE",
        "subscriptionUrl": "https://connect.example.test/subscription/synthetic_short",
    } | values


def test_panel_absolute_target_and_identity_preservation():
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "PATCH":
            body = json.loads(request.content)
            assert body == {
                "uuid": UUID,
                "expireAt": TARGET["expires_at"],
                "hwidDeviceLimit": 6,
                "activeInternalSquads": [SQUAD],
                "status": "ACTIVE",
            }
        return httpx.Response(200, json={"response": user()})

    p = panel(handler)
    result = p.apply(BINDING, TARGET, create=False)
    assert result["uuid"] == UUID and result["short_uuid"] == "synthetic_short"
    assert [r.method for r in calls] == ["GET", "PATCH", "GET"]


@pytest.mark.parametrize(
    "response",
    [
        [],
        {"response": user(activeInternalSquads=None)},
        {"response": user(activeInternalSquads=[None])},
        {"response": user(expireAt=7)},
    ],
)
def test_panel_malformed_response_is_a_controlled_review(response):
    from sunrise_infrastructure.panel import PanelReview

    p = panel(lambda _: httpx.Response(200, json=response))
    with pytest.raises(PanelReview):
        p.apply(BINDING, TARGET)


def test_panel_malformed_device_entry_is_a_controlled_review():
    from sunrise_infrastructure.panel import PanelReview

    p = panel(
        lambda request: httpx.Response(
            200, json={"response": {"devices": [None]} if "hwid" in request.url.path else user()}
        )
    )
    with pytest.raises(PanelReview):
        p.revoke(BINDING, "synthetic_hwid")


def test_emoji_malformed_json_root_returns_fallback():
    cls = implementation("sunrise_infrastructure.emoji", "TelegramEmojiAssets")
    credential = "123456:synthetic_test_token"
    assets = cls(
        token=credential,
        approved_bot_id="123456",
        authorized=True,
        allowed_ids={"9"},
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=[])),
    )
    assert assets.get("9") is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"authorized": False},
        {"contract": "3_numeric"},
        {"allowed_hosts": set()},
        {"base_url": "http://panel.example.test"},
        {"base_url": "https://evil.test"},
        {"token": ""},
        {"allowed_uuids": set()},
        {"allowed_squads": set()},
    ],
)
def test_panel_default_deny_configuration(overrides):
    with pytest.raises(ValueError):
        panel(lambda _: pytest.fail("No network allowed"), **overrides)


def test_panel_denies_unknown_identity_squad_and_hwid_without_request():
    p = panel(lambda _: pytest.fail("No network allowed"))
    for binding, target in [
        (BINDING | {"uuid": SQUAD}, TARGET),
        (BINDING, TARGET | {"internal_squad_uuid": UUID}),
    ]:
        with pytest.raises(ValueError):
            p.apply(binding, target, create=False)
    with pytest.raises(ValueError):
        p.revoke(BINDING, "customer_device")


def test_panel_create_uncertainty_only_reconciles_and_never_blind_posts():
    calls = []

    def handler(request):
        calls.append(request.method)
        return httpx.Response(404)

    p = panel(handler)
    with pytest.raises(Exception) as exc:
        p.apply(BINDING, TARGET, create=True, uncertain=True)
    assert type(exc.value).__name__ == "PanelReview"
    assert calls == ["GET"]


class FakeSMTP:
    def __init__(self):
        self.events = []
        self.messages = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def ehlo(self):
        self.events.append("ehlo")

    def starttls(self, *, context):
        assert context.check_hostname
        self.events.append("tls")

    def login(self, username, password):
        self.events.append("login")

    def send_message(self, message, **kwargs):
        self.messages.append((message, kwargs))
        return {}


def test_smtp_shared_transport_tls_and_recipient_boundary():
    cls = implementation("sunrise_infrastructure.mail", "SMTPTestMail")
    smtp = FakeSMTP()
    mail = cls(
        host="smtp.example.test",
        port=587,
        mode="starttls",
        sender="shop@example.test",
        username="test",
        password=SYNTHETIC,
        allowed_hosts={"smtp.example.test"},
        allowed_recipients={"payer@example.test"},
        authorized=True,
        factory=lambda **_: smtp,
    )
    mail.deliver("payer@example.test", "Код входа", "123456", "challenge-test")
    assert smtp.events == ["ehlo", "tls", "ehlo", "login"]
    assert smtp.messages[0][0]["To"] == "payer@example.test"
    from email.utils import parsedate_to_datetime

    sent_at = parsedate_to_datetime(smtp.messages[0][0]["Date"])
    assert sent_at.utcoffset().total_seconds() == 0
    assert "123456" in smtp.messages[0][0].get_body(preferencelist=("plain",)).get_content()
    with pytest.raises(ValueError):
        mail.deliver("private@customer.invalid", "Subject", "Body", "denied")
    assert len(smtp.messages) == 1


def test_emoji_asset_static_bounded_and_cached():
    cls = implementation("sunrise_infrastructure.emoji", "TelegramEmojiAssets")
    calls = []
    content = b"\x89PNG\r\n\x1a\n" + b"static-fixture"

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("getCustomEmojiStickers"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": [
                        {
                            "custom_emoji_id": "90071992547409930001",
                            "file_id": "synthetic_file",
                            "type": "custom_emoji",
                            "is_animated": False,
                            "is_video": False,
                        }
                    ],
                },
            )
        if request.url.path.endswith("getFile"):
            return httpx.Response(
                200,
                json={"ok": True, "result": {"file_path": "stickers/test.png", "file_size": len(content)}},
            )
        return httpx.Response(200, content=content, headers={"Content-Type": "image/png"})

    credential = "123456:synthetic_test_token"
    assets = cls(
        token=credential,
        approved_bot_id="123456",
        authorized=True,
        allowed_ids={"90071992547409930001"},
        transport=httpx.MockTransport(handler),
    )
    assert assets.get("90071992547409930001") == (content, "image/png")
    assert assets.get("90071992547409930001") == (content, "image/png")
    assert len(calls) == 3
    for denied in ["1", "١٢", "-1", "1/../../", "1" * 33]:
        assert assets.get(denied) is None
    assert len(calls) == 3


def test_emoji_rejects_unapproved_bot_and_host():
    cls = implementation("sunrise_infrastructure.emoji", "TelegramEmojiAssets")
    for overrides in [{"approved_bot_id": "7"}, {"authorized": False}, {"base_url": "https://evil.test"}]:
        with pytest.raises(ValueError):
            credential = "123456:synthetic_test_token"
            cls(
                **(
                    dict(
                        token=credential,
                        approved_bot_id="123456",
                        authorized=True,
                        allowed_ids={"9"},
                        transport=httpx.MockTransport(lambda _: pytest.fail("No network")),
                    )
                    | overrides
                )
            )


def test_panel_confirmed_connection_url_is_protected_and_host_bounded():
    link = "https://connect.example.test/subscription/synthetic_short"
    p = panel(lambda _: httpx.Response(200, json={"response": user(subscriptionUrl=link)}))
    p.subscription_hosts = {"connect.example.test"}
    result = p.apply(BINDING, TARGET)
    assert result.get("connection_url") == link, (
        "Verified subscription URL must be returned for protected storage"
    )


def test_emoji_busy_fetch_returns_fallback_without_waiting():
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    cls = implementation("sunrise_infrastructure.emoji", "TelegramEmojiAssets")
    started, release = threading.Event(), threading.Event()

    def handler(_):
        started.set()
        assert release.wait(2)
        return httpx.Response(404)

    credential = "123456:synthetic_test_token"
    assets = cls(
        token=credential,
        approved_bot_id="123456",
        authorized=True,
        allowed_ids={"9", "10"},
        transport=httpx.MockTransport(handler),
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(assets.get, "9")
        assert started.wait(1)
        start = time.monotonic()
        assert assets.get("10") is None
        assert time.monotonic() - start < 0.15
        release.set()
        assert first.result() is None
    assert len(assets.cache) <= len(assets.ids)


def test_emoji_transport_logs_never_expose_bot_credential(caplog):
    import logging

    cls = implementation("sunrise_infrastructure.emoji", "TelegramEmojiAssets")
    credential = "123456:synthetic_test_token"
    assets = cls(
        token=credential,
        approved_bot_id="123456",
        authorized=True,
        allowed_ids={"9"},
        transport=httpx.MockTransport(lambda _: httpx.Response(404)),
    )
    with caplog.at_level(logging.INFO, logger="httpx"):
        assert assets.get("9") is None
    assert credential not in caplog.text


def test_smtp_failure_has_no_sensitive_exception_details():
    from sunrise_core.errors import DomainError

    cls = implementation("sunrise_infrastructure.mail", "SMTPTestMail")

    def failure(**_):
        raise OSError("synthetic credential and private address")

    mail = cls(
        host="smtp.example.test",
        port=465,
        mode="smtps",
        sender="shop@example.test",
        allowed_hosts={"smtp.example.test"},
        allowed_recipients={"payer@example.test"},
        authorized=True,
        factory=failure,
    )
    with pytest.raises(DomainError) as error:
        mail.deliver("payer@example.test", "Код входа", "123456", "challenge-test")
    assert error.value.code == "mail_unavailable"
    assert "credential" not in str(error.value)
    with pytest.raises(ValueError):
        mail.deliver("payer@example.test", "Subject\nBcc: private@customer.invalid", "Body", "denied")


@pytest.mark.parametrize(
    "url",
    [
        "http://connect.example.test/sub",
        "https://evil.test/sub",
        "https://user:password@connect.example.test/sub",
        None,
    ],
)
def test_panel_connection_url_denies_unsafe_or_missing_confirmed_links(url):
    p = panel(lambda _: httpx.Response(200, json={"response": user(subscriptionUrl=url)}))
    from sunrise_infrastructure.panel import PanelReview, PanelUnavailable

    with pytest.raises((PanelReview, PanelUnavailable)):
        p.apply(BINDING, TARGET)


@pytest.mark.parametrize(
    "mime,content",
    [
        ("text/html", b"<html>bad</html>"),
        ("image/svg+xml", b"<svg onload='bad'/>"),
        ("image/png", b"<html>bad</html>"),
        ("image/png", b"\x89PNG\r\n\x1a\nacTL"),
        ("image/png", b"x" * (256 * 1024 + 1)),
    ],
    ids=["html", "svg", "fake_png", "animated_png", "oversize"],
)
def test_emoji_active_wrong_animated_and_oversized_payloads_are_not_served(mime, content):
    cls = implementation("sunrise_infrastructure.emoji", "TelegramEmojiAssets")

    def handler(request):
        if request.url.path.endswith("getCustomEmojiStickers"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": [
                        {
                            "custom_emoji_id": "9",
                            "file_id": "f",
                            "type": "custom_emoji",
                            "is_animated": False,
                            "is_video": False,
                        }
                    ],
                },
            )
        if request.url.path.endswith("getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "stickers/test.png"}})
        return httpx.Response(200, content=content, headers={"Content-Type": mime})

    credential = "123456:synthetic_test_token"
    assets = cls(
        token=credential,
        approved_bot_id="123456",
        authorized=True,
        allowed_ids={"9"},
        transport=httpx.MockTransport(handler),
    )
    assert assets.get("9") is None
