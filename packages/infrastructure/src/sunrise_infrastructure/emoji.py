"""Статические emoji разрешённого тестового бота, без внешних URL в ответе браузеру."""

import json
import logging
import re
import time
from threading import Lock

import httpx


class _TelegramCredentialFilter(logging.Filter):
    def filter(self, record):
        record.msg = re.sub(
            r"https://api\.telegram\.org/(?:file/)?bot[^/\s]+/",
            "https://api.telegram.org/bot[REDACTED]/",
            record.getMessage(),
        )
        record.args = ()
        return True


_transport_logger = logging.getLogger("httpx")
if not any(isinstance(item, _TelegramCredentialFilter) for item in _transport_logger.filters):
    _transport_logger.addFilter(_TelegramCredentialFilter())


class TelegramEmojiAssets:
    max_bytes = 256 * 1024

    def __init__(
        self,
        *,
        token,
        approved_bot_id,
        authorized=False,
        allowed_ids=(),
        transport=None,
        base_url="https://api.telegram.org",
    ):
        if (
            not authorized
            or base_url != "https://api.telegram.org"
            or not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token)
            or token.split(":", 1)[0] != approved_bot_id
        ):
            raise ValueError("emoji_configuration_denied")
        self.ids = set(allowed_ids)
        if len(self.ids) > 200 or any(not re.fullmatch(r"[0-9]{1,32}", x) for x in self.ids):
            raise ValueError("emoji_allowlist_invalid")
        self.client = httpx.Client(
            base_url=base_url, transport=transport, timeout=5, follow_redirects=False, trust_env=False
        )
        self._api = "/bot" + token + "/"
        self._files = "/file/bot" + token + "/"
        self.cache = {}
        self.lock = Lock()

    def _json(self, method, data):
        with self.client.stream("POST", self._api + method, json=data) as response:
            if response.status_code != 200:
                raise ValueError("emoji_unavailable")
            content = bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > 16384:
                    raise ValueError("emoji_metadata_too_large")
            result = json.loads(content)
            if not isinstance(result, dict) or result.get("ok") is not True:
                raise ValueError("emoji_unavailable")
            return result["result"]

    def get(self, emoji_id):
        if not re.fullmatch(r"[0-9]{1,32}", emoji_id) or emoji_id not in self.ids:
            return None
        if not self.lock.acquire(blocking=False):
            return None
        try:
            cached = self.cache.get(emoji_id)
            if cached and cached[0] > time.monotonic():
                return cached[1]
            try:
                stickers = self._json("getCustomEmojiStickers", {"custom_emoji_ids": [emoji_id]})
                if not isinstance(stickers, list) or len(stickers) != 1:
                    raise ValueError("emoji_metadata_invalid")
                sticker = stickers[0]
                if (
                    not isinstance(sticker, dict)
                    or sticker.get("custom_emoji_id") != emoji_id
                    or sticker.get("type") != "custom_emoji"
                    or sticker.get("is_animated") is not False
                    or sticker.get("is_video") is not False
                ):
                    raise ValueError("emoji_format_denied")
                file = self._json("getFile", {"file_id": sticker["file_id"]})
                if not isinstance(file, dict):
                    raise ValueError("emoji_metadata_invalid")
                path = file["file_path"]
                if (
                    not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+\.(png|webp)", path)
                    or ".." in path
                    or file.get("file_size", 0) > self.max_bytes
                ):
                    raise ValueError("emoji_file_denied")
                with self.client.stream("GET", self._files + path) as response:
                    mime = response.headers.get("content-type", "").split(";", 1)[0]
                    if response.status_code != 200 or mime not in {"image/png", "image/webp"}:
                        raise ValueError("emoji_file_denied")
                    content = bytearray()
                    for chunk in response.iter_bytes():
                        content.extend(chunk)
                        if len(content) > self.max_bytes:
                            raise ValueError("emoji_file_too_large")
                    content = bytes(content)
                    if mime == "image/png":
                        valid = content.startswith(b"\x89PNG\r\n\x1a\n") and b"acTL" not in content
                    else:
                        valid = (
                            content.startswith(b"RIFF")
                            and content[8:12] == b"WEBP"
                            and b"ANIM" not in content
                            and b"ANMF" not in content
                        )
                    if not valid:
                        raise ValueError("emoji_file_denied")
                value = (content, mime)
            except (httpx.HTTPError, ValueError, TypeError, KeyError):
                value = None
            self.cache[emoji_id] = (time.monotonic() + (300 if value else 30), value)
            return value
        finally:
            self.lock.release()


def configured_emoji(config, transport=None):
    if not config.emoji_test_authorized:
        return None
    return TelegramEmojiAssets(
        token=config.emoji_bot_token,
        approved_bot_id=config.emoji_test_bot_id,
        authorized=True,
        allowed_ids=config.emoji_test_ids,
        transport=transport,
    )
