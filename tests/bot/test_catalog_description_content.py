"""Owned catalog HTML becomes the shared safe AST; synthetic transport only."""

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sunrise_core import telegram_content
from sunrise_core.content import validate_content
from sunrise_infrastructure.commerce import Commerce
from test_dispatcher import setup


def convert(value):
    assert hasattr(telegram_content, "telegram_html_to_content"), "Shared owned HTML converter is missing"
    return telegram_content.telegram_html_to_content(value)


@pytest.mark.parametrize("encoded", [False, True])
def test_owned_html_preserves_premium_bold_links_and_line_breaks(encoded):
    from html import escape

    raw = '<b>Тариф &amp; доступ</b>\n<tg-emoji emoji-id="9007199254740993">🌐</tg-emoji><br><a href="https://example.test/help">Помощь</a>'
    nodes = convert(escape(raw) if encoded else raw)
    validate_content(nodes)
    html = telegram_content.render_classic(nodes, custom_emoji=True)
    assert "<b>Тариф &amp; доступ</b>" in html
    assert '<tg-emoji emoji-id="9007199254740993">🌐</tg-emoji>' in html
    assert '<a href="https://example.test/help">Помощь</a>' in html
    plain = telegram_content.render_plain(nodes)
    assert "🌐\nПомощь" in plain
    assert "tg-emoji" not in telegram_content.render_classic(nodes, custom_emoji=False)


def test_paragraph_and_list_content_retains_readable_order():
    nodes = convert("<p>Главное</p><ul><li>Первое</li><li><b>Второе</b></li></ul>")
    assert telegram_content.render_plain(nodes) == "Главное\n\n• Первое\n• Второе"


@pytest.mark.parametrize("tag", ["ul", "ol"])
def test_stray_list_text_is_preserved_as_safe_plain_fallback(tag):
    raw = f"<{tag}>До<li>Пункт</li>После</{tag}>"
    nodes = convert(raw)
    assert nodes == [{"type": "text", "text": raw}]
    plain = telegram_content.render_plain(nodes)
    assert "До" in plain and "После" in plain and "Пункт" in plain
    rendered = telegram_content.render_classic(nodes, custom_emoji=True)
    assert f"<{tag}>" not in rendered
    assert f"&lt;{tag}&gt;" in rendered


@pytest.mark.parametrize("attribute", ["id", "emoji_id", "emoji-id"])
def test_legacy_emoji_attribute_and_mixed_escaped_markup_keep_exact_id(attribute):
    raw = f"<b>Описание</b> &lt;tg-emoji {attribute}=&quot;9007199254740993&quot;&gt;🌐&lt;/tg-emoji&gt;"
    rendered = telegram_content.render_classic(convert(raw), custom_emoji=True)
    assert "<b>Описание</b>" in rendered
    assert '<tg-emoji emoji-id="9007199254740993">🌐</tg-emoji>' in rendered


@pytest.mark.parametrize(
    "raw",
    [
        '<script>alert(1)</script><img src="https://example.test/image" onerror="bad()">',
        '<a href="javascript:bad()">Не ссылка</a>',
        '<tg-emoji emoji-id="123" onclick="bad()">🌐</tg-emoji>',
        '<tg-emoji emoji-id="123" emoji-id="456">🌐</tg-emoji>',
        "<b>Незакрытый текст",
    ],
)
def test_malformed_or_active_markup_never_reaches_telegram(raw):
    nodes = convert(raw)
    validate_content(nodes)
    html = telegram_content.render_classic(nodes, custom_emoji=True)
    assert "<script" not in html
    assert "<img" not in html
    assert 'href="javascript:' not in html
    assert "<tg-emoji" not in html
    assert "onclick=" not in html or "&lt;" in html


@pytest.mark.parametrize("raw", [None, 123, "x" * 4001])
def test_converter_rejects_non_string_or_unbounded_source(raw):
    with pytest.raises(ValueError):
        convert(raw)


def test_catalog_projection_preserves_separate_existing_intro_keys(monkeypatch):
    from sunrise_infrastructure import commerce

    intro = {
        "faq": [],
        "how_to_connect": [],
        "catalog_description": "Отдельный каталог",
        "catalog_description_content": [{"type": "text", "text": "AST каталога"}],
    }

    @contextmanager
    def transaction():
        yield SimpleNamespace(scalars=lambda query: [])

    values = {"public_content": intro}
    target = SimpleNamespace(
        db=SimpleNamespace(transaction=transaction),
        _setting=lambda session, key, default: values.get(key, default),
        _payment_methods_in=lambda session: [],
        _help_center=lambda session: {},
        legal=SimpleNamespace(current=lambda session: {}),
        live=False,
        sandbox_payments=False,
        local_test=True,
        panel_provider=None,
    )
    monkeypatch.setattr(commerce, "product_dto", lambda product, periods: product)
    content = Commerce.catalog(target)["content"]
    assert content["catalog_description"] == intro["catalog_description"]
    assert content["catalog_description_content"] == intro["catalog_description_content"]


@pytest.mark.parametrize("use_ast", [False, True])
def test_main_catalog_never_combines_lot_descriptions_and_selected_lot_keeps_premium(use_ast):
    async def run():
        app, service, session = setup()
        app.presenter.custom_emoji = True
        original = service.catalog

        def catalog():
            value = original()
            value["content"]["catalog_description"] = (
                '<b>Общее описание каталога</b> <tg-emoji emoji-id="9007199254740993">🌐</tg-emoji>'
            )
            for index, product in enumerate(value["products"]):
                raw = f'<b>Только лот {index + 1}</b> <tg-emoji emoji-id="{9007199254740995 + index}">⭐</tg-emoji>'
                product["description"] = raw
                if use_ast:
                    product["content"] = {"description": convert(raw)}
            return value

        service.catalog = catalog
        await app.execute("101", "9223372036854775700", "products", {"intent": "purchase"}, None, "")
        main = session.calls[-1]
        assert "<b>Общее описание каталога</b>" in main.text
        assert 'emoji-id="9007199254740993"' in main.text
        assert "Только лот" not in main.text
        assert (
            len(
                [
                    button
                    for row in main.reply_markup.inline_keyboard
                    for button in row
                    if app.actions.get(button.callback_data)
                    and app.actions[button.callback_data].action == "periods"
                ]
            )
            == 2
        )
        await app.execute(
            "101", "9223372036854775700", "periods", {"intent": "purchase", "product_id": "standard"}, 5, ""
        )
        lot = session.calls[-1]
        assert "<b>Только лот 1</b>" in lot.text
        assert 'emoji-id="9007199254740995"' in lot.text
        assert "Только лот 2" not in lot.text
        assert "Общее описание каталога" not in lot.text
        assert main.parse_mode == lot.parse_mode == "HTML"
        assert not any(call[0] in {"quote", "create"} for call in service.calls)

    asyncio.run(run())


def test_period_buttons_display_authoritative_months_and_discounts_without_changing_payload():
    async def run():
        app, service, session = setup()
        original = service.catalog
        periods = [
            {"days": 30, "months": 1, "discount_percent": "0"},
            {"days": 90, "months": 3, "discount_percent": "10"},
            {"days": 180, "months": 6, "discount_percent": "15"},
            {"days": 365, "months": 12, "discount_percent": "20"},
        ]

        def catalog():
            value = original()
            value["products"][0]["periods"] = periods
            return value

        service.catalog = catalog
        await app.execute(
            "101",
            "9223372036854775700",
            "periods",
            {"intent": "purchase", "product_id": "standard"},
            None,
            "",
        )
        buttons = [
            button
            for row in session.calls[-1].reply_markup.inline_keyboard
            for button in row
            if app.actions.get(button.callback_data)
            and app.actions[button.callback_data].action == "bonus_choice"
        ]
        assert len(buttons) == 4
        month_labels = ["1 месяц", "3 месяца", "6 месяцев", "12 месяцев"]
        for button, period, month_label in zip(buttons, periods, month_labels, strict=True):
            assert month_label in button.text
            assert f"{period['days']} дней" in button.text
            if period["discount_percent"] != "0":
                assert f"скидка {period['discount_percent']}%" in button.text
            assert app.actions[button.callback_data].data["period_days"] == period["days"]
        assert not any(call[0] in {"quote", "create"} for call in service.calls)

    asyncio.run(run())


@pytest.mark.parametrize("enabled", [False, True])
def test_notifications_and_node_news_use_configured_premium_icons_and_same_actions(enabled):
    async def run():
        app, service, session = setup()
        app.presenter.custom_emoji = enabled
        original = service.catalog
        identifiers = {"notifications": "9007199254740993", "node_news": "9007199254740995"}

        def catalog():
            value = original()
            value["content"]["telegram_theme"] = {
                "icons": {
                    key: {"fallback": "🔔", "custom_emoji_id": identifier}
                    for key, identifier in identifiers.items()
                }
            }
            return value

        service.catalog = catalog
        await app.main("101", service.resolve_telegram(101))
        buttons = [button for row in session.calls[-1].reply_markup.inline_keyboard for button in row]
        for action, identifier in identifiers.items():
            button = next(button for button in buttons if app.actions[button.callback_data].action == action)
            assert button.icon_custom_emoji_id == (identifier if enabled else None)
            assert app.actions[button.callback_data].data == {}

    asyncio.run(run())
