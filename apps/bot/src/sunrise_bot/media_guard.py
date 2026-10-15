"""Одноразовые task-bound разрешения медиа после текущей серверной авторизации."""

import asyncio
import hashlib
import re
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from time import monotonic

import aiohttp
from aiogram.types import BufferedInputFile
from sunrise_core.errors import DomainError


def _deny():
    return PermissionError("Медиа не связано с текущим разрешённым действием владельца.")


@dataclass
class _Scope:
    nonce: object
    task: object
    bot_id: str
    expires: float
    kind: str
    active: bool = True
    used: bool = False
    downloaded: bool = False
    mime: str = ""
    file_id: str = ""
    size: int = 0
    message: object = None
    snapshot: object = None
    draft: object = None
    draft_snapshot: object = None
    authorize: object = None
    file: object = None
    file_snapshot: object = None
    local: object = None
    digest: str = ""
    recipient: str = ""


class BotMediaGuard:
    def __init__(self, session, *, http_factory=None, photo_bytes=2_000_000, video_bytes=8_000_000):
        if any(type(value) is not int or value <= 0 for value in (photo_bytes, video_bytes)):
            raise ValueError("media_guard_limits_invalid")
        self.session = session
        self.http_factory = http_factory or aiohttp.ClientSession
        self.photo_bytes, self.video_bytes = photo_bytes, video_bytes
        self._current = ContextVar("bot_media_scope", default=None)
        self._registry = {}

    def has_scope(self):
        return self._current.get() is not None

    def _bot(self, bot):
        self.session.safety.validate(bot.token)
        if (
            not re.fullmatch(r"[1-9][0-9]{0,18}:[A-Za-z0-9_-]{1,200}", bot.token)
            or bot.session is not self.session
        ):
            raise _deny()

    def _check(self, bot, kind):
        scope = self._current.get()
        self._bot(bot)
        if (
            not scope
            or self._registry.get(scope.nonce) is not scope
            or not scope.active
            or scope.task is not asyncio.current_task()
            or scope.expires <= monotonic()
            or scope.kind != kind
            or str(bot.id) != scope.bot_id
            or bot.session is not self.session
        ):
            raise _deny()
        return scope

    @asynccontextmanager
    async def _activate(self, scope):
        if self.has_scope():
            raise _deny()
        self._registry[scope.nonce] = scope
        token = self._current.set(scope)
        try:
            yield scope
        finally:
            scope.active = False
            self._registry.pop(scope.nonce, None)
            self._current.reset(token)

    def _incoming(self, message):
        sender = message.from_user
        if (
            not sender
            or sender.is_bot
            or str(message.chat.type) != "private"
            or message.chat.id != sender.id
            or not self.session.safety.permits(sender.id)
            or type(message.message_id) is not int
            or message.message_id <= 0
            or bool(message.photo) == bool(message.video)
        ):
            raise _deny()
        media = message.photo[-1] if message.photo else message.video
        mime = "image/jpeg" if message.photo else media.mime_type
        cap = self.photo_bytes if mime == "image/jpeg" else self.video_bytes
        if (
            mime not in {"image/jpeg", "video/mp4"}
            or not isinstance(media.file_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", media.file_id)
            or type(media.file_size) is not int
            or not 0 < media.file_size <= cap
            or message.photo
            and (
                media.width <= 0
                or media.height <= 0
                or media.width + media.height > 10_000
                or media.width * media.height > 16_000_000
                or max(media.width, media.height) > min(media.width, media.height) * 20
            )
        ):
            raise _deny()
        return (
            str(sender.id),
            message.message_id,
            media.file_id,
            media.file_unique_id,
            media.file_size,
            mime,
        )

    @staticmethod
    def _draft(draft):
        return (draft.owner, draft.identifier, draft.revision, draft.kind, draft.step, draft.expires)

    @asynccontextmanager
    async def authorized_upload_scope(self, bot, message, *, owner_id, draft, authorize):
        self._bot(bot)
        snapshot = self._incoming(message)
        if (
            bot.session is not self.session
            or draft.owner != str(owner_id)
            or draft.kind != "broadcast"
            or draft.step != "fields"
            or draft.expires <= monotonic()
            or not draft.fields
            or draft.fields[0][0] != "nodes"
            or not await authorize()
        ):
            raise _deny()
        scope = _Scope(
            object(),
            asyncio.current_task(),
            str(bot.id),
            min(monotonic() + 30, draft.expires),
            "upload",
            mime=snapshot[5],
            file_id=snapshot[2],
            size=snapshot[4],
            message=message,
            snapshot=snapshot,
            draft=draft,
            draft_snapshot=self._draft(draft),
            authorize=authorize,
        )
        async with self._activate(scope):
            yield scope

    async def _upload(self, bot):
        scope = self._check(bot, "upload")
        if (
            scope.snapshot != self._incoming(scope.message)
            or scope.draft_snapshot != self._draft(scope.draft)
            or not await scope.authorize()
        ):
            raise _deny()
        # Авторизация тоже await: после неё повторно проверяем task, отзыв и срок.
        self._check(bot, "upload")
        if scope.snapshot != self._incoming(scope.message) or scope.draft_snapshot != self._draft(
            scope.draft
        ):
            raise _deny()
        return scope

    @staticmethod
    def _file_snapshot(file):
        return (file.file_id, file.file_size, file.file_path)

    @staticmethod
    def _path(path, mime):
        pattern = (
            r"photos/[A-Za-z0-9_-]{1,200}\.(?:jpg|jpeg)"
            if mime == "image/jpeg"
            else r"videos/[A-Za-z0-9_-]{1,200}\.mp4"
        )
        if not isinstance(path, str) or not re.fullmatch(pattern, path):
            raise _deny()
        return path

    async def request_get_file(self, bot, method, timeout):
        scope = await self._upload(bot)
        if scope.used or method.file_id != scope.file_id:
            raise _deny()
        scope.used = True
        result = await self.session.wrapped.make_request(bot, method, timeout)
        scope = await self._upload(bot)
        if (
            result.file_id != scope.file_id
            or result.file_unique_id != scope.snapshot[3]
            or type(result.file_size) is not int
            or result.file_size != scope.size
        ):
            raise _deny()
        self._path(result.file_path, scope.mime)
        scope.file, scope.file_snapshot = result, self._file_snapshot(result)
        return result

    async def download_approved_file(self, bot, file):
        scope = await self._upload(bot)
        if (
            scope.downloaded
            or not scope.used
            or file is not scope.file
            or self._file_snapshot(file) != scope.file_snapshot
        ):
            raise _deny()
        scope.downloaded = True
        path = self._path(file.file_path, scope.mime)
        # Origin и путь никогда не поступают как произвольный URL; redirects и proxy env запрещены.
        url = "https://api.telegram.org/file/bot" + bot.token + "/" + path
        result = bytearray()
        try:
            async with asyncio.timeout(15):
                async with self.http_factory(
                    timeout=aiohttp.ClientTimeout(total=15, connect=5, sock_read=5),
                    trust_env=False,
                    auto_decompress=False,
                ) as client:
                    async with client.get(url, allow_redirects=False, ssl=True) as response:
                        if response.status != 200:
                            raise DomainError(
                                "media_download_failed", "Не удалось получить файл Telegram.", 503
                            )
                        if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                            raise DomainError(
                                "media_download_failed", "Сжатая загрузка медиа не поддерживается.", 400
                            )
                        declared = response.headers.get("Content-Length")
                        if declared is not None and (
                            not re.fullmatch(r"[0-9]{1,10}", declared) or int(declared) != scope.size
                        ):
                            raise DomainError(
                                "media_download_failed", "Размер файла Telegram не подтверждён.", 400
                            )
                        async for chunk in response.content.iter_chunked(65_536):
                            self._check(bot, "upload")
                            if len(result) + len(chunk) > scope.size:
                                raise DomainError(
                                    "media_download_failed",
                                    "Файл Telegram превышает подтверждённый размер.",
                                    413,
                                )
                            result.extend(chunk)
            await self._upload(bot)
            if len(result) != scope.size:
                raise DomainError("media_download_failed", "Файл Telegram загружен не полностью.", 400)
            return bytes(result)
        except (aiohttp.ClientError, OSError, TimeoutError):
            raise DomainError(
                "media_download_failed", "Не удалось загрузить файл Telegram. Повторите действие.", 503
            ) from None

    @asynccontextmanager
    async def registered_asset_send_scope(self, bot, recipient, reference, *, resolve, authorize):
        self._bot(bot)
        if bot.session is not self.session or not self.session.safety.permits(recipient):
            raise _deny()
        if not isinstance(reference, dict):
            raise _deny()
        requested = dict(reference)
        if not await authorize():
            raise _deny()
        # Resolver — доверенный серверный hook с актуальной auth и metadata registry, не HMAC-only lookup.
        resolved = await resolve(reference)
        if not await authorize():
            raise _deny()
        if (
            not isinstance(resolved, dict)
            or set(resolved) != {"reference", "bytes"}
            or resolved["reference"] != requested
            or reference != requested
        ):
            raise _deny()
        data = resolved["bytes"]
        if (
            not isinstance(reference, dict)
            or set(reference) != {"asset_id", "sha256", "mime", "size_bytes", "proof"}
            or not isinstance(reference.get("mime"), str)
            or reference.get("mime") not in {"image/jpeg", "image/png", "video/mp4"}
            or type(reference.get("size_bytes")) is not int
            or not isinstance(data, bytes)
            or not 0
            < len(data)
            <= (self.video_bytes if reference["mime"] == "video/mp4" else self.photo_bytes)
            or len(data) != reference["size_bytes"]
            or hashlib.sha256(data).hexdigest() != reference.get("sha256")
            or any(
                not isinstance(reference.get(k), str)
                or not re.fullmatch(r"[0-9a-f]{" + str(n) + "}", reference[k])
                for k, n in (("asset_id", 32), ("sha256", 64), ("proof", 64))
            )
        ):
            raise _deny()
        extension = {"image/jpeg": "jpg", "image/png": "png", "video/mp4": "mp4"}[reference["mime"]]
        local = BufferedInputFile(data, "media-" + reference["asset_id"] + "." + extension)
        scope = _Scope(
            object(),
            asyncio.current_task(),
            str(bot.id),
            monotonic() + 30,
            "send",
            mime=reference["mime"],
            size=len(data),
            local=local,
            digest=reference["sha256"],
            recipient=str(recipient),
            authorize=authorize,
        )
        async with self._activate(scope):
            yield local

    async def authorize_send(self, bot, method):
        scope = self._check(bot, "send")
        if scope.used:
            raise _deny()
        self._validate_send(scope, method)
        # Одноразовый scope расходуется до новой async auth; отказ не даёт повторного права.
        scope.used = True
        if not await scope.authorize():
            raise _deny()
        scope = self._check(bot, "send")
        self._validate_send(scope, method)

    def _validate_send(self, scope, method):
        expected = "sendVideo" if scope.mime == "video/mp4" else "sendPhoto"
        local = getattr(method, "video" if expected == "sendVideo" else "photo", None)
        if (
            method.__api_method__ != expected
            or str(method.chat_id) != scope.recipient
            or not self.session.safety.permits(method.chat_id)
            or local is not scope.local
            or not isinstance(local, BufferedInputFile)
            or len(local.data) != scope.size
            or hashlib.sha256(local.data).hexdigest() != scope.digest
            or any(
                getattr(method, k, None) is not None
                for k in (
                    "thumbnail",
                    "cover",
                    "business_connection_id",
                    "message_thread_id",
                    "direct_messages_topic_id",
                    "ephemeral_message_parameters",
                )
            )
        ):
            raise _deny()
