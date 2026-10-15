import json
import socket

import httpx
import pytest
from sunrise_infrastructure import subscription_instructions as module
from sunrise_infrastructure.subscription_instructions import (
    PublicSubscriptionTransport,
    SubscriptionInstructions,
    normalize_guides,
)


def config(description="Откройте приложение.<br>Добавьте подписку."):
    return {
        "version": "1",
        "platforms": {
            "android": {
                "displayName": {"ru": "Android"},
                "apps": [
                    {
                        "name": "Happ",
                        "blocks": [
                            {
                                "title": {"ru": "Подключение"},
                                "description": {"ru": description},
                                "buttons": [
                                    {
                                        "type": "external",
                                        "text": {"ru": "Скачать"},
                                        "link": "https://store.example.test/app",
                                    },
                                    {
                                        "type": "subscriptionLink",
                                        "text": {"ru": "Добавить"},
                                        "link": "happ://add/{{SUBSCRIPTION_LINK}}",
                                    },
                                ],
                            }
                        ],
                    }
                ],
            }
        },
    }


def test_russian_guides_strip_executable_html_preserve_paragraphs_and_download_links():
    guides = normalize_guides(
        config("<p>Первый шаг.</p><script>steal()</script><style>hidden</style><p>Второй <b>шаг</b>.</p>")
    )
    assert guides[0]["id"] == "android"
    step = guides[0]["apps"][0]["steps"][0]
    assert step["description"] == "Первый шаг.\nВторой шаг."
    assert step["links"] == [{"text": "Скачать", "url": "https://store.example.test/app"}]
    assert "<" not in json.dumps(guides, ensure_ascii=False)


@pytest.mark.parametrize(
    "link",
    [
        "javascript:alert(1)",
        "https://user:password@store.example.test/app",
        "http://store.example.test/app",
        "https://store.example.test/{{USERNAME}}",
        "https://127.0.0.1/app",
    ],
)
def test_unsafe_download_actions_are_removed(link):
    value = config()
    value["platforms"]["android"]["apps"][0]["blocks"][0]["buttons"][0]["link"] = link
    assert normalize_guides(value)[0]["apps"][0]["steps"][0]["links"] == []


def test_unrecognized_schema_and_missing_russian_steps_do_not_invent_a_guide():
    assert normalize_guides({"version": "2", "platforms": config()["platforms"]}) == []
    value = config()
    value["platforms"]["android"]["apps"][0]["blocks"][0]["description"] = {"en": "English only"}
    value["platforms"]["android"]["apps"][0]["blocks"][0]["title"] = {"en": "English"}
    assert normalize_guides(value) == []


def test_loader_uses_actual_page_cookie_and_fixed_same_origin_asset_without_panel_token():
    seen = []

    def handle(request):
        seen.append(request)
        assert "authorization" not in request.headers
        if len(seen) == 1:
            assert request.url.path == "/custom/synthetic"
            return httpx.Response(
                200,
                headers={"set-cookie": "page-session=synthetic; Path=/; Secure"},
                stream=httpx.ByteStream(b"<div id='sbpg'></div>"),
            )
        assert request.url.path == "/assets/.app-config-v2.json"
        assert request.headers["cookie"] == "page-session=synthetic"
        return httpx.Response(200, stream=httpx.ByteStream(json.dumps(config()).encode()))

    loader = SubscriptionInstructions({"subscription.example.test"}, transport=httpx.MockTransport(handle))
    value = loader.get("https://subscription.example.test/custom/synthetic")
    assert value[0]["apps"][0]["name"] == "Happ"
    assert loader.get("https://subscription.example.test/custom/synthetic") == value
    assert len(seen) == 2


@pytest.mark.parametrize(
    "url",
    [
        "http://subscription.example.test/a",
        "https://evil.example.test/a",
        "https://subscription.example.test:444/a",
        "https://x@subscription.example.test/a",
        "https://subscription.example.test/a?token=b",
        "https://subscription.example.test/a#fragment",
    ],
)
def test_loader_denies_unapproved_urls_before_network(url):
    loader = SubscriptionInstructions(
        {"subscription.example.test"},
        transport=httpx.MockTransport(lambda _: pytest.fail("denied URL reached network")),
    )
    assert loader.get(url) == []


@pytest.mark.parametrize(
    "status,body",
    [(302, "redirect"), (500, "error"), (200, "not JSON"), (200, "x" * (512 * 1024 + 1))],
    ids=["redirect", "unavailable", "invalid-json", "oversize"],
)
def test_config_failures_fall_back_without_following_redirects(status, body):
    seen = []

    def handle(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(200, stream=httpx.ByteStream(b"page"))
        return httpx.Response(
            status, stream=httpx.ByteStream(body.encode()), headers={"location": "https://evil.example.test/"}
        )

    loader = SubscriptionInstructions({"subscription.example.test"}, transport=httpx.MockTransport(handle))
    assert loader.get("https://subscription.example.test/synthetic") == []
    assert len(seen) == 2


def test_pinned_transport_rejects_private_dns_and_never_connects(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))],
    )
    transport = PublicSubscriptionTransport(
        {"subscription.example.test"},
        delegate=httpx.MockTransport(lambda _: pytest.fail("private IP reached network")),
    )
    with pytest.raises(ValueError):
        transport.handle_request(httpx.Request("GET", "https://subscription.example.test/a"))


def test_pinned_transport_uses_resolved_public_ip_with_original_tls_sni_and_host(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )
    seen = []
    transport = PublicSubscriptionTransport(
        {"subscription.example.test"},
        delegate=httpx.MockTransport(lambda request: seen.append(request) or httpx.Response(200)),
    )
    transport.handle_request(httpx.Request("GET", "https://subscription.example.test/a"))
    assert seen[0].url.host == "93.184.216.34"
    assert seen[0].headers["host"] == "subscription.example.test"
    assert seen[0].extensions["sni_hostname"] == "subscription.example.test"


def test_fake_ip_dns_uses_fixed_public_resolver_without_allowing_virtual_address(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.1.211", 443))],
    )
    monkeypatch.setattr(module, "public_dns", lambda hostname: ["93.184.216.34"])
    seen = []
    transport = PublicSubscriptionTransport(
        {"subscription.example.test"},
        delegate=httpx.MockTransport(lambda request: seen.append(request) or httpx.Response(200)),
    )
    transport.handle_request(httpx.Request("GET", "https://subscription.example.test/a"))
    assert seen[0].url.host == "93.184.216.34"


def test_public_resolver_private_result_is_still_denied(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.1.211", 443))],
    )
    monkeypatch.setattr(module, "public_dns", lambda hostname: ["10.0.0.1"])
    transport = PublicSubscriptionTransport(
        {"subscription.example.test"},
        delegate=httpx.MockTransport(lambda _: pytest.fail("private resolved address reached network")),
    )
    with pytest.raises(ValueError):
        transport.handle_request(httpx.Request("GET", "https://subscription.example.test/a"))


def test_slow_small_chunks_stop_at_total_deadline_before_buffering_the_whole_response(monkeypatch):
    clock, received = [0], []
    monkeypatch.setattr(module, "monotonic", lambda: clock[0])

    class SlowStream(httpx.SyncByteStream):
        def __iter__(self):
            for index in range(20):
                clock[0] += 6
                received.append(index)
                yield b"x"

    loader = SubscriptionInstructions(
        {"subscription.example.test"},
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=SlowStream())),
    )
    assert loader.get("https://subscription.example.test/a") == []
    assert received == [0, 1]


def test_malformed_html_in_valid_config_falls_back_instead_of_crashing():
    seen = []

    def handle(request):
        seen.append(request)
        body = b"page" if len(seen) == 1 else json.dumps(config("<![bogus]>")).encode()
        return httpx.Response(200, stream=httpx.ByteStream(body))

    loader = SubscriptionInstructions({"subscription.example.test"}, transport=httpx.MockTransport(handle))
    assert loader.get("https://subscription.example.test/a") == []
