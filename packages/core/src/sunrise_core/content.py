from urllib.parse import urlsplit


def validate_content(nodes, depth=0):
    if not isinstance(nodes, list) or len(nodes) > 100 or depth > 8:
        raise ValueError("Слишком сложный контент")
    for node in nodes:
        if not isinstance(node, dict):
            raise ValueError("Неверный элемент")
        kind = node.get("type")
        allowed = {"type"}
        if kind == "text":
            allowed |= {"text"}
            if not isinstance(node.get("text"), str) or len(node["text"]) > 4000:
                raise ValueError("Неверный текст")
        elif kind == "emoji":
            allowed |= {"id", "fallback", "label"}
            if "id" in node and (
                not isinstance(node["id"], str)
                or not node["id"].isascii()
                or not node["id"].isdigit()
                or len(node["id"]) > 32
            ):
                raise ValueError("Неверный ID эмодзи")
            if not all(
                isinstance(node.get(k), str) and 0 < len(node[k]) < 100 for k in ("fallback", "label")
            ):
                raise ValueError("Эмодзи требует понятного текста и замены")
        elif kind in {
            "bold",
            "italic",
            "underline",
            "code",
            "paragraph",
            "link",
            "heading",
            "quote",
            "details",
        }:
            allowed |= {"children"}
            validate_content(node.get("children"), depth + 1)
            if kind == "link":
                allowed |= {"href"}
                link = urlsplit(node.get("href", ""))
                if link.scheme != "https" or not link.hostname or link.username or link.password:
                    raise ValueError("Разрешены только HTTPS ссылки")
            if kind == "heading":
                allowed |= {"level"}
                if type(node.get("level", 3)) is not int or not 1 <= node.get("level", 3) <= 6:
                    raise ValueError("Неверный уровень заголовка")
            if kind == "details":
                allowed |= {"title"}
                if not isinstance(node.get("title"), str) or not 1 <= len(node["title"]) <= 200:
                    raise ValueError("Блок подробностей требует заголовок")
        elif kind == "list":
            allowed |= {"items", "ordered"}
            items = node.get("items")
            if (
                not isinstance(items, list)
                or not 1 <= len(items) <= 100
                or type(node.get("ordered", False)) is not bool
            ):
                raise ValueError("Неверный список")
            for item in items:
                validate_content(item, depth + 1)
        elif kind == "table":
            allowed |= {"rows"}
            rows = node.get("rows")
            if not isinstance(rows, list) or not 1 <= len(rows) <= 30:
                raise ValueError("Неверная таблица")
            width = None
            for row in rows:
                if not isinstance(row, list) or not 1 <= len(row) <= 6:
                    raise ValueError("Неверная строка таблицы")
                if width is not None and len(row) != width:
                    raise ValueError("Строки таблицы должны иметь одинаковое число ячеек")
                width = len(row)
                for cell in row:
                    validate_content(cell, depth + 1)
        elif kind == "br":
            pass
        else:
            raise ValueError("Элемент не поддерживается")
        if set(node) - allowed:
            raise ValueError("Лишние поля контента")
    return nodes
