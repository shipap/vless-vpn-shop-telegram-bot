import pytest
from sunrise_core.content import validate_content


def test_semantic_emoji_preserved_and_script_href_refused():
    nodes = [{"type": "emoji", "id": "9007199254740993", "fallback": "☀", "label": "Солнце"}]
    assert validate_content(nodes) == nodes
    with pytest.raises(ValueError):
        validate_content(
            [{"type": "link", "href": "javascript:alert(1)", "children": [{"type": "text", "text": "x"}]}]
        )
    with pytest.raises(ValueError):
        validate_content([{"type": "script", "text": "x"}])


def test_unicode_only_emoji_can_be_saved_and_optional_id_is_strict():
    nodes = [{"type": "emoji", "fallback": "☀️", "label": "Северный VPN"}]
    assert validate_content(nodes) == nodes
    for value in (None, "", "１２３", "x", 123):
        with pytest.raises(ValueError):
            validate_content([nodes[0] | {"id": value}])


def test_rich_content_common_ast_is_bounded_and_validated_recursively():
    text = {"type": "text", "text": "Инструкция"}
    nodes = [
        {"type": "heading", "level": 2, "children": [text]},
        {"type": "quote", "children": [text]},
        {"type": "details", "title": "Подробнее", "children": [text]},
        {"type": "list", "ordered": True, "items": [[text], [text]]},
        {"type": "table", "rows": [[[text], [text]]]},
    ]
    assert validate_content(nodes) == nodes
    for invalid in (
        {"type": "heading", "level": 0, "children": [text]},
        {"type": "list", "ordered": "yes", "items": [[text]]},
        {"type": "table", "rows": [[[{"type": "script", "text": "x"}]]]},
        {"type": "table", "rows": [[[text]], [[text], [text]]]},
        {"type": "details", "title": "", "children": [text]},
        {"type": "quote", "children": [text], "onclick": "script"},
    ):
        with pytest.raises(ValueError):
            validate_content([invalid])
