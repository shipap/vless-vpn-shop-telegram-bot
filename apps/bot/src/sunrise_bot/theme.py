"""Telegram presentation only; shared admin settings own the theme values."""

from aiogram.types import InlineKeyboardButton

ICONS = {
    "connect": "🔌",
    "connection_qr": "🔳",
    "subscriptions": "🗂",
    "profile": "👤",
    "bonuses": "🎁",
    "history": "💳",
    "promo": "🏷",
    "help": "💬",
    "products": "🛍",
    "catalog": "🛍",
    "main": "🏠",
    "back": "⬅️",
    "accept_legal": "✅",
    "privacy": "🔒",
    "offer": "📄",
    "cancel": "⛔️",
    "revoke": "🚫",
    "admin": "⚙️",
    "purchase": "➕",
    "renew": "🔄",
    "upgrade": "⬆️",
    "downgrade": "⬇️",
    "devices": "📱",
    "device_add": "🖥",
    "revoke_confirm": "🔓",
    "cancel_confirm": "🛑",
    "enroll_confirm": "🔁",
    "enroll": "🟢",
    "product_select": "📦",
    "period_select": "📆",
    "without_bonus": "💵",
    "bonus_max": "🪙",
    "cash_max": "👛",
    "bonus_cash_max": "💰",
    "checkout": "✔️",
    "payment_sbp": "💠",
    "payment_crypto": "🦋",
    "payment_xrocket": "🚀",
    "payment_bank_card": "💳",
    "order": "🔎",
    "subscription": "🎫",
    "link": "🔗",
    "link_confirm": "🤝",
    "referral": "👥",
    "promo_continue": "▶️",
    "promo_claim": "🎟",
    "infinity": "♾️",
    "menu_heading": "🌐",
    "legal_heading": "⚖️",
    "catalog_heading": "🛒",
    "subscription_heading": "🗃",
    "connect_heading": "🌐",
    "period_heading": "🗓",
    "payment_sources_heading": "💸",
    "quote_heading": "🧾",
    "order_heading": "📑",
    "devices_heading": "💻",
    "promo_heading": "🎟",
    "help_heading": "🛟",
    "admin_heading": "🛠",
    "bonuses_heading": "🎉",
    "history_heading": "📊",
    "profile_heading": "🪪",
    "referral_heading": "🤲",
    "link_heading": "🧩",
    "success_heading": "☑️",
    "error_heading": "⚠️",
    "recurring_heading": "⏳",
    "confirm_heading": "❔",
    "stale_heading": "🔃",
    "profile_id": "🆔",
    "profile_email": "✉️",
    "profile_bonus": "💎",
    "referral_link": "📨",
    "referral_reward": "🏆",
    "referral_count": "👫",
    "quote_target": "🎯",
    "quote_period": "⌛️",
    "quote_price": "💷",
    "quote_discount": "📉",
    "quote_bonus": "🪙",
    "quote_credit": "👛",
    "quote_total": "💳",
    "quote_devices": "📲",
    "quote_expiry": "🕒",
    "quote_validity": "⏱",
    "promo_code": "🔖",
    "promo_reward": "🎁",
    "admin_promos": "🎫",
    "admin_promo_create": "➕",
    "admin_promo_percent": "📉",
    "admin_promo_fixed": "💴",
    "admin_promo_free": "🎁",
    "admin_promo_all": "🌍",
    "admin_promo_new": "🐣",
    "admin_promo_renewal": "🔄",
    "admin_promo_limit": "🔢",
    "admin_promo_confirm": "✅",
    "admin_promos_heading": "🗂",
}

HEADINGS = {
    "Каталог": "catalog_heading",
    "Выберите тариф": "catalog_heading",
    "Подписки": "subscription_heading",
    "Ваши подписки": "subscription_heading",
    "Подключить VPN": "connect_heading",
    "Выберите период": "period_heading",
    "Источники оплаты": "payment_sources_heading",
    "Проверьте заказ": "quote_heading",
    "Ваш заказ": "order_heading",
    "Устройства": "devices_heading",
    "Промокод": "promo_heading",
    "Помощь": "help_heading",
    "FAQ": "help_heading",
    "Администрирование": "admin_heading",
    "Админ-панель": "admin_heading",
    "Бонусы": "bonuses_heading",
    "История платежей": "history_heading",
    "Профиль": "profile_heading",
    "Реферальная система": "referral_heading",
    "Связывание аккаунтов": "link_heading",
    "Объединить аккаунты?": "confirm_heading",
    "Аккаунты связаны": "success_heading",
    "Действие не выполнено": "error_heading",
    "Страница не найдена": "error_heading",
    "Автопродление": "recurring_heading",
    "Меню обновлено": "stale_heading",
    "Отключить устройство?": "confirm_heading",
    "Отключить автопродление?": "confirm_heading",
    "Подключить автопродление?": "confirm_heading",
}


ADMIN_ICON_ALIASES = {
    "connection_qr": "link",
    "notifications": "help",
    "notifications_heading": "help_heading",
    "node_news": "history",
    "admin_finance_heading": "history_heading",
    "admin_warn": "error_heading",
    "admin_block": "revoke",
    "notification": "history",
    "notification_read": "accept_legal",
    "connection_open": "connect",
    "admin_stats": "history",
    "admin_referrals": "referral",
    "admin_products": "products",
    "admin_broadcasts": "help",
    "admin_users": "profile",
    "admin_utm": "connect",
    "admin_settings": "devices",
    "admin_period": "period_select",
    "admin_today": "period_heading",
    "admin_all": "history_heading",
    "admin_product_add": "purchase",
    "admin_edit_name": "profile_id",
    "admin_edit_description": "offer",
    "admin_edit_price": "quote_price",
    "admin_edit_uuid": "link",
    "admin_catalog_intro": "catalog_heading",
    "admin_broadcast_compose": "help_heading",
    "admin_broadcast_test": "confirm_heading",
    "admin_broadcast_start": "checkout",
    "admin_broadcast_stop": "cancel",
    "admin_utm_add": "device_add",
    "admin_next": "upgrade",
    "admin_prev": "downgrade",
    "admin_save": "accept_legal",
    "admin_confirm": "success_heading",
    "admin_toggle": "enroll",
    "admin_fee": "quote_credit",
    "admin_external": "devices_heading",
    "admin_legal": "privacy",
    "admin_stats_heading": "history_heading",
    "admin_referrals_heading": "referral_heading",
    "admin_products_heading": "catalog_heading",
    "admin_broadcasts_heading": "help_heading",
    "admin_users_heading": "profile_heading",
    "admin_utm_heading": "link_heading",
    "admin_settings_heading": "devices_heading",
}


def header_emoji(theme, registry, key="menu_heading", *, registry_alias=None):
    key = key if key in theme.get("icons", {}) or key in registry else ADMIN_ICON_ALIASES.get(key, key)
    if registry.get(key):
        return dict(registry[key])
    if key == "menu_heading" and registry_alias and registry.get(registry_alias):
        return dict(registry[registry_alias])
    entry = theme.get("icons", {}).get(key, {})
    return {
        "type": "emoji",
        "id": entry.get("custom_emoji_id"),
        "fallback": entry.get("fallback") or ICONS.get(key, "🔹"),
        "label": key,
    }


def presentation(theme, key, *, custom_emoji, icon=None, semantic=None):
    requested_key = key
    key = key if key in theme.get("icons", {}) else ADMIN_ICON_ALIASES.get(key, key)
    semantic = semantic or (
        "admin"
        if key == "admin"
        else "success"
        if key in {"accept_legal", "checkout", "enroll"}
        else "danger"
        if key in {"cancel", "revoke", "cancel_confirm", "revoke_confirm"}
        else "primary"
        if key in {"connect", "connection_qr", "catalog", "admin"}
        else "neutral"
    )
    style = theme.get("styles", {}).get(semantic, "primary" if semantic == "admin" else semantic)
    if semantic == "neutral":
        style = "neutral"
    if key == "accept_legal":
        style = "success"
    if requested_key == "connection_qr":
        style = "primary"
    if key in {"back", "main"}:
        style = "danger"
    icons = theme.get("icons", {})
    entry = icon or icons.get(key, {})
    fallback = entry.get("fallback", ICONS.get(key, ""))
    identifier = entry.get("custom_emoji_id")
    fields = {}
    if style in {"primary", "success", "danger"} and "style" in InlineKeyboardButton.model_fields:
        fields["style"] = style
    if (
        custom_emoji
        and isinstance(identifier, str)
        and identifier.isdigit()
        and "icon_custom_emoji_id" in InlineKeyboardButton.model_fields
    ):
        fields["icon_custom_emoji_id"] = identifier
        fallback = ""
    return fallback, fields
