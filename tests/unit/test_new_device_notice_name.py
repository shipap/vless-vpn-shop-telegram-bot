"""New-device notices use the same owned tariff content as subscription cards."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from sunrise_core.telegram_content import render_classic, render_plain
from sunrise_infrastructure.panel_events import new_device_notice_content

EMOJI_ID = "9007199254740993123"


def product(**values):
    return SimpleNamespace(name="Стандартный", content={}, presentation={}, **values)


def test_catalog_tariff_ast_replaces_default_and_custom_subscription_name():
    tariff = product()
    tariff.content = {
        "name": [
            {"type": "emoji", "id": EMOJI_ID, "fallback": "💎", "label": "Тариф"},
            {"type": "text", "text": " Стандартный"},
        ]
    }
    before = deepcopy(tariff.content)
    body, nodes = new_device_notice_content(tariff, "Моя подписка", "Windows 11 Pro")
    assert (
        body
        == "К подписке «💎 Стандартный» подключено устройство «Windows 11 Pro». Если это не вы, проверьте устройства и обновите ссылку."
    )
    assert render_plain(nodes) == body
    assert f'<tg-emoji emoji-id="{EMOJI_ID}">💎</tg-emoji>' in render_classic(nodes, custom_emoji=True)
    assert "Моя подписка" not in body
    nodes[1]["children"][0]["fallback"] = "🔹"
    assert tariff.content == before


def test_product_icon_is_premium_fallback_when_name_ast_has_no_emoji():
    tariff = product()
    tariff.presentation = {"icon": {"fallback": "🚀", "custom_emoji_id": EMOJI_ID}}
    body, nodes = new_device_notice_content(tariff, "Мой рабочий VPN", "Phone")
    assert "«🚀 Стандартный»" in body
    assert f'<tg-emoji emoji-id="{EMOJI_ID}">🚀</tg-emoji>' in render_classic(nodes, custom_emoji=True)
    assert "Мой рабочий VPN" not in body


def test_existing_name_emoji_is_not_duplicated_by_catalog_icon():
    tariff = product()
    tariff.name = f'<tg-emoji emoji-id="{EMOJI_ID}">💎</tg-emoji> Стандартный'
    tariff.presentation = {"icon": {"fallback": "🚀", "custom_emoji_id": "123"}}
    body, nodes = new_device_notice_content(tariff, "Моя подписка", "Phone")
    assert "«💎 Стандартный»" in body
    assert render_classic(nodes, custom_emoji=True).count("<tg-emoji") == 1


@pytest.mark.parametrize(
    "invalid_name",
    [
        [{"type": "script", "text": "unsafe"}],
        [{"type": "link", "href": 123, "children": [{"type": "text", "text": "unsafe"}]}],
    ],
)
def test_malformed_presentation_falls_back_safely_and_device_name_is_plain_text(invalid_name):
    tariff = product()
    tariff.content = {"name": invalid_name}
    tariff.presentation = {"icon": {"fallback": "🚀", "custom_emoji_id": "invalid"}}
    body, nodes = new_device_notice_content(tariff, "Моя подписка", "<b>Device & Browser</b>")
    assert "«Стандартный»" in body
    html = render_classic(nodes, custom_emoji=True)
    assert "&lt;b&gt;Device &amp; Browser&lt;/b&gt;" in html
    assert "unsafe" not in html
    assert "<tg-emoji" not in html


def test_missing_product_preserves_custom_label_only_as_escaped_last_fallback():
    body, nodes = new_device_notice_content(None, "<b>Рабочая подписка</b>", "Phone")
    assert "«<b>Рабочая подписка</b>»" in body
    assert "&lt;b&gt;Рабочая подписка&lt;/b&gt;" in render_classic(nodes, custom_emoji=True)
