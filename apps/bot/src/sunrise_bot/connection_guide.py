"""Telegram presentation of the shared, sanitized connection-instruction DTO."""

from sunrise_infrastructure.instance_config import InstanceConfig

from sunrise_bot import copy
from sunrise_bot.content import card, safe_url

PLATFORM_ICONS = (
    "devices",
    "quote_devices",
    "device_add",
    "devices_heading",
    "product_select",
    "link",
    "quote_target",
)
APP_ICONS = (
    "product_select",
    "link",
    "device_add",
    "devices_heading",
    "quote_devices",
    "devices",
    "link_heading",
    "quote_target",
)
DOWNLOAD_ICONS = ("link", "product_select", "device_add", "devices_heading", "quote_devices")


def units(value):
    return len(value.encode("utf-16-le")) // 2


def split_text(value, budget):
    """Keep every character, including supplementary Unicode, across bounded pages."""
    chunk, size = [], 0
    for character in value:
        width = 2 if ord(character) > 0xFFFF else 1
        if chunk and size + width > budget:
            yield "".join(chunk)
            chunk, size = [], 0
        chunk.append(character)
        size += width
    if chunk:
        yield "".join(chunk)


def connection_nodes(body, url, notice, instance):
    nodes = card(copy.CONNECT, body)
    if url:
        nodes.extend(
            [
                {"type": "paragraph", "children": [{"type": "text", "text": copy.copy_connection(instance)}]},
                {
                    "type": "paragraph",
                    "children": [{"type": "code", "children": [{"type": "text", "text": url}]}],
                },
            ]
        )
    return nodes


def guide_screen(guide, sid, url, *, platform=None, app=None, page=0, instance=None):
    """Return show() arguments; callback payload stays in the adapter's owner-bound map."""
    instance = instance if instance is not None else InstanceConfig()
    platforms = guide.get("platforms", [])
    if guide.get("source") != "remnawave" or not platforms:
        return None
    notice = ""
    shared = {"sid": sid, "guide": guide}
    buttons = []
    links = [("Открыть подписку", url, "connection_open")] if url else []
    back, back_data = "subscription", {"sid": sid}
    if platform is None:
        body = "Выберите платформу для подключения."
        buttons = [
            (
                item["name"],
                "connection_platform",
                shared | {"platform": index, "icon_key": PLATFORM_ICONS[index]},
            )
            for index, item in enumerate(platforms)
        ]
    else:
        if type(platform) is not int or not 0 <= platform < len(platforms):
            return None
        selected = platforms[platform]
        shared |= {"platform": platform}
        back, back_data = "connect", {"sid": sid}
        if app is None:
            body = selected["name"] + " — выберите приложение."
            buttons = [
                (item["name"], "connection_app", shared | {"app": index, "icon_key": APP_ICONS[index]})
                for index, item in enumerate(selected["apps"])
            ]
        else:
            if type(app) is not int or not 0 <= app < len(selected["apps"]):
                return None
            application = selected["apps"][app]
            introduction = selected["name"] + " · " + application["name"]
            # Reserve the complete shared caption, personal URL, notice and header on every page.
            budget = min(
                1800,
                4096
                - units(introduction + (url or "") + notice + copy.copy_connection(instance) + copy.CONNECT)
                - 256,
            )
            if budget < 2:
                return None
            pages = []
            for index, step in enumerate(application["steps"]):
                text = f"{index + 1}. {step['title']}\n{step['description']}"
                download = [
                    (item["text"], link, DOWNLOAD_ICONS[link_index])
                    for link_index, item in enumerate(step["links"])
                    if (link := safe_url(item["url"])) and link.startswith("https://")
                ]
                pages.extend((part, download) for part in split_text(text, budget))
            if type(page) is not int or not 0 <= page < len(pages):
                return None
            text, download = pages[page]
            body = f"{introduction}\nСтраница {page + 1} из {len(pages)}\n\n{text}"
            links.extend(download)
            shared |= {"app": app}
            if page:
                buttons.append(
                    ("Предыдущая", "connection_app", shared | {"page": page - 1, "icon_key": "admin_prev"})
                )
            if page + 1 < len(pages):
                buttons.append(
                    ("Далее", "connection_app", shared | {"page": page + 1, "icon_key": "admin_next"})
                )
            back, back_data = "connection_platform", {"sid": sid, "guide": guide, "platform": platform}
    return {
        "nodes": connection_nodes(body, url, notice, instance),
        "buttons": buttons,
        "back": back,
        "back_data": back_data,
        "static_header": True,
        "compact": True,
        "url_buttons": links,
        "url_buttons_after": True,
    }
