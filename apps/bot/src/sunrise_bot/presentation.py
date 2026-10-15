from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, LinkPreviewOptions

from sunrise_bot.content import render_classic, render_plain, render_rich


class Presenter:
    def __init__(self, bot, *, custom_emoji=False, rich_messages=False):
        self.bot = bot
        self.custom_emoji = custom_emoji
        self.rich_messages = rich_messages
        self.fallback_labels = {}
        self.reason = (
            "Возможность custom emoji подтверждена на разрешённом тестовом боте; Rich Messages отключены"
            if custom_emoji and not rich_messages
            else "Custom emoji и Rich Messages подтверждены на разрешённом тестовом боте"
            if custom_emoji and rich_messages
            else "Rich Messages подтверждены на разрешённом тестовом боте; custom emoji не подтверждены"
            if rich_messages
            else "Внешние возможности Telegram не подтверждены на разрешённом тестовом боте"
        )

    def capability_status(self):
        return {"rich_messages": self.rich_messages, "custom_emoji": self.custom_emoji, "reason": self.reason}

    async def show(self, chat_id, nodes, rows, message_id=None):
        markup = InlineKeyboardMarkup(inline_keyboard=rows)
        link_preview_options = LinkPreviewOptions(is_disabled=True)
        try:
            if self.rich_messages:
                message = render_rich(nodes, custom_emoji=self.custom_emoji)
                if message_id:
                    return await self.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        rich_message=message,
                        reply_markup=markup,
                        link_preview_options=link_preview_options,
                    )
                # sendRichMessage принимает явные блоки, без параметров предпросмотра ссылок.
                return await self.bot.send_rich_message(
                    chat_id=chat_id, rich_message=message, reply_markup=markup
                )
            text = render_classic(nodes, custom_emoji=self.custom_emoji)
            if message_id:
                return await self.bot.edit_message_text(
                    text=text,
                    chat_id=chat_id,
                    message_id=message_id,
                    parse_mode="HTML",
                    reply_markup=markup,
                    link_preview_options=link_preview_options,
                )
            return await self.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                reply_markup=markup,
                link_preview_options=link_preview_options,
            )
        except TelegramBadRequest as exc:
            reason = exc.message.lower()
            if "message is not modified" in reason:
                return None
            if not any(part in reason for part in ("parse", "emoji", "rich", "entity", "entities")):
                raise
            self.reason = "Telegram отклонил формат; используется безопасный текст и Unicode"
            self.custom_emoji = False
            self.rich_messages = False
            markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        button.model_copy(
                            update={
                                "icon_custom_emoji_id": None,
                                "text": self.fallback_labels.get(
                                    button.callback_data or button.url, button.text
                                ),
                            }
                        )
                        for button in row
                    ]
                    for row in rows
                ]
            )
            # Повторяем только доставку; бизнес-операции уже завершились до этой функции.
            return await self.bot.send_message(
                chat_id=chat_id,
                text=render_plain(nodes),
                parse_mode=None,
                reply_markup=markup,
                link_preview_options=link_preview_options,
            )
