from dataclasses import dataclass

from aiogram.client.session.base import BaseSession
from aiogram.types import BufferedInputFile


@dataclass(frozen=True)
class BotSafety:
    bot_ids: frozenset[str]
    recipient_ids: frozenset[str]

    def validate(self, token: str) -> None:
        if not self.bot_ids or not self.recipient_ids:
            raise ValueError("Нужны явные списки разрешённых тестовых ботов и получателей")
        if token.split(":", 1)[0] not in self.bot_ids:
            raise ValueError("Идентификатор бота отсутствует в тестовом списке")
        if any(not x.isdecimal() or int(x) <= 0 for x in self.bot_ids | self.recipient_ids):
            raise ValueError("Разрешены только положительные идентификаторы личных тестовых аккаунтов")

    def permits(self, telegram_id: str | int) -> bool:
        return str(telegram_id) in self.recipient_ids


@dataclass(frozen=True)
class ProductionBotSafety:
    runtime_policy: object

    @property
    def bot_ids(self):
        return frozenset({self.runtime_policy.scope.bot_id})

    @property
    def recipient_ids(self):
        # Live customer authority is checked by shared handlers, not a test allowlist.
        return frozenset()

    def validate(self, token):
        if not self.runtime_policy.production or token.split(":", 1)[0] not in self.bot_ids:
            raise ValueError("production_bot_identity_denied")
        self.runtime_policy.require()

    def permits(self, telegram_id):
        value = str(telegram_id)
        return value.isascii() and value.isdecimal() and 0 < int(value) < 2**63

    def authorize_outbound(self, method):
        self.runtime_policy.require(
            "notice_telegram" if method not in {"getMe", "getUpdates", "getFile"} else None
        )


class GuardedSession(BaseSession):
    """Recipient check also protects accidental service/outbox sends."""

    def __init__(self, wrapped: BaseSession, safety: BotSafety):
        super().__init__()
        self.wrapped = wrapped
        self.safety = safety
        from .media_guard import BotMediaGuard

        self.media = BotMediaGuard(self)

    async def close(self):
        await self.wrapped.close()

    async def stream_content(self, *args, **kwargs):
        raise PermissionError("Скачивание файлов не включено в разрешённые действия тестового бота")
        yield b""  # pragma: no cover

    async def make_request(self, bot, method, timeout=None):
        self.safety.validate(bot.token)
        if method.__api_method__ == "getFile":
            return await self.media.request_get_file(bot, method, timeout)
        if method.__api_method__ in {"sendPhoto", "sendVideo"} and self.media.has_scope():
            await self.media.authorize_send(bot, method)
            if isinstance(self.safety, ProductionBotSafety):
                self.safety.authorize_outbound(method.__api_method__)
            return await self.wrapped.make_request(bot, method, timeout)
        if method.__api_method__ not in {
            "getMe",
            "getUpdates",
            "sendMessage",
            "sendPhoto",
            "sendRichMessage",
            "editMessageText",
            "answerCallbackQuery",
        }:
            raise PermissionError("Метод Telegram не включён в разрешённые действия тестового адаптера")
        if method.__api_method__ == "sendPhoto" and (
            not isinstance(method.photo, BufferedInputFile)
            or not method.photo.data.startswith(b"\x89PNG\r\n\x1a\n")
            or len(method.photo.data) > 2_000_000
        ):
            raise PermissionError("Разрешён только локально созданный PNG QR-кода")
        targets = [getattr(method, name, None) for name in ("chat_id", "from_chat_id", "user_id")]
        for target in targets:
            if target is not None and not self.safety.permits(target):
                raise PermissionError("Получатель отсутствует в разрешённом тестовом списке")
        if isinstance(self.safety, ProductionBotSafety):
            self.safety.authorize_outbound(method.__api_method__)
        return await self.wrapped.make_request(bot, method, timeout)
