"""Русские инструкции фактической страницы подписки без переноса HTML и cookies."""

import hashlib
import ipaddress
import json
import re
import socket
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from html.parser import HTMLParser
from threading import Lock
from time import monotonic
from urllib.parse import urlsplit

import httpx

PLATFORMS = ("ios", "android", "windows", "macos", "linux", "appleTV", "androidTV")
LIMIT = 512 * 1024
DNS_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="subscription-dns")


def safe_https(value, hosts=None):
    if not isinstance(value, str) or len(value) > 2048 or "{{" in value:
        return False
    try:
        url = urlsplit(value)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.port not in {None, 443}
            or any(ord(c) <= 32 for c in value)
            or (hosts is not None and url.hostname not in hosts)
        ):
            return False
        try:
            return ipaddress.ip_address(url.hostname).is_global
        except ValueError:
            return True
    except ValueError:
        return False


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "svg", "iframe", "object", "template"}:
            self.hidden += 1
        if not self.hidden and tag in {"br", "p", "div", "li"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "svg", "iframe", "object", "template"}:
            self.hidden = max(0, self.hidden - 1)
        elif not self.hidden and tag in {"p", "div", "li"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain(value, limit):
    if not isinstance(value, str):
        return ""
    parser = PlainText()
    try:
        parser.feed(value[:10000])
    except AssertionError:
        raise ValueError("guide_html_invalid") from None
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f\u202a-\u202e\u2066-\u2069]", "", "".join(parser.parts))
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())[:limit]


def russian(value, limit=2000):
    return plain(value.get("ru"), limit) if isinstance(value, dict) else ""


def normalize_guides(config):
    if (
        not isinstance(config, dict)
        or config.get("version") != "1"
        or not isinstance(config.get("platforms"), dict)
    ):
        return []
    result = []
    for pid in PLATFORMS:
        platform = config["platforms"].get(pid)
        if not isinstance(platform, dict) or not isinstance(platform.get("apps"), list):
            continue
        apps = []
        for app in platform["apps"][:8]:
            if not isinstance(app, dict) or not isinstance(app.get("blocks"), list):
                continue
            steps = []
            for block in app["blocks"][:8]:
                if not isinstance(block, dict):
                    continue
                title, description = russian(block.get("title"), 160), russian(block.get("description"))
                if not title and not description:
                    continue
                links = []
                buttons = block.get("buttons", [])
                for button in buttons[:5] if isinstance(buttons, list) else []:
                    if not isinstance(button, dict) or button.get("type") != "external":
                        continue
                    label, link = russian(button.get("text"), 100), button.get("link")
                    if label and safe_https(link):
                        links.append({"text": label, "url": link})
                steps.append({"title": title, "description": description, "links": links})
            name = plain(app.get("name"), 100)
            if name and steps:
                apps.append({"name": name, "steps": steps})
        if apps:
            result.append({"id": pid, "name": russian(platform.get("displayName"), 60) or pid, "apps": apps})
    return result


def public_dns(hostname):
    # Некоторые локальные VPN возвращают Fake-IP из 198.18/15. Запрашивается только
    # публичный разрешённый домен: путь/личная ссылка и cookies в DNS не передаются.
    deadline = monotonic() + 2
    with httpx.Client(timeout=2, trust_env=False, follow_redirects=False) as client:
        with client.stream(
            "GET",
            "https://1.1.1.1/dns-query",
            params={"name": hostname, "type": "A"},
            headers={"Accept": "application/dns-json", "Accept-Encoding": "identity"},
        ) as response:
            if (
                response.status_code != 200
                or response.headers.get("content-encoding", "identity") != "identity"
            ):
                raise ValueError("subscription_dns_unavailable")
            data = bytearray()
            for chunk in response.iter_raw():
                if len(data) + len(chunk) > 8192 or monotonic() > deadline:
                    raise ValueError("subscription_dns_invalid")
                data.extend(chunk)
    value = json.loads(data)
    if not isinstance(value, dict) or value.get("Status") != 0 or not isinstance(value.get("Answer"), list):
        raise ValueError("subscription_dns_invalid")
    return [
        item["data"]
        for item in value["Answer"]
        if isinstance(item, dict) and item.get("type") == 1 and isinstance(item.get("data"), str)
    ]


class PublicSubscriptionTransport(httpx.BaseTransport):
    """Закрепляет публичный DNS-адрес, сохраняя проверку сертификата исходного домена."""

    def __init__(self, hosts, delegate=None):
        self.hosts = frozenset(hosts)
        self.delegate = delegate or httpx.HTTPTransport(trust_env=False, retries=0)
        self.resolved = {}

    def handle_request(self, request):
        if not safe_https(str(request.url), self.hosts):
            raise ValueError("subscription_origin_denied")
        hostname = request.url.host
        ips = self.resolved.get(hostname)
        if ips is None:
            lookup = DNS_POOL.submit(socket.getaddrinfo, hostname, 443, type=socket.SOCK_STREAM)
            try:
                addresses = lookup.result(timeout=2)
            except FutureTimeout:
                lookup.cancel()
                raise ValueError("subscription_dns_timeout") from None
            ips = [item[4][0] for item in addresses]
            if ips and all(ipaddress.ip_address(ip) in ipaddress.ip_network("198.18.0.0/15") for ip in ips):
                ips = public_dns(hostname)
        if not ips or any(
            not ipaddress.ip_address(ip).is_global or ipaddress.ip_address(ip).is_multicast for ip in ips
        ):
            raise ValueError("subscription_ip_denied")
        self.resolved[hostname] = ips
        headers = request.headers.copy()
        headers["Host"] = hostname
        forwarded = httpx.Request(
            request.method,
            request.url.copy_with(host=ips[0]),
            headers=headers,
            stream=request.stream,
            extensions={**request.extensions, "sni_hostname": hostname},
        )
        return self.delegate.handle_request(forwarded)

    def close(self):
        self.delegate.close()


class SubscriptionInstructions:
    def __init__(self, hosts, transport=None):
        self.hosts = frozenset(hosts) - {"*"}
        self.transport = transport
        self.cache, self.lock = OrderedDict(), Lock()

    @staticmethod
    def read(client, url, accept, deadline):
        with client.stream("GET", url, headers={"Accept": accept}) as response:
            if (
                response.status_code != 200
                or response.headers.get("content-encoding", "identity") != "identity"
            ):
                raise ValueError("subscription_page_unavailable")
            chunks, size = [], 0
            for chunk in response.iter_raw():
                size += len(chunk)
                if size > LIMIT or monotonic() > deadline:
                    raise ValueError("subscription_page_too_large")
                chunks.append(chunk)
            return b"".join(chunks)

    def get(self, url):
        if not safe_https(url, self.hosts):
            return []
        parsed = urlsplit(url)
        if parsed.query or parsed.fragment or not parsed.path or parsed.path == "/":
            return []
        key = hashlib.sha256(url.encode()).digest()
        with self.lock:
            cached = self.cache.get(key)
            if cached and cached[0] > monotonic():
                self.cache.move_to_end(key)
                return cached[1]
        try:
            with httpx.Client(
                transport=self.transport or PublicSubscriptionTransport(self.hosts),
                timeout=3,
                follow_redirects=False,
                trust_env=False,
                headers={"User-Agent": "Mozilla/5.0 VPNSubscriptionGuide", "Accept-Encoding": "identity"},
            ) as client:
                deadline = monotonic() + 10
                self.read(client, url, "text/html", deadline)
                raw = self.read(
                    client,
                    f"https://{parsed.hostname}/assets/.app-config-v2.json",
                    "application/json",
                    deadline,
                )
                guides = normalize_guides(json.loads(raw))
        except (httpx.HTTPError, ValueError, OSError, UnicodeError, RecursionError):
            guides = []
        with self.lock:
            self.cache[key] = (monotonic() + (300 if guides else 30), guides)
            self.cache.move_to_end(key)
            while len(self.cache) > 128:
                self.cache.popitem(last=False)
        return guides
