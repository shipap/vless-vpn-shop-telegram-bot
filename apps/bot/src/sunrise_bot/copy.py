MAIN = "Главное меню"
BACK = "Назад"
CONNECT = "Подключить подписку"
STALE = "Меню устарело. Откройте актуальное меню и повторите действие."
FOREIGN = "Это меню открыто другим аккаунтом."
ERROR = "Не удалось выполнить действие. Попробуйте позже или откройте главное меню."
LINK = "Чтобы связать аккаунты, откройте профиль в кабинете и запросите одноразовое подтверждение. Подтвердите здесь полученный код. Совпадение имени или email не связывает аккаунты автоматически."
LINK_BLOCKED = (
    "Связывание пока недоступно: сервис проверки обоих аккаунтов не подключён. Ваши подписки сохранены."
)
NO_SUBSCRIPTIONS = "Подписок пока нет. Выберите тариф, чтобы начать."
ADMIN_AUDIT_REASON = "Подтверждённое действие через Telegram"
NODE_NEWS_PERIOD = "События за последние 24 часа."
NODE_NEWS_PAGE = "Последнее событие на странице для каждого сервера."
NODE_NEWS_EMPTY = "За последние 24 часа событий нет."
NODE_NEWS_NO_SUBSCRIPTION = "Для получения информации о серверах нужна подписка."
NODE_NEWS_HISTORY_EMPTY = "Событий на этой странице больше нет. Откройте актуальные новости."
NODE_NEWS_STATES = {"recovered": "Работа восстановлена", "outage": "Перебои в работе"}
NODE_NEWS_COUNT = "Событий на странице: {count}"
NODE_NEWS_HISTORY = "История на странице: "
NODE_NEWS_NEWEST = "К новым"
CAPABILITY = "Новые подключения автопродления сейчас недоступны. Действующие согласия можно отменить."
LEGAL_BODY = (
    "Перед использованием сервиса ознакомьтесь с Публичной офертой и Политикой конфиденциальности."
    "\n\nНажимая «Принять», вы подтверждаете согласие с указанными документами."
    "\n\nДействующие подписки: /subscriptions. Помощь: /help."
)
LEGAL_UNAVAILABLE = (
    "Документы временно не настроены. Новые покупки недоступны. Действующие подписки: /subscriptions."
)
PROMO_GUIDE = "Отправьте промокод одним сообщением. Он сохранится в вашем аккаунте до использования."
PROMO_INVALID = "Введите один код без пробелов, до 40 символов."
PROMO_PENDING = "Промокод активирован и сохранён. Он будет учтён в подходящем заказе. Новый код можно активировать после использования текущего."
CONNECTION_HELP = "Подписка создана больше суток назад, но первого подключения пока нет. Откройте инструкцию подключения. Если нужна помощь, обратитесь в поддержку."
REFERRAL_GUIDE = "Приглашайте друзей по личной ссылке. Вознаграждение начисляется после подтверждённой оплаты согласно условиям программы."
REFERRAL_UNAVAILABLE = "Ссылка приглашения временно недоступна. Попробуйте открыть этот раздел позже."
BONUS_GUIDE = "Доступную скидку и сумму списания покажем в расчёте заказа."
ADMIN_GUIDE = "Управление доступно в защищённом кабинете после входа по подтверждённому email и дополнительной проверки."
CHECKOUT_LEGAL = (
    "Создавая заказ, вы подтверждаете согласие с текущей Публичной офертой и Политикой конфиденциальности."
)


def retention_blocked(subscription):
    return bool(subscription.get("retention_state") and subscription["retention_state"] != "none")


def retention_notice(subscription):
    from sunrise_bot.dates import moscow_datetime

    state = subscription.get("retention_state")
    if state == "archived":
        return (
            "VPN-подписка удалена из панели. Аккаунт и история платежей сохранены. Можно оформить новую подписку."
            + (
                "\nУдалена: " + moscow_datetime(subscription["archived_at"])
                if subscription.get("archived_at")
                else ""
            )
        )
    if retention_blocked(subscription):
        phase = {
            "pending": "Удаление VPN-подписки ожидает обработки.",
            "in_flight": "Результат удаления VPN-подписки проверяется.",
        }.get(state, "Удаление VPN-подписки требует проверки.")
        return (
            phase
            + " Изменения этой подписки временно недоступны. Аккаунт и история платежей сохраняются. Уже принятые платежи не теряются."
        )
    if subscription.get("cleanup_at"):
        return (
            "Удаление VPN-подписки из панели запланировано через 7 дней после окончания срока при отсутствии препятствий. Аккаунт и история платежей сохранятся."
            + "\nПлановая дата удаления из панели: "
            + moscow_datetime(subscription["cleanup_at"])
        )
    return ""


def renewal_discount(quote):
    selected = quote.get("selected_secondary") or {}
    if quote.get("action") != "renew" or not isinstance(selected.get("percent"), str):
        return ""
    labels = {
        "near_expiry_renewal": "Скидка на продление",
        "renewal_campaign": "Кампания продления",
        "loyalty": "Скидка за стаж",
        "promo": "Промокод",
        "configured_secondary": "Скидка по условиям тарифа",
    }
    return f"{labels.get(selected.get('source'), 'Выбранная скидка')}: {selected['percent']}%"


def notification_actions(item):
    result = []
    operations = {"connect": "connect", "devices": "devices", "renew": "renewal_offer"}
    actions = item.get("actions", [])
    if not isinstance(actions, list):
        return result
    for action in actions:
        if not isinstance(action, dict):
            continue
        metadata = action.get("metadata")
        label = action.get("label")
        if (
            action.get("target") != "/#subscriptions"
            or not isinstance(action.get("operation"), str)
            or action.get("operation") not in operations
            or not isinstance(label, str)
            or not 0 < len(label) <= 200
            or not isinstance(metadata, dict)
            or not isinstance(metadata.get("subscription_id"), str)
            or not 0 < len(metadata["subscription_id"]) <= 128
        ):
            continue
        data = {"sid": metadata["subscription_id"]}
        if metadata.get("quote_id") is not None:
            if not isinstance(metadata["quote_id"], str) or not 0 < len(metadata["quote_id"]) <= 128:
                continue
            data["quote_id"] = metadata["quote_id"]
        result.append((label, operations[action["operation"]], data))
    return result


def engagement_notice(subscription):
    from sunrise_bot.dates import moscow_datetime

    parts = []
    if subscription.get("ever_connected") is True:
        parts.append(
            "Первое подключение подтверждено"
            + (
                ": " + moscow_datetime(subscription["first_connected_at"])
                if subscription.get("first_connected_at")
                else "."
            )
        )
    if subscription.get("traffic_progress_percent") is not None:
        parts.append(f"Использовано лимита трафика: {subscription['traffic_progress_percent']}%.")
    if subscription.get("traffic_warning") is True:
        parts.append("Лимит трафика почти исчерпан.")
    return "\n".join(parts)


def menu_title(instance):
    return f"Меню {instance.app_name}"


def legal_title(instance):
    return f"Добро пожаловать в {instance.app_name}"


def admin_brand(instance):
    return f"{instance.app_name} · управление"


def copy_connection(instance):
    return (
        "Нажмите на ссылку в блоке ниже, чтобы скопировать её. "
        f"Добавьте её в {instance.terminology.client_label} как подписку."
    )


def payment_method_label(method):
    from decimal import Decimal

    percent = str(method["discount_percent"])
    if Decimal(percent) == 0:
        return method["label"]
    return f"{method['label']} (−{percent.replace('.', ',')}%)"


def current_device_limit(value: int | None) -> str:
    if value is None:
        return "Лимит не задан в панели"
    return "Без ограничения" if value == 0 else str(value)
