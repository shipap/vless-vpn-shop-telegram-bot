"""Semantic AST compatible with the shared web renderer; no arbitrary HTML."""

from copy import deepcopy
from html import escape

from aiogram.types import (
    InputRichBlockBlockQuotation,
    InputRichBlockDetails,
    InputRichBlockParagraph,
    InputRichBlockSectionHeading,
    InputRichBlockTable,
    InputRichMessage,
    RichBlockTableCell,
)
from sunrise_core.telegram_content import render_classic as render_classic
from sunrise_core.telegram_content import render_plain as render_plain
from sunrise_core.telegram_content import safe_url as safe_url


def button_content(nodes, icon=None):
    """Telegram permits one custom button icon; the full name AST stays unchanged."""
    if icon:
        return render_plain(nodes).strip(), icon
    label_nodes = deepcopy(nodes)

    def extract(items):
        for node in items:
            identifier = node.get("id") if node.get("type") == "emoji" else None
            if isinstance(identifier, str) and identifier.isdecimal():
                result = {"custom_emoji_id": identifier, "fallback": node.get("fallback", "")}
                node.clear()
                node.update(type="text", text="")
                return result
            result = extract(node.get("children", []))
            if result:
                return result
        return None

    icon = extract(label_nodes)
    return render_plain(label_nodes).strip(), icon


def render_rich(nodes, custom_emoji=False, depth=0):
    blocks = []
    if depth > 12:
        return InputRichMessage(blocks=[])
    for node in nodes[:100]:
        text = render_classic([node], custom_emoji=custom_emoji)
        if node.get("type") == "heading":
            text = render_classic(node.get("children", []), custom_emoji=custom_emoji)
            level = node.get("level", 2)
            if type(level) is not int or not 1 <= level <= 6:
                level = 2
            blocks.append(InputRichBlockSectionHeading(text=text, size=level))
        elif node.get("type") == "table":
            cells = [
                [
                    RichBlockTableCell(
                        align="left", valign="top", text=render_classic(cell, custom_emoji=custom_emoji)
                    )
                    for cell in row[:6]
                ]
                for row in node.get("rows", [])[:30]
            ]
            blocks.append(InputRichBlockTable(cells=cells, is_compact=True))
        elif node.get("type") == "details":
            blocks.append(
                InputRichBlockDetails(
                    summary=escape(str(node.get("title", "Подробнее"))),
                    blocks=render_rich(node.get("children", []), custom_emoji, depth + 1).blocks,
                )
            )
        elif node.get("type") == "quote":
            blocks.append(
                InputRichBlockBlockQuotation(
                    blocks=render_rich(node.get("children", []), custom_emoji, depth + 1).blocks
                )
            )
        else:
            blocks.append(InputRichBlockParagraph(text=text or " "))
    return InputRichMessage(blocks=blocks)


def card(title, body, emoji=None):
    children = ([emoji, {"type": "text", "text": " "}] if emoji else []) + [{"type": "text", "text": title}]
    return [{"type": "heading", "children": children}] + (
        [
            {"type": "paragraph", "children": [{"type": "text", "text": body}]},
        ]
        if body
        else []
    )
