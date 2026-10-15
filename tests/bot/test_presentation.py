import asyncio

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage, SendRichMessage, SetWebhook
from aiogram.types import Message
from sunrise_bot.content import render_classic, render_plain, render_rich
from sunrise_bot.presentation import Presenter
from sunrise_bot.safety import BotSafety, GuardedSession

TOKEN = "700001:" + "x" * 35


class FakeSession(BaseSession):
    def __init__(self, fail_html=False, fail_rich=False):
        super().__init__()
        self.calls = []
        self.fail_html = fail_html
        self.fail_rich = fail_rich

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        if False:
            yield b""

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, AnswerCallbackQuery):
            return True
        if isinstance(method, SetWebhook):
            return True
        if self.fail_rich and (
            isinstance(method, SendRichMessage)
            or isinstance(method, EditMessageText)
            and method.rich_message is not None
        ):
            raise TelegramBadRequest(method=method, message="rich message format is unsupported")
        if self.fail_html and isinstance(method, (SendMessage, EditMessageText)) and method.parse_mode:
            raise TelegramBadRequest(method=method, message="can't parse entities")
        return Message.model_validate(
            {
                "message_id": len(self.calls),
                "date": 1791384304,
                "chat": {"id": method.chat_id, "type": "private"},
                "text": getattr(method, "text", ""),
            },
            context={"bot": bot},
        )


def test_guard_denies_empty_allowlists_and_wrong_bot():
    with pytest.raises(ValueError):
        BotSafety(frozenset(), frozenset({"101"})).validate(TOKEN)
    with pytest.raises(ValueError):
        BotSafety(frozenset({"700002"}), frozenset({"101"})).validate(TOKEN)


def test_session_denies_restored_recipient_without_network():
    async def run():
        underlying = FakeSession()
        policy = BotSafety(frozenset({"700001"}), frozenset({"101"}))
        bot = Bot(TOKEN, session=GuardedSession(underlying, policy))
        with pytest.raises(PermissionError):
            await bot.send_message(202, "закрыто")
        assert not underlying.calls
        await bot.send_message(101, "разрешено")
        assert len(underlying.calls) == 1

    asyncio.run(run())


def test_semantic_content_escapes_names_and_keeps_emoji_string():
    nodes = [
        {"type": "heading", "children": [{"type": "text", "text": "<script>&"}]},
        {"type": "emoji", "id": "5368324170671202286", "fallback": "☀️", "label": "Солнце"},
        {"type": "link", "href": "javascript:alert(1)", "children": [{"type": "text", "text": "помощь"}]},
    ]
    html = render_classic(nodes, custom_emoji=True)
    assert "&lt;script&gt;&amp;" in html
    assert 'emoji-id="5368324170671202286"' in html
    assert "javascript:" not in html
    assert "tg-emoji" not in render_classic(nodes, custom_emoji=False)
    assert "<script>&" in render_plain(nodes)
    assert render_rich(nodes).blocks


def test_inline_formatting_preserves_spaces_around_bold_labels():
    nodes = [
        {
            "type": "paragraph",
            "children": [
                {"type": "text", "text": "👛 "},
                {"type": "bold", "children": [{"type": "text", "text": "Денежный остаток: "}]},
                {"type": "text", "text": "75,00 ₽"},
            ],
        }
    ]
    assert render_classic(nodes) == "👛 <b>Денежный остаток: </b>75,00 ₽"
    assert render_plain(nodes) == "👛 Денежный остаток: 75,00 ₽"


def test_owner_quote_renders_supported_format_and_plain_fallback():
    nodes = [{"type": "quote", "children": [{"type": "text", "text": "Не передавайте личную ссылку."}]}]
    assert render_classic(nodes) == "<blockquote>Не передавайте личную ссылку.</blockquote>"
    assert render_plain(nodes) == "Не передавайте личную ссылку."
    assert render_rich(nodes).blocks[0].type == "blockquote"


def test_parse_error_retries_only_delivery_in_plaintext():
    async def run():
        session = FakeSession(fail_html=True)
        bot = Bot(TOKEN, session=session)
        presenter = Presenter(bot, custom_emoji=True)
        await presenter.show("101", [{"type": "text", "text": "Безопасно < &"}], [])
        assert len(session.calls) == 2
        assert session.calls[-1].parse_mode is None
        assert session.calls[-1].text == "Безопасно < &"
        assert presenter.capability_status()["custom_emoji"] is False

    asyncio.run(run())


def test_native_rich_method_is_used_only_when_explicitly_enabled():
    async def run():
        session = FakeSession()
        bot = Bot(TOKEN, session=session)
        presenter = Presenter(bot, rich_messages=True)
        await presenter.show(
            "101",
            [
                {"type": "heading", "children": [{"type": "text", "text": "Тарифы"}]},
                {
                    "type": "link",
                    "href": "https://example.test/offer",
                    "children": [{"type": "text", "text": "Публичная оферта"}],
                },
            ],
            [],
        )
        assert isinstance(session.calls[0], SendRichMessage)
        payload = session.prepare_value(session.calls[0].model_dump(warnings=False), bot, {}, False)
        assert "link_preview_options" not in payload
        assert "media" not in payload["rich_message"]
        assert payload["rich_message"]["blocks"] == [
            {"type": "heading", "size": 2, "text": "Тарифы"},
            {
                "type": "paragraph",
                "text": '<a href="https://example.test/offer">Публичная оферта</a>',
            },
        ]

    asyncio.run(run())


@pytest.mark.parametrize("rich_messages,message_id", [(False, None), (False, 42), (True, 42)])
def test_presenter_disables_link_previews_on_supported_send_and_edit_methods(rich_messages, message_id):
    async def run():
        session = FakeSession()
        bot = Bot(TOKEN, session=session, default=DefaultBotProperties(link_preview_is_disabled=False))
        presenter = Presenter(bot, rich_messages=rich_messages)
        await presenter.show(
            "101",
            [
                {
                    "type": "link",
                    "href": "https://example.test/offer",
                    "children": [{"type": "text", "text": "Публичная оферта"}],
                }
            ],
            [],
            message_id=message_id,
        )
        assert len(session.calls) == 1
        method = session.calls[0]
        assert isinstance(method, EditMessageText if message_id else SendMessage)
        payload = session.prepare_value(method.model_dump(warnings=False), bot, {}, False)
        assert payload["link_preview_options"]["is_disabled"] is True
        expected_link = '<a href="https://example.test/offer">Публичная оферта</a>'
        if rich_messages:
            assert payload["rich_message"]["blocks"][0]["text"] == expected_link
        else:
            assert payload["text"] == expected_link

    asyncio.run(run())


@pytest.mark.parametrize("rich_messages", [False, True])
@pytest.mark.parametrize("message_id", [None, 42])
def test_format_fallback_disables_link_previews_for_plain_delivery(rich_messages, message_id):
    async def run():
        session = FakeSession(fail_html=not rich_messages, fail_rich=rich_messages)
        bot = Bot(TOKEN, session=session, default=DefaultBotProperties(link_preview_is_disabled=False))
        presenter = Presenter(bot, rich_messages=rich_messages)
        await presenter.show(
            "101",
            [{"type": "text", "text": "Публичная оферта: https://example.test/offer"}],
            [],
            message_id=message_id,
        )
        assert len(session.calls) == 2
        method = session.calls[-1]
        assert isinstance(method, SendMessage)
        payload = session.prepare_value(method.model_dump(warnings=False), bot, {}, False)
        assert payload["link_preview_options"]["is_disabled"] is True
        assert method.parse_mode is None
        assert payload["text"] == "Публичная оферта: https://example.test/offer"
        assert presenter.capability_status()["rich_messages"] is False

    asyncio.run(run())


def test_native_tables_and_details_have_exact_api_block_schema():
    nodes = [
        {
            "type": "table",
            "rows": [[[{"type": "text", "text": "Тариф"}], [{"type": "text", "text": "Стоимость"}]]],
        },
        {"type": "details", "title": "Инструкция", "children": [{"type": "text", "text": "Подключение"}]},
    ]
    rich = render_rich(nodes).model_dump(exclude_none=True)
    assert rich["blocks"][0]["type"] == "table"
    assert rich["blocks"][0]["cells"][0][0]["text"] == "Тариф"
    assert rich["blocks"][1]["type"] == "details"
    assert rich["blocks"][1]["summary"] == "Инструкция"
    classic = render_classic(nodes)
    assert "<table>" not in classic and "<details>" not in classic


@pytest.mark.parametrize("level", [1, 3, 6])
def test_native_heading_preserves_shared_ast_level(level):
    node = {"type": "heading", "level": level, "children": [{"type": "text", "text": "Тарифы"}]}
    block = render_rich([node]).model_dump(exclude_none=True)["blocks"][0]
    assert block["size"] == level
    assert block["text"] == "Тарифы"


def test_session_rejects_webhook_even_for_approved_bot():
    async def run():
        underlying = FakeSession()
        bot = Bot(
            TOKEN, session=GuardedSession(underlying, BotSafety(frozenset({"700001"}), frozenset({"101"})))
        )
        with pytest.raises(PermissionError):
            await bot(SetWebhook(url="https://example.test/hook"))
        assert not underlying.calls

    asyncio.run(run())


def test_semantic_inline_styles_breaks_and_ordered_lists_have_readable_fallback():
    nodes = [
        {"type": "underline", "children": [{"type": "text", "text": "<подчёркнуто>"}]},
        {"type": "br"},
        {"type": "code", "children": [{"type": "text", "text": "A&B"}]},
        {
            "type": "list",
            "ordered": True,
            "items": [[{"type": "text", "text": "Первый"}], [{"type": "text", "text": "Второй"}]],
        },
    ]
    classic = render_classic(nodes)
    assert "<u>&lt;подчёркнуто&gt;</u>\n<code>A&amp;B</code>" in classic
    assert "1. Первый\n2. Второй" in classic
    plain = render_plain(nodes)
    assert "<подчёркнуто>\nA&B" in plain
    assert "1. Первый\n2. Второй" in plain
