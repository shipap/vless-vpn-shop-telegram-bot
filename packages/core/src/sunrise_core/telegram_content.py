"""Pure shared Telegram HTML/plain rendering, independent of bot runtime."""

from html import escape, unescape
from html.parser import HTMLParser
from urllib.parse import urlsplit


def telegram_html_to_content(raw):
    """Convert explicitly owned Telegram HTML to validated shared content nodes."""
    from .content import validate_content

    if not isinstance(raw, str) or len(raw) > 4000:
        raise ValueError("telegram_content_source_invalid")
    encoded_emoji = "&lt;tg-emoji" in raw.lower()
    source = unescape(raw) if encoded_emoji or ("&lt;" in raw and "<" not in raw) else raw

    class Parser(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.nodes = []
            self.stack = [("", self.nodes, None)]
            self.count = 0

        def append(self, node):
            self.count += 1
            if self.count > 100 or len(self.stack) > 8:
                raise ValueError("telegram_content_depth_invalid")
            self.stack[-1][1].append(node)

        def handle_data(self, data):
            if data:
                self.append({"type": "text", "text": data})

        def handle_starttag(self, tag, attrs):
            if tag == "br":
                if attrs:
                    raise ValueError("telegram_content_attributes_invalid")
                self.append({"type": "br"})
                return
            kinds = {
                "b": "bold",
                "strong": "bold",
                "i": "italic",
                "em": "italic",
                "u": "underline",
                "code": "code",
                "pre": "code",
                "blockquote": "quote",
                "p": "paragraph",
            }
            children = []
            if tag in kinds and not attrs:
                node = {"type": kinds[tag], "children": children}
            elif tag == "a" and len(attrs) == 1 and attrs[0][0] == "href" and safe_url(attrs[0][1]):
                node = {"type": "link", "href": attrs[0][1], "children": children}
            elif tag == "tg-emoji" and len(attrs) == 1 and attrs[0][0] in {"emoji-id", "emoji_id", "id"}:
                identifier = attrs[0][1]
                if (
                    not isinstance(identifier, str)
                    or not identifier.isascii()
                    or not identifier.isdigit()
                    or not 1 <= len(identifier) <= 32
                ):
                    raise ValueError("telegram_content_emoji_invalid")
                node = {"type": "emoji", "id": identifier, "fallback": "", "label": "Эмодзи"}
            elif tag in {"ul", "ol"} and not attrs:
                node = {"type": "list", "ordered": tag == "ol", "items": []}
            elif tag == "li" and not attrs and self.stack[-1][0] in {"ul", "ol"}:
                self.stack[-1][2]["items"].append(children)
                self.stack.append((tag, children, None))
                return
            else:
                self.handle_data(self.get_starttag_text())
                return
            self.append(node)
            self.stack.append((tag, children, node))

        def handle_startendtag(self, tag, attrs):
            self.handle_starttag(tag, attrs)
            if tag != "br":
                self.handle_endtag(tag)

        def handle_endtag(self, tag):
            supported = {
                "b",
                "strong",
                "i",
                "em",
                "u",
                "code",
                "pre",
                "blockquote",
                "p",
                "a",
                "tg-emoji",
                "ul",
                "ol",
                "li",
            }
            if tag not in supported:
                self.handle_data(f"</{tag}>")
                return
            if self.stack[-1][0] != tag:
                raise ValueError("telegram_content_markup_invalid")
            _, children, node = self.stack.pop()
            if tag in {"ul", "ol"} and any(
                item["type"] != "text" or item["text"].strip() for item in children
            ):
                raise ValueError("telegram_content_list_invalid")
            if tag == "tg-emoji":
                if any(item["type"] != "text" for item in children):
                    raise ValueError("telegram_content_emoji_invalid")
                fallback = "".join(item["text"] for item in children)
                if not 1 <= len(fallback) <= 16:
                    raise ValueError("telegram_content_emoji_invalid")
                node["fallback"] = fallback

    parser = Parser()
    try:
        parser.feed(source)
        parser.close()
        if len(parser.stack) != 1:
            raise ValueError("telegram_content_markup_invalid")
        return validate_content(parser.nodes)
    except ValueError:
        # Malformed or unsupported structure remains escaped text, never raw HTML.
        return validate_content([{"type": "text", "text": raw}])


def safe_url(value):
    if not isinstance(value, str) or any(ord(c) <= 32 for c in value) or "\\" in value:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.username or parsed.password:
            return None
        if parsed.scheme == "https" and parsed.hostname:
            return value
        if parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
            return value
    except ValueError:
        pass
    return None


def _render(nodes, *, html, custom_emoji=False, depth=0):
    if depth > 12 or not isinstance(nodes, list):
        return ""
    result = []
    for node in nodes[:500]:
        if not isinstance(node, dict):
            continue
        kind = node.get("type")
        raw = str(node.get("text", ""))[:4096]
        text = escape(raw) if html else raw
        children = _render(node.get("children", []), html=html, custom_emoji=custom_emoji, depth=depth + 1)
        if kind == "text":
            result.append(text)
        elif kind == "emoji":
            fallback = str(node.get("fallback") or "☀️")[:16]
            emoji_id = node.get("id")
            rendered = escape(fallback) if html else fallback
            if html and custom_emoji and isinstance(emoji_id, str) and emoji_id.isdecimal():
                rendered = f'<tg-emoji emoji-id="{emoji_id}">{rendered}</tg-emoji>'
            result.append(rendered)
        elif kind in {"bold", "strong", "heading", "italic", "emphasis", "underline", "code"}:
            tag = {"italic": "i", "emphasis": "i", "underline": "u", "code": "code"}.get(kind, "b")
            result.append(
                (f"<{tag}>{children}</{tag}>" if html else children) + ("\n\n" if kind == "heading" else "")
            )
        elif kind == "br":
            result.append("\n")
        elif kind == "link":
            href = safe_url(node.get("href"))
            result.append(
                f'<a href="{escape(href, quote=True)}">{children}</a>' if html and href else children
            )
        elif kind == "quote":
            result.append((f"<blockquote>{children}</blockquote>" if html else children) + "\n\n")
        elif kind in {"paragraph", "details"}:
            title = str(node.get("title", "Подробнее")) if kind == "details" else ""
            result.append((escape(title) if html else title) + ("\n" if title else "") + children + "\n\n")
        elif kind == "list":
            for index, item in enumerate(node.get("items", [])[:100], start=1):
                marker = f"{index}. " if node.get("ordered") else "• "
                result.append(
                    marker + _render(item, html=html, custom_emoji=custom_emoji, depth=depth + 1) + "\n"
                )
        elif kind == "table":
            for row in node.get("rows", [])[:30]:
                result.append(
                    " · ".join(
                        _render(cell, html=html, custom_emoji=custom_emoji, depth=depth + 1)
                        for cell in row[:6]
                    )
                    + "\n"
                )
        else:
            result.append(text or children)
    return "".join(result)


def render_classic(nodes, custom_emoji=False):
    return _render(nodes, html=True, custom_emoji=custom_emoji).strip()


def render_plain(nodes):
    return _render(nodes, html=False).strip()
