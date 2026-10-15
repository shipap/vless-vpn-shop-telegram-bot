"""Read-only support drafts; credentials and arbitrary record fields are never included."""

import re
from urllib.parse import urlencode, urlsplit

SUPPORT_STATUSES = {
    "active": "Действует",
    "awaiting_payment": "Ожидает оплаты",
    "paid": "Оплачено",
    "paid_provisioning_pending": "Оплачено, подключаем",
    "provisioning_pending": "Подключение ожидает обработки",
    "paid_pending": "Оплачено, ожидает обработки",
    "paid_pending_provisioning": "Оплачено, подключаем",
    "pending_provisioning": "Оплачено, подключаем",
    "provisioning": "Подключаем",
    "pending": "Ожидает обработки",
    "fulfilled": "Доступ выдан",
    "completed": "Завершено",
    "canceled": "Отменено",
    "cancelled": "Отменено",
    "failed": "Не удалось",
    "expired": "Срок истёк",
    "suspended": "Приостановлено",
    "suspension_pending": "Приостановка ожидает проверки",
    "unavailable": "Недоступно",
    "review": "На проверке",
    "reconciliation_required": "Требуется сверка оплаты",
    "refund_review": "Возврат на проверке",
    "unknown": "Не определён",
}
RESERVED_PATHS = frozenset(
    {
        "addemoji",
        "addlist",
        "addstickers",
        "addstyle",
        "addtheme",
        "auction",
        "auth",
        "boost",
        "call",
        "confirmphone",
        "contact",
        "giftcode",
        "invoice",
        "joinchat",
        "login",
        "m",
        "nft",
        "proxy",
        "setlanguage",
        "share",
        "socks",
        "web",
        "a",
        "k",
        "z",
        "c",
        "msg",
        "www",
    }
)


def support_draft_url(url, *, kind, identifier, status):
    if not isinstance(url, str) or re.search(r"[\x00-\x20\\]", url):
        return None
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            return None
        port = parsed.port
    except ValueError:
        return None
    username = parsed.path.removeprefix("/")
    if (
        parsed.hostname != "t.me"
        or port is not None
        or parsed.query
        or parsed.fragment
        or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", username)
        or username.lower() in RESERVED_PATHS
        or username.lower().endswith("bot")
        or kind not in {"order", "subscription"}
        or not isinstance(identifier, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", identifier)
    ):
        return url
    label = "заказу" if kind == "order" else "подписке"
    state = SUPPORT_STATUSES.get(status, "Не определён") if isinstance(status, str) else "Не определён"
    text = f"Нужна помощь по {label}.\nID: {identifier}\nСтатус: {state}"
    return url + "?" + urlencode({"text": text})
