import asyncio
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from secrets import token_urlsafe
from time import monotonic

from aiogram import Dispatcher, Router
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import BufferedInputFile, CallbackQuery, CopyTextButton, InlineKeyboardButton, Message
from sunrise_core.support_links import support_draft_url
from sunrise_infrastructure.instance_config import InstanceConfig, configured_instance

from sunrise_bot import copy
from sunrise_bot.content import button_content, card, safe_url
from sunrise_bot.dates import moscow_datetime, remaining_days
from sunrise_bot.presentation import Presenter
from sunrise_bot.safety import BotSafety, GuardedSession
from sunrise_bot.theme import HEADINGS, header_emoji, presentation


@dataclass
class MenuAction:
    owner: str
    action: str
    data: dict
    expires: float


class BotApplication:
    """Thin transport adapter; Commerce owns identity, quotes and all mutations."""

    def __init__(
        self,
        bot,
        commerce,
        safety: BotSafety,
        *,
        custom_emoji=False,
        rich_messages=False,
        emoji_registry=None,
        menu_ttl=900,
        accounts=None,
        admin=None,
        admin_console=None,
        payment_provider=None,
        customer_operations=None,
        instance: InstanceConfig | None = None,
    ):
        safety.validate(bot.token)
        if not isinstance(bot.session, GuardedSession):
            bot.session = GuardedSession(bot.session, safety)
        self.bot = bot
        self.commerce = commerce
        self.accounts = accounts
        self.instance = (
            instance if instance is not None else configured_instance(getattr(accounts, "settings", None))
        )
        self.admin = admin
        self.admin_console = admin_console
        self.payment_provider = payment_provider
        self.customer_operations = customer_operations
        from sunrise_bot.admin_flow import AdminFlow
        from sunrise_bot.promo_admin import PromoAdminFlow

        self.admin_flow = AdminFlow(self)
        self.admin_promos = PromoAdminFlow(self)
        self.safety = safety
        self.presenter = Presenter(bot, custom_emoji=custom_emoji, rich_messages=rich_messages)
        self.reported_capabilities = None
        self.emoji_registry = emoji_registry or {}
        self.theme = {}
        self.menu_ttl = menu_ttl
        self.actions = {}
        self.checkout_generations = {}
        self.promo_inputs = {}
        self.locks = {}
        self.dispatcher = Dispatcher()
        router = Router(name="sunrise")
        router.message.register(self.message)
        router.callback_query.register(self.callback)
        self.dispatcher.include_router(router)

    async def call(self, method, *args, **kwargs):
        return await asyncio.to_thread(getattr(self.commerce, method), *args, **kwargs)

    @staticmethod
    def subscription_title(subscription, catalog):
        from sunrise_core.telegram_content import telegram_html_to_content

        product = next((p for p in catalog["products"] if p["id"] == subscription["product_id"]), {})
        name = (product.get("content") or {}).get("name") or telegram_html_to_content(
            subscription.get("product_name") or product.get("name") or subscription["name"]
        )
        return name, product.get("icon")

    @staticmethod
    def period_label(period, currency=None):
        label = f"{period['days']} дней"
        try:
            months = Decimal(str(period.get("months")))
            if months.is_finite() and months > 0:
                unit = "мес."
                if months == months.to_integral_value():
                    count = int(months)
                    unit = (
                        "месяцев"
                        if 11 <= count % 100 <= 14
                        else "месяц"
                        if count % 10 == 1
                        else "месяца"
                        if 2 <= count % 10 <= 4
                        else "месяцев"
                    )
                label = f"{format(months.normalize(), 'f')} {unit} ({label})"
            discount = Decimal(str(period.get("discount_percent", "0")))
            if discount.is_finite() and discount > 0:
                label += f" · скидка {format(discount.normalize(), 'f')}%"
        except InvalidOperation:
            pass
        try:
            price = period.get("price")
            if (
                isinstance(price, bool)
                or not isinstance(currency, str)
                or not re.fullmatch("[A-Z]{3}", currency)
            ):
                raise InvalidOperation()
            amount = Decimal(str(price))
            if not amount.is_finite() or amount < 0:
                raise InvalidOperation()
            label += " · " + money(price, currency)
        except InvalidOperation:
            label += " · цена уточняется в расчёте"
        return label

    def button(self, owner, label, action, *, icon=None, semantic=None, icon_key=None, **data):
        now = monotonic()
        expired = [key for key, item in self.actions.items() if item.expires < now]
        for key in expired:
            del self.actions[key]
            self.presenter.fallback_labels.pop(key, None)
        if len(self.actions) >= 10000:
            oldest = next(iter(self.actions))
            self.actions.pop(oldest)
            self.presenter.fallback_labels.pop(oldest, None)
        token = "s:" + token_urlsafe(12)
        self.actions[token] = MenuAction(str(owner), action, data, now + self.menu_ttl)
        key = icon_key or {
            "products": data.get("intent", "purchase"),
            "periods": "product_select",
            "bonus_choice": "period_select",
            "quote": "without_bonus",
            "device_quote": "device_add",
        }.get(action, action)
        fallback, fields = presentation(
            self.theme,
            key,
            custom_emoji=self.presenter.custom_emoji,
            icon=icon,
            semantic=semantic,
        )
        if action == "main" or icon_key == "back":
            fields["style"] = "danger"
        unicode_icon, _ = presentation(self.theme, key, custom_emoji=False, icon=icon)
        self.presenter.fallback_labels[token] = (unicode_icon + " " + label).strip()
        return InlineKeyboardButton(text=(fallback + " " + label).strip(), callback_data=token, **fields)

    def section(self, key, label, value):
        return {
            "type": "paragraph",
            "children": [
                header_emoji(self.theme, self.emoji_registry, key),
                {"type": "text", "text": " "},
                {"type": "bold", "children": [{"type": "text", "text": label + ": "}]},
                {"type": "text", "text": str(value)},
            ],
        }

    @staticmethod
    def group_sections(sections):
        children = []
        for index, section in enumerate(sections):
            if index:
                children.append({"type": "br"})
            children.extend(section["children"])
        return [{"type": "paragraph", "children": children}]

    async def show(
        self,
        uid,
        title,
        body,
        buttons=(),
        *,
        back="main",
        back_data=None,
        message_id=None,
        nodes=None,
        compact=False,
        static_header=False,
        header_key=None,
        url_buttons=(),
        copy_buttons=(),
        columns=1,
        icon_variants=True,
        url_buttons_after=False,
    ):
        if hasattr(self.commerce, "content_registry"):
            try:
                self.emoji_registry = await self.call("content_registry")
            except Exception:  # noqa: BLE001 -- preserve delivery after optional shared-content failure
                # Контент не должен отменять финансовый результат или ответ об ошибке.
                self.presenter.reason = (
                    "Общий контент временно недоступен; используются сохранённые подписи и Unicode"
                )
        rows = []
        pagination = []
        links = [[self.url_button(label, url, key)] for label, url, key in url_buttons]
        if not url_buttons_after:
            rows.extend(links)
        for label, text in copy_buttons:
            rows.append([InlineKeyboardButton(text=label, copy_text=CopyTextButton(text=text))])
        variants = {}
        for label, action, data in buttons:
            key = data.get("icon_key") or {
                "periods": "product_select",
                "bonus_choice": "period_select",
                "subscription": "subscription",
                "revoke_confirm": "revoke_confirm",
            }.get(action)
            if icon_variants and key and not data.get("icon"):
                variants[key] = variants.get(key, 0) + 1
                variant = f"{key}_{variants[key]}"
                if variant in self.theme.get("icons", {}):
                    data = {**data, "icon_key": variant}
            row = [self.button(uid, label, action, **data)]
            if data.get("icon_key") in {"admin_prev", "admin_next"}:
                pagination.extend(row)
            else:
                rows.append(row)
        if columns > 1:
            rows = [sum(rows[i : i + columns], []) for i in range(0, len(rows), columns)]
        if compact and rows:
            admin_rows = [
                row
                for row in rows
                if self.actions.get(row[0].callback_data)
                and self.actions[row[0].callback_data].action == "admin"
            ]
            rows = [row for row in rows if row not in admin_rows]
            rows = rows[:1] + [sum(rows[i : i + 2], []) for i in range(1, len(rows), 2)] + admin_rows
        if pagination:
            rows.append(pagination)
        if url_buttons_after:
            rows.extend(links)
        if header_key != "menu_heading":
            rows.append(
                [
                    self.button(uid, copy.BACK, back, icon_key="back", **(back_data or {})),
                    self.button(uid, copy.MAIN, "main"),
                ]
            )
        emoji = header_emoji(
            self.theme,
            self.emoji_registry,
            header_key or HEADINGS.get(title, "subscription_heading"),
            registry_alias=self.instance.telegram_menu_emoji_key,
        )
        if nodes and header_key == "menu_heading":
            nodes = [*card(title, "", emoji), *nodes]
        if nodes and static_header:
            nodes = [
                {**nodes[0], "children": [emoji, {"type": "text", "text": " "}, *nodes[0]["children"]]},
                *nodes[1:],
            ]
        await self.presenter.show(uid, nodes or card(title, body, emoji), rows, message_id)
        capability = self.presenter.capability_status()
        if hasattr(self.commerce, "record_bot_capabilities") and capability != self.reported_capabilities:
            try:
                await self.call("record_bot_capabilities", capability)
                self.reported_capabilities = dict(capability)
            except Exception:  # noqa: BLE001 -- optional status recording cannot repeat or undo a completed operation
                self.reported_capabilities = None

    async def main(self, uid, account, message_id=None):
        self.promo_inputs.pop(uid, None)
        if not account.get("id") or "email_verified" not in account:
            account = await self.call("resolve_telegram", int(uid))
        if hasattr(self.commerce, "account_info"):
            account = await self.call("account_info", account["id"])
        legal = await self.call("legal_status", account["id"])
        if not legal.get("accepted"):
            return await self.legal_gate(uid, legal, message_id)
        catalog = await self.call("catalog")
        self.theme = catalog.get("content", {}).get("telegram_theme", {})
        menu = catalog.get("content", {}).get("bot_menu") or None
        buttons = [
            (
                "Каталог",
                "products",
                {"intent": "purchase", "icon_key": "catalog", "semantic": "primary"},
            ),
            ("Подписки", "subscriptions", {}),
            ("Профиль", "profile", {}),
            ("Бонусы", "bonuses", {}),
            ("История платежей", "history", {}),
            ("Уведомления", "notifications", {}),
            ("Новости серверов", "node_news", {}),
            ("Промокод", "promo", {}),
            ("FAQ", "help", {}),
        ]
        if self.admin_allowed(account):
            buttons.append(("Админ-панель", "admin", {}))
        await self.show(
            uid,
            copy.menu_title(self.instance),
            self.instance.terminology.welcome_body,
            buttons,
            message_id=message_id,
            nodes=menu,
            compact=True,
            header_key="menu_heading",
        )

    def admin_allowed(self, account):
        from sunrise_infrastructure.staff_permissions import telegram_admin_ids

        settings = getattr(self.accounts, "settings", None)
        identifier = account.get("telegram_id")
        return bool(
            type(identifier) is str
            and re.fullmatch(r"[1-9][0-9]{0,18}", identifier)
            and int(identifier) in telegram_admin_ids(settings)
        )

    async def deliver_owner_alerts(self):
        settings = getattr(self.accounts, "settings", None)
        owner = getattr(settings, "owner_id", None)
        console = self.admin_flow.console
        if not owner or not str(owner).isdigit() or not self.safety.permits(str(owner)) or not console:
            return 0
        uid = str(owner)
        async with self.locks.setdefault(uid, asyncio.Lock()):
            account = await self.call("resolve_telegram", int(owner))
            if hasattr(self.commerce, "account_info"):
                account = await self.call("account_info", account["id"])
            alerts = await asyncio.to_thread(console.pending_owner_alerts, account["id"], limit=10)
            count = 0
            for item in alerts.get("items", []):
                if str(item.get("target_id")) != uid:
                    continue
                item = await asyncio.to_thread(console.claim_owner_alert, account["id"], item["id"])
                if item is None:
                    continue
                payload = item["payload"]
                target = str(payload["account_id"])
                identity = (
                    "@" + str(payload["telegram_username"])
                    if payload.get("telegram_username")
                    else str(payload.get("telegram_id") or payload.get("email") or target)
                )
                body = (
                    f"Пользователь: {identity}\nВнутренний ID: {target}\n"
                    f"Telegram ID: {payload.get('telegram_id') or 'не привязан'}\n"
                    f"Устройство: {str(payload.get('device_name') or 'Название недоступно')[:120]}\n"
                    f"Отвязки за {payload.get('month', 'текущий месяц')} · МСК: {payload.get('unlink_month', 0)}\n"
                    f"За всё время: {payload.get('unlink_total', 0)}\n"
                    f"По одному · за месяц: {payload.get('unlink_individual_month', 0)} · всего: {payload.get('unlink_individual_total', 0)}\n"
                    f"Все устройства · за месяц: {payload.get('unlink_bulk_month', 0)} · всего: {payload.get('unlink_bulk_total', 0)}\n"
                    f"Предупреждения: {payload.get('current_warnings', 0)}/2 · всего: {payload.get('total_warnings', 0)}\n"
                    "Блокировка относится только к отвязке устройств."
                )
                try:
                    await self.show(
                        uid,
                        "Устройство отвязано",
                        body,
                        [
                            (
                                "Предупредить",
                                "admin_user_moderate",
                                {
                                    "target_id": target,
                                    "moderation_action": "warn",
                                    "icon_key": "error_heading",
                                },
                            ),
                            (
                                "Блокировать отвязку",
                                "admin_user_moderate",
                                {
                                    "target_id": target,
                                    "moderation_action": "block",
                                    "icon_key": "revoke",
                                    "semantic": "danger",
                                },
                            ),
                            (
                                "Карточка пользователя",
                                "admin_user",
                                {"target_id": target, "icon_key": "profile"},
                            ),
                        ],
                        back="admin_users",
                        header_key="devices_heading",
                    )
                except Exception as exc:  # noqa: BLE001 -- persist uncertain outcome before runtime resumes
                    state, retry_after = "review", None
                    if (
                        isinstance(exc, TelegramRetryAfter)
                        and type(exc.retry_after) is int
                        and 1 <= exc.retry_after <= 3600
                    ):
                        state, retry_after = "pending", exc.retry_after
                    elif isinstance(exc, (TelegramForbiddenError, PermissionError)):
                        state = "blocked"
                    try:
                        await asyncio.to_thread(
                            console.fail_owner_alert,
                            account["id"],
                            item["id"],
                            state=state,
                            retry_after=retry_after,
                        )
                    except Exception:  # noqa: BLE001 -- an unrecorded outcome remains in_flight until review
                        self.owner_alert_delivery = "review_pending"
                    raise
                await asyncio.to_thread(console.acknowledge_owner_alert, account["id"], item["id"])
                count += 1
            return count

    async def legal_gate(self, uid, legal, mid=None):
        catalog = await self.call("catalog")
        self.theme = catalog.get("content", {}).get("telegram_theme", {})
        self.emoji_registry = catalog.get("content", {}).get("emoji_registry", self.emoji_registry)
        docs = {doc["document_type"]: doc for doc in legal.get("documents", [])}
        rows = []
        if legal.get("configured") and all(
            safe_url(docs.get(k, {}).get("url")) for k in ("privacy", "offer")
        ):
            rows = [
                [self.url_button("Политика конфиденциальности", docs["privacy"]["url"], "privacy")],
                [self.url_button("Публичная оферта", docs["offer"]["url"], "offer")],
                [self.button(uid, "Принять", "accept_legal", documents=legal["documents"])],
            ]
        await self.presenter.show(
            uid,
            card(
                copy.legal_title(self.instance),
                copy.LEGAL_BODY if rows else copy.LEGAL_UNAVAILABLE,
                header_emoji(self.theme, self.emoji_registry, "legal_heading"),
            ),
            rows,
            mid,
        )

    def url_button(self, label, url, key):
        semantic = "neutral" if key == "connection_open" else None
        fallback, fields = presentation(
            self.theme, key, custom_emoji=self.presenter.custom_emoji, semantic=semantic
        )
        unicode_icon, _ = presentation(self.theme, key, custom_emoji=False, semantic=semantic)
        self.presenter.fallback_labels[url] = (unicode_icon + " " + label).strip()
        return InlineKeyboardButton(text=(fallback + " " + label).strip(), url=url, **fields)

    async def message(self, message: Message):
        if not message.from_user or message.from_user.is_bot or message.chat.type != "private":
            return
        uid = str(message.from_user.id)
        if str(message.chat.id) != uid or not self.safety.permits(uid):
            return
        async with self.locks.setdefault(uid, asyncio.Lock()):
            try:
                text = (message.text or "").strip()
                if text.startswith("/unlink "):
                    proof = text.split(maxsplit=1)[1]
                    if not self.accounts or not re.fullmatch(
                        r"[A-Za-z0-9_-]{1,64}\.[A-Za-z0-9_-]{20,100}", proof
                    ):
                        return await self.show(uid, "Отвязка Telegram", "Подтверждение недействительно.")
                    return await self.presenter.show(
                        uid,
                        card(
                            "Отвязать Telegram?",
                            "Подтвердите отвязку этого Telegram от аккаунта сайта. Оплаченные подписки и текущий вход в кабинет сохранятся. Подтверждение действует 5 минут.",
                        ),
                        [
                            [
                                self.button(
                                    uid,
                                    "Подтвердить отвязку",
                                    "telegram_unlink_confirm",
                                    proof=proof,
                                    semantic="danger",
                                )
                            ]
                        ],
                    )
                referral = re.fullmatch(r"/start (ref_[1-9][0-9]{0,18})", text)
                account = (
                    await self.call(
                        "resolve_telegram",
                        message.from_user.id,
                        username=message.from_user.username,
                        referral_code=referral[1],
                    )
                    if referral
                    else await self.call(
                        "resolve_telegram", message.from_user.id, username=message.from_user.username
                    )
                )
                start = re.fullmatch(r"/start(?:@\w+)? (utm_[A-Za-z0-9_-]{1,64})", text)
                console = self.admin_console or getattr(self.admin, "bot_console", None)
                if start and console:
                    await asyncio.to_thread(console.record_utm_start, account["id"], start[1])
                if await self.admin_flow.message(uid, account, message):
                    return
                if await self.admin_promos.message(uid, account, text):
                    return
                if message.text in {"/subscriptions", "/help"}:
                    return await self.execute(
                        uid,
                        account["id"],
                        "subscriptions" if message.text == "/subscriptions" else "help",
                        {},
                        None,
                        "",
                    )
                if text == "/promo":
                    return await self.execute(uid, str(account["id"]), "promo", {}, None, "")
                context = self.promo_inputs.get(uid)
                if context and not text.startswith("/"):
                    if context.expires < monotonic() or context.owner != str(account["id"]):
                        self.promo_inputs.pop(uid, None)
                        return await self.main(uid, account)
                    if not text or len(text) > 40 or any(char.isspace() for char in text):
                        return await self.show(uid, "Промокод", copy.PROMO_INVALID)
                    legal = await self.call("legal_status", account["id"])
                    if not legal.get("accepted"):
                        return await self.legal_gate(uid, legal)
                    await self.call("activate_promo", account["id"], text)
                    self.promo_inputs.pop(uid, None)
                    return await self.execute(uid, str(account["id"]), "promo", {}, None, "")
                # Explicit Telegram proof is submitted to a backend challenge, never accepted locally.
                if message.text and message.text.startswith("/link "):
                    proof = message.text.split(maxsplit=1)[1].strip()
                    if self.accounts and 10 <= len(proof) <= 200:
                        catalog = await self.call("catalog")
                        self.theme = catalog.get("content", {}).get("telegram_theme", {})
                        await self.show(
                            uid,
                            "Объединить аккаунты?",
                            "Вы подтверждаете привязку этого Telegram к аккаунту, в котором запросили код. Все подписки и покупки сохранятся. Старые сеансы входа будут завершены. Код одноразовый, действует 5 минут.",
                            [("Объединить аккаунты", "link_confirm", {"proof": proof})],
                        )
                    else:
                        await self.show(uid, "Связывание аккаунтов", copy.LINK_BLOCKED)
                    return
                await self.main(uid, account)
            except Exception as exc:  # noqa: BLE001 -- transport boundary renders no infrastructure error details
                await self.error(uid, exc)

    def callback_action(self, callback_data, uid):
        # Transactional notices survive restarts; these tokens only open the sender's own menu.
        if callback_data == "notice:subscriptions":
            return MenuAction(uid, "subscriptions", {"show_list": True}, float("inf"))
        if callback_data == "notice:main":
            return MenuAction(uid, "main", {}, float("inf"))
        if self.accounts:
            from sunrise_core.notice_callbacks import parse_owner_notice_callback

            parsed = parse_owner_notice_callback(
                callback_data, getattr(getattr(self.accounts, "settings", None), "secret_key", None)
            )
            if parsed:
                operation, event_id = parsed
                return MenuAction(
                    uid, "owner_device_notice", {"operation": operation, "event_id": event_id}, float("inf")
                )
        return self.actions.get(callback_data)

    async def callback(self, callback: CallbackQuery):
        uid = str(callback.from_user.id)
        if not self.safety.permits(uid) or callback.from_user.is_bot:
            return
        if (
            not callback.message
            or callback.message.chat.type != "private"
            or str(callback.message.chat.id) != uid
        ):
            return
        item = self.callback_action(callback.data, uid)
        # Answer first so DB/provider latency can never leave an endless Telegram spinner.
        try:
            await callback.answer(copy.FOREIGN if item and item.owner != uid else None)
        except TelegramAPIError:
            pass
        if item and item.owner != uid:
            return
        async with self.locks.setdefault(uid, asyncio.Lock()):
            item = self.callback_action(callback.data, uid)
            if item and item.owner != uid:
                return
            try:
                if item and item.action == "telegram_unlink_confirm":
                    if item.expires < monotonic() or not self.accounts:
                        return await self.presenter.show(
                            uid,
                            card("Отвязка Telegram", "Подтверждение истекло. Запросите новое в кабинете."),
                            [],
                        )
                    from sunrise_infrastructure.account_features import AccountsFeatures

                    await asyncio.to_thread(
                        AccountsFeatures(self.accounts).confirm_unlink,
                        callback.from_user.id,
                        item.data["proof"],
                    )
                    # Leave this owner-bound action for safe idempotent replay; never resolve/create an account here.
                    url = safe_url(getattr(self.accounts.settings, "dashboard_url", None))
                    rows = [[self.url_button("Открыть кабинет", url, "profile")]] if url else []
                    return await self.presenter.show(
                        uid,
                        card(
                            "Telegram отвязан",
                            "Привязка удалена. Оплаченный доступ и текущая сессия сайта сохранены. Для управления аккаунтом откройте кабинет.",
                        ),
                        rows,
                        callback.message.message_id,
                    )
                account = await self.call(
                    "resolve_telegram", callback.from_user.id, username=callback.from_user.username
                )
                if item and item.action == "owner_device_notice":
                    from sunrise_core.errors import missing

                    if not await self.admin_flow.authorized(uid, account["id"]):
                        raise missing()
                    target = await self.admin_flow.call(
                        "bot_owner_device_notice", account["id"], item.data["event_id"]
                    )
                    data = {"target_id": target["account_id"]}
                    action = "admin_user"
                    if item.data["operation"] in {"w", "b"}:
                        action = "admin_user_moderate"
                        data["moderation_action"] = "warn" if item.data["operation"] == "w" else "block"
                        data["notice_event_id"] = target["event_id"]
                    return await self.admin_flow.execute(
                        uid, account["id"], action, data, callback.message.message_id
                    )
                if not item or item.expires < monotonic():
                    await self.main(uid, account, callback.message.message_id)
                    return
                await self.execute(
                    uid,
                    account["id"],
                    item.action,
                    item.data,
                    callback.message.message_id,
                    callback.data,
                )
            except Exception as exc:  # noqa: BLE001 -- transport boundary renders no infrastructure error details
                await self.error(uid, exc, callback.message.message_id)

    async def error(self, uid, exc, message_id=None):
        # Only domain errors contain public messages; never stringify SDK/SQL/credential errors.
        public = getattr(exc, "message", None) if type(exc).__name__ == "DomainError" else None
        if type(exc).__name__ == "DomainError" and getattr(exc, "code", "") in {
            "legal_versions_stale",
            "legal_consent_required",
            "legal_not_configured",
        }:
            account = await self.call("resolve_telegram", int(uid))
            return await self.legal_gate(uid, await self.call("legal_status", account["id"]), message_id)
        await self.show(uid, "Действие не выполнено", public or copy.ERROR, message_id=message_id)

    async def subscription(self, aid, sid, *, refresh_panel=False):
        dashboard = (
            await self.call("dashboard", aid, refresh_panel=True, refresh_subscription_id=sid)
            if refresh_panel
            else await self.call("dashboard", aid)
        )
        sub = next((x for x in dashboard["subscriptions"] if x["id"] == sid), None)
        if sub is None:
            from sunrise_core.errors import missing

            raise missing()
        return sub

    async def agreement_subscription(self, aid, sid):
        dashboard = await self.call("dashboard", aid)
        item = next((sub for sub in dashboard["subscriptions"] if sub["id"] == sid), None)
        if item:
            return item
        item = next(
            (
                sub
                for sub in dashboard.get("archived_subscription_agreements", [])
                if sub["subscription_id"] == sid
            ),
            None,
        )
        if item is None:
            from sunrise_core.errors import missing

            raise missing()
        return item

    async def customer_call(self, method, *args, **kwargs):
        if self.customer_operations is None:
            from sunrise_core.errors import DomainError

            raise DomainError("panel_unavailable", "Управление устройствами временно недоступно.", 503)
        return await asyncio.to_thread(getattr(self.customer_operations, method), *args, **kwargs)

    @staticmethod
    def observed_inventory(inventory):
        return (
            inventory.get("source") == "remnawave"
            and bool(inventory.get("observed_at"))
            and not inventory.get("error")
        )

    async def device_selection(self, aid, data):
        from sunrise_core.errors import DomainError

        sub = await self.subscription(aid, data["sid"])
        if sub.get("credential_rotation_state") in {"pending", "in_flight", "review"}:
            raise DomainError(
                "credential_rotation_pending", "Обновление ссылки ожидает завершения или проверки.", 409
            )
        inventory = await self.customer_call("inventory", aid, data["sid"])
        expected = data.get("selection")
        current = {d["id"]: d.get("generation") for d in inventory["devices"] if not d.get("revoked")}
        if not self.observed_inventory(inventory):
            raise DomainError(
                "panel_unavailable",
                "Панель временно недоступна. Отвязка не отправлена. Попробуйте обновить список позже.",
                503,
            )
        if (
            not expected
            or any(current.get(did) != generation for did, generation in expected)
            or data.get("bulk")
            and set(current) != {did for did, _ in expected}
        ):
            raise DomainError(
                "devices_changed",
                "Список устройств изменился. Обновите его и подтвердите действие заново.",
                409,
            )
        return inventory

    async def devices_screen(self, uid, aid, data, mid):
        sub = await self.subscription(aid, data["sid"])
        inventory = (
            await self.customer_call(
                "inventory", aid, sub["id"], refresh=data.get("refresh") is True or "page" not in data
            )
            if self.customer_operations is not None
            else {"source": "unavailable", "devices": [], "error": "panel_unavailable"}
        )
        rotation = sub.get("credential_rotation_state")
        known = self.observed_inventory(inventory)
        active = [d for d in inventory["devices"] if not d.get("revoked")]
        page = max(0, min(int(data.get("page", 0)), max(0, (len(active) - 1) // 5)))
        view = active[page * 5 : (page + 1) * 5]
        can_mutate = known and rotation not in {"pending", "in_flight", "review"}
        buttons = []
        if can_mutate:
            buttons += [
                (
                    "Отключить: " + d["name"][:80],
                    "revoke_confirm",
                    {
                        "sid": sub["id"],
                        "selection": [(d["id"], d["generation"])],
                        "icon_key": "revoke_confirm",
                    },
                )
                for d in view
            ]
            if 1 <= len(active) <= 100:
                buttons.append(
                    (
                        "Отключить все",
                        "revoke_confirm",
                        {
                            "sid": sub["id"],
                            "selection": [(d["id"], d["generation"]) for d in active],
                            "bulk": True,
                            "icon_key": "revoke",
                        },
                    )
                )
            buttons.append(("Перевыпустить ссылку", "rotate_confirm", {"sid": sub["id"], "icon_key": "link"}))
        if self.customer_operations is not None:
            buttons.append(
                (
                    "Обновить список",
                    "devices",
                    {"sid": sub["id"], "page": page, "refresh": True, "icon_key": "order"},
                )
            )
        buttons.append(("Докупить устройство", "device_quote", {"sid": sub["id"], "icon_key": "device_add"}))
        if page:
            buttons.append(
                ("Предыдущие", "devices", {"sid": sub["id"], "page": page - 1, "icon_key": "admin_prev"})
            )
        if (page + 1) * 5 < len(active):
            buttons.append(
                ("Далее", "devices", {"sid": sub["id"], "page": page + 1, "icon_key": "admin_next"})
            )
        body = f"Лимит: {copy.current_device_limit(sub['devices_limit'])}\n"
        body += (
            ("\n".join(d["name"][:100] for d in view) or "Активных устройств нет.")
            if known
            else "Актуальный список устройств недоступен. Обновите данные панели."
        )
        body += "\n\nТрафик: " + traffic(inventory.get("traffic_bytes"))
        body += "\nДанные панели: " + moscow_datetime(inventory.get("observed_at"))
        if rotation:
            body += "\nОбновление ссылки: " + (
                "требуется ручная проверка; повторный запрос запрещён."
                if rotation == "review"
                else "ожидается подтверждение панели."
            )
        if data.get("origin"):
            buttons = [
                (label, action, {**payload, "origin": data["origin"]}) for label, action, payload in buttons
            ]
        return await self.show(
            uid,
            "Устройства",
            body,
            buttons,
            back="profile" if data.get("origin") == "profile" else "subscription",
            back_data={"sid": sub["id"]},
            message_id=mid,
        )

    async def execute(self, uid, aid, action, data, mid, token, *, subscription_snapshot=None):
        uid = str(uid)
        checkout_generation = self.checkout_generations.get(uid, 0)
        if action == "checkout" and data.get("_created_order_id"):
            data = {**data, "order_id": data["_created_order_id"]}
            action = "order"
        elif action == "checkout" and data.get("_checkout_generation", 0) != self.checkout_generations.get(
            uid, 0
        ):
            return await self.show(uid, "Расчёт устарел", copy.STALE, message_id=mid)
        if action in {
            "products",
            "periods",
            "bonus_choice",
            "quote",
            "bonus_max",
            "cash_max",
            "bonus_cash_max",
            "renewal_offer",
            "device_quote",
        }:
            checkout_generation += 1
            self.checkout_generations[uid] = checkout_generation
        if action != "promo":
            self.promo_inputs.pop(uid, None)
        catalog = await self.call("catalog")
        self.theme = catalog.get("content", {}).get("telegram_theme", {})
        selection_back = (
            "products"
            if data.get("intent") in {"upgrade", "downgrade"}
            else "devices"
            if data.get("intent") == "devices"
            else "periods"
        )
        operation_entry = (
            action == "periods"
            and data.get("intent") == "renew"
            or action == "products"
            and data.get("intent") in {"upgrade", "downgrade"}
            or action == "device_quote"
        )
        if data.get("sid") and action in {
            "connect",
            "connection_platform",
            "connection_app",
            "connection_qr",
            "devices",
            "products",
            "periods",
            "bonus_choice",
            "quote",
            "renewal_offer",
            "bonus_max",
            "cash_max",
            "bonus_cash_max",
            "device_quote",
            "revoke_confirm",
            "revoke",
            "rotate_confirm",
            "rotate",
            "enroll_confirm",
            "enroll",
            "promo_claim_confirm",
            "promo_claim_apply",
        }:
            sub = subscription_snapshot or await self.subscription(
                aid, data["sid"], refresh_panel=operation_entry
            )
            subscription_snapshot = sub
            if copy.retention_blocked(sub):
                return await self.execute(
                    uid, aid, "subscription", {"sid": sub["id"]}, mid, token, subscription_snapshot=sub
                )
            if operation_entry:
                if sub.get("renewal_pricing_state") == "blocked":
                    from sunrise_core.errors import DomainError

                    raise DomainError(
                        "panel_sync_required",
                        "Не удалось проверить данные панели. Обновите подписку перед выбранным действием.",
                        409,
                    )
                if data.get("intent") == "renew":
                    data = {**data, "product_id": sub["product_id"]}
        if action.startswith("admin_promo"):
            self.admin_flow.cancel(uid)
            return await self.admin_promos.execute(uid, aid, action, data, mid)
        self.admin_promos.cancel(uid)
        if action.startswith("admin_") or action == "admin" and self.admin_flow.console is not None:
            return await self.admin_flow.execute(uid, aid, action, data, mid)
        self.admin_flow.cancel(uid)
        if action == "accept_legal":
            legal = await self.call("legal_status", aid)
            if data.get("documents") != legal.get("documents"):
                return await self.legal_gate(uid, legal, mid)
            await self.call("accept_legal", aid, data["documents"], "telegram", "first_use")
            self.actions.pop(token, None)
            return await self.main(uid, {"id": aid}, mid)
        if action in {
            "products",
            "periods",
            "bonus_choice",
            "quote",
            "bonus_max",
            "cash_max",
            "bonus_cash_max",
            "checkout",
            "device_quote",
            "enroll",
            "enroll_confirm",
            "promo_claim",
            "promo_claim_confirm",
            "promo_claim_apply",
        }:
            legal = await self.call("legal_status", aid)
            if not legal.get("accepted"):
                return await self.legal_gate(uid, legal, mid)
        if action == "promo":
            legal = await self.call("legal_status", aid)
            if not legal.get("accepted"):
                return await self.legal_gate(uid, legal, mid)
            active = await self.call("active_promo", aid)
            if active:
                self.promo_inputs.pop(uid, None)
                buttons = (
                    [("Получить доступ", "promo_claim", {"code": active["code"]})]
                    if active.get("reward_type") == "free_days" and active.get("available", True)
                    else [
                        ("Открыть каталог", "products", {"intent": "purchase", "icon_key": "promo_continue"})
                    ]
                )
                note = active.get("unavailable_reason") or copy.PROMO_PENDING
                return await self.show(
                    uid,
                    "Промокод",
                    "",
                    buttons,
                    message_id=mid,
                    nodes=card("Промокод", note)
                    + [
                        self.section("promo_code", "Код", active["code"]),
                        self.section("promo_reward", "Награда", promo_reward(active)),
                    ],
                    static_header=True,
                )
            if len(self.promo_inputs) >= 10000:
                self.promo_inputs.pop(next(iter(self.promo_inputs)))
            self.promo_inputs[uid] = MenuAction(str(aid), "promo", {}, monotonic() + self.menu_ttl)
            return await self.show(
                uid,
                "Промокод",
                copy.PROMO_GUIDE,
                message_id=mid,
            )
        if action == "promo_claim":
            dash = await self.call("dashboard", aid)
            eligible_ids = set(await self.call("promo_claim_targets", aid))
            active_subs = [sub for sub in dash["subscriptions"] if sub["id"] in eligible_ids]
            if len(active_subs) > 1:
                return await self.show(
                    uid,
                    "Промокод",
                    "Выберите подписку, к которой добавить бесплатные дни.",
                    [
                        (
                            sub["name"],
                            "promo_claim_confirm",
                            {**data, "sid": sub["id"], "icon_key": "subscription"},
                        )
                        for sub in active_subs
                    ],
                    back="promo",
                    message_id=mid,
                )
            return await self.execute(
                uid,
                aid,
                "promo_claim_confirm",
                {**data, "sid": active_subs[0]["id"] if active_subs else None},
                mid,
                token,
            )
        if action == "promo_claim_confirm":
            active = await self.call("active_promo", aid)
            if (
                not active
                or active["code"] != data["code"]
                or active.get("reward_type") != "free_days"
                or not active.get("available", True)
            ):
                return await self.execute(uid, aid, "promo", {}, mid, token)
            target = (
                (await self.subscription(aid, data["sid"]))["name"]
                if data.get("sid")
                else "Новая подписка по тарифу промокода"
            )
            return await self.show(
                uid,
                "Промокод",
                f"{promo_reward(active)}\nПодписка: {target}\nТариф и оплаченные устройства действующей подписки сохраняются.",
                [
                    (
                        "Подтвердить",
                        "promo_claim_apply",
                        {**data, "icon_key": "promo_claim", "semantic": "success"},
                    )
                ],
                back="promo",
                message_id=mid,
            )
        if action == "promo_claim_apply":
            result = await self.call("claim_promo", aid, data["code"], subscription_id=data.get("sid"))
            self.actions.pop(token, None)
            buttons = (
                [("Открыть подписку", "subscription", {"sid": result["subscription_id"]})]
                if result.get("subscription_id")
                else [("Мои подписки", "subscriptions", {})]
            )
            return await self.show(
                uid,
                "Промокод",
                "Промокод использован. Бесплатные дни сохранены; доступ подключается. Повторно активировать код не нужно.",
                buttons,
                message_id=mid,
                header_key="success_heading",
            )
        if action == "help" or action.startswith("faq_"):
            return await self.faq(uid, aid, action, mid)
        if action == "admin":
            account = await self.call("resolve_telegram", int(uid))
            if hasattr(self.commerce, "account_info"):
                account = await self.call("account_info", account["id"])
            if not self.admin_allowed(account):
                return await self.show(uid, "Страница не найдена", "404", message_id=mid)
            return await self.show(
                uid,
                "Админ-панель",
                copy.ADMIN_GUIDE,
                [("Промокоды", "admin_promos", {"icon_key": "admin_promos"})] if self.admin else (),
                message_id=mid,
            )
        if action == "main":
            return await self.main(uid, {}, mid)
        if action in {"notifications", "notification", "notification_read"} and self.accounts:
            from sunrise_infrastructure.notifications import NotificationService

            service = NotificationService(self.accounts.db, self.accounts.settings)
            offset = data.get("offset", 0)
            if type(offset) is not int or offset < 0 or offset % 5:
                from sunrise_core.errors import missing

                raise missing()
            if action == "notification_read":
                await asyncio.to_thread(service.mark_read, aid, data["id"])
                return await self.execute(uid, aid, "notifications", {"offset": offset}, mid, token)
            inbox = await asyncio.to_thread(service.list, aid, limit=5, offset=offset)
            if action == "notification":
                item = next((item for item in inbox["items"] if item["id"] == data["id"]), None)
                if not item:
                    from sunrise_core.errors import missing

                    raise missing()
                return await self.show(
                    uid,
                    item["title"],
                    item["body"],
                    [
                        *copy.notification_actions(item),
                        ("Отметить прочитанным", "notification_read", {"id": item["id"], "offset": offset}),
                    ],
                    back="notifications",
                    back_data={"offset": offset},
                    message_id=mid,
                    nodes=card(item["title"], "") + item["nodes"] if item.get("nodes") else None,
                )
            buttons = [
                (item["title"][:80], "notification", {"id": item["id"], "offset": offset})
                for item in inbox["items"][:5]
            ]
            for field, caption, icon in (
                ("previous_offset", "Предыдущие", "admin_prev"),
                ("next_offset", "Далее", "admin_next"),
            ):
                if inbox.get(field) is not None:
                    buttons.append((caption, "notifications", {"offset": inbox[field], "icon_key": icon}))
            return await self.show(
                uid,
                "Уведомления",
                "Непрочитано: "
                + str(inbox["unread_count"])
                + "\n\n"
                + ("\n".join(item["title"] for item in inbox["items"]) or "Уведомлений пока нет."),
                buttons,
                message_id=mid,
            )
        if action == "subscriptions":
            dashboard = await self.call("dashboard", aid, refresh_panel=True)
            subs = dashboard["subscriptions"]
            notices = dashboard.get("subscription_deletion_notices", [])
            agreements = dashboard.get("archived_subscription_agreements", [])
            subscription_buttons = []
            for sub in subs:
                name, icon = self.subscription_title(sub, catalog)
                label, icon = button_content(name, icon)
                label += f" ({remaining_days(sub.get('expires_at'))})"
                subscription_buttons.append(
                    (label, data.get("next_action", "subscription"), {**data, "sid": sub["id"], "icon": icon})
                )
            if notices or agreements:
                body = "Выберите подписку." if subs else copy.NO_SUBSCRIPTIONS
                for item in notices:
                    body += f"\n\n{item['name']}\nПодписка была удалена. Если это ошибка, обратитесь в поддержку.\n{moscow_datetime(item['deleted_at'])}"
                buttons = subscription_buttons
                buttons.append(("Добавить подписку", "products", {"intent": "purchase"}))
                if agreements:
                    buttons.append(("Автопродление удалённых подписок", "archived_agreements", {}))
                support = safe_url(self.instance.support_url)
                return await self.show(
                    uid,
                    "Подписки",
                    body,
                    buttons,
                    back="profile" if data.get("origin") == "profile" else "main",
                    message_id=mid,
                    url_buttons=[("Поддержка", support, "help")] if support else (),
                )
            if not subs:
                return await self.show(
                    uid,
                    "Подписки",
                    copy.NO_SUBSCRIPTIONS,
                    [("Добавить подписку", "products", {"intent": "purchase"})],
                    back="profile" if data.get("origin") == "profile" else "main",
                    message_id=mid,
                )
            if len(subs) == 1 and not data.get("show_list"):
                return await self.execute(
                    uid,
                    aid,
                    data.get("next_action", "subscription"),
                    {**data, "sid": subs[0]["id"]},
                    mid,
                    token,
                    subscription_snapshot=subs[0],
                )
            return await self.show(
                uid,
                "Ваши подписки",
                "Выберите подписку.",
                subscription_buttons,
                back="profile" if data.get("origin") == "profile" else "main",
                message_id=mid,
            )
        if action == "node_news":
            feed = await self.call("node_news", aid, before=data.get("before"), limit=5)
            page = {"before": data["before"]} if data.get("before") else {}
            groups = {}
            for item in feed["items"]:
                groups.setdefault(item["title"], []).append(item)
            server = data.get("server")
            selected = {server: groups[server]} if server in groups else {} if server else groups
            nodes = card(
                "Новости серверов",
                copy.NODE_NEWS_PERIOD,
                header_emoji(self.theme, self.emoji_registry, "node_news"),
            )
            if not selected:
                empty = copy.NODE_NEWS_HISTORY_EMPTY if server else copy.NODE_NEWS_EMPTY
                if not feed["items"]:
                    dashboard = await self.call("dashboard", aid)
                    if not dashboard["subscriptions"]:
                        empty = copy.NODE_NEWS_NO_SUBSCRIPTION
                nodes += card("", empty)[1:]
            elif not server and page:
                nodes += card("", copy.NODE_NEWS_PAGE)[1:]
            buttons = []
            for title, events in selected.items():
                children = [{"type": "bold", "children": [{"type": "text", "text": title}]}]
                for item in events if server else events[:1]:
                    state = "recovered" if item["state"] == "recovered" else "outage"
                    children += [
                        {"type": "br"},
                        header_emoji(
                            self.theme,
                            self.emoji_registry,
                            "success_heading" if state == "recovered" else "error_heading",
                        ),
                        {
                            "type": "text",
                            "text": " "
                            + copy.NODE_NEWS_STATES[state]
                            + " · "
                            + moscow_datetime(item["occurred_at"]),
                        },
                    ]
                if not server:
                    if len(events) > 1:
                        children += [
                            {"type": "br"},
                            {"type": "text", "text": copy.NODE_NEWS_COUNT.format(count=len(events))},
                        ]
                    buttons.append(
                        (
                            (copy.NODE_NEWS_HISTORY + title)[:80],
                            "node_news",
                            {**page, "server": title, "icon_key": "node_news"},
                        )
                    )
                nodes.append({"type": "paragraph", "children": children})
            if not server and feed.get("next_cursor"):
                buttons.append(
                    (
                        "Более ранние события",
                        "node_news",
                        {"before": feed["next_cursor"], "icon_key": "admin_next"},
                    )
                )
            if page:
                buttons.append((copy.NODE_NEWS_NEWEST, "node_news", {"icon_key": "admin_prev"}))
            return await self.show(
                uid,
                "Новости серверов",
                "",
                buttons,
                back="node_news" if server else "main",
                back_data=page if server else None,
                message_id=mid,
                nodes=nodes,
                header_key="node_news",
            )
        if action in {"archived_agreements", "archived_agreement"}:
            dashboard = await self.call("dashboard", aid)
            agreements = dashboard.get("archived_subscription_agreements", [])
            if action == "archived_agreements":
                return await self.show(
                    uid,
                    "Автопродление удалённых подписок",
                    "Действующие согласия можно отменить.",
                    [
                        (item["name"], "archived_agreement", {"sid": item["subscription_id"]})
                        for item in agreements
                    ],
                    message_id=mid,
                )
            item = next((item for item in agreements if item["subscription_id"] == data["sid"]), None)
            if item is None:
                from sunrise_core.errors import missing

                raise missing()
            recurring = item["recurring"]
            amount = (
                money(recurring["amount"], recurring["currency"])
                if recurring.get("amount") is not None and recurring.get("currency")
                else "Сумма неизвестна"
            )
            return await self.show(
                uid,
                item["name"],
                f"Автопродление: {status(recurring['state'])}\n{amount}",
                [("Отключить автопродление", "cancel_confirm", {"sid": item["subscription_id"]})],
                back="archived_agreements",
                message_id=mid,
            )
        if action == "subscription":
            sub = subscription_snapshot or await self.subscription(aid, data["sid"], refresh_panel=True)
            catalog = await self.call("catalog")
            product = next((p for p in catalog["products"] if p["id"] == sub["product_id"]), {})
            name, icon = self.subscription_title(sub, catalog)
            title, _ = button_content(name, icon)
            tier = product.get("tier_level")
            held = copy.retention_blocked(sub)
            buttons = [
                (copy.CONNECT, "connect", data),
                (
                    "Продлить",
                    "periods",
                    {"sid": sub["id"], "intent": "renew", "product_id": sub["product_id"]},
                ),
                ("Устройства", "devices", data),
                ("Добавить подписку", "products", {"intent": "purchase"}),
            ]
            if any(
                type(tier) is int
                and p.get("available")
                and type(p.get("tier_level")) is int
                and p["tier_level"] > tier
                and p.get("eligibility", {}).get("allow_upgrade", True)
                for p in catalog["products"]
            ):
                buttons.append(("Повысить тариф", "products", {"sid": sub["id"], "intent": "upgrade"}))
            if any(
                type(tier) is int
                and p.get("available")
                and type(p.get("tier_level")) is int
                and p["tier_level"] < tier
                and p.get("eligibility", {}).get("allow_downgrade", True)
                for p in catalog["products"]
            ):
                buttons.append(("Понизить тариф", "products", {"sid": sub["id"], "intent": "downgrade"}))
            recurring = sub.get("recurring")
            if held:
                buttons = [("Добавить подписку", "products", {"intent": "purchase"})]
            if recurring and recurring.get("state", recurring.get("status")) not in {
                "stopped",
                "cancelled",
                "canceled",
            }:
                buttons.append(("Отключить автопродление", "cancel_confirm", data))
            elif (
                not held
                and catalog.get("recurring_enrollment_enabled")
                and catalog.get("capabilities", {}).get("recurring")
            ):
                buttons.append(("Включить автопродление", "enroll_confirm", data))
            panel_limit = sub.get("panel_device_limit")
            device_limit = copy.current_device_limit(
                None
                if sub["devices_limit"] is None
                else panel_limit
                if type(panel_limit) is int and panel_limit >= 0
                else sub["devices_limit"]
            )
            body = f"Тариф: {product.get('name', 'Не указан')}\nСрок: {moscow_datetime(sub.get('expires_at'))}\nУстройств: {device_limit}\nСостояние: {status(sub['status'])}"
            if sub["name"] != title:
                body += f"\nНазвание подписки: {sub['name']}"
            if held:
                body = f"Тариф: {product.get('name', 'Не указан')}\nСостояние: {'Архив' if sub.get('retention_state') == 'archived' else 'На проверке'}"
            notice = copy.retention_notice(sub)
            if notice:
                body += "\n\n" + notice
            if not held and sub.get("connection_help_due") is True:
                body += "\n\n" + copy.CONNECTION_HELP
            engagement = copy.engagement_notice(sub)
            if engagement:
                body += "\n\n" + engagement
            if (
                not held
                and sub.get("manual_renewal_discount_eligible")
                and sub.get("manual_renewal_discount_percent")
            ):
                body += f"\n\nСкидка ручного продления перед окончанием срока: {sub['manual_renewal_discount_percent']}%. Итоговую скидку покажет расчёт заказа."
            body += (
                "\nТрафик: "
                + traffic(sub.get("traffic_bytes"))
                + "\nЛимит трафика: "
                + traffic(sub.get("traffic_limit_bytes"))
            )
            body += "\nДанные панели: " + (
                moscow_datetime(sub["observed_at"]) if sub.get("observed_at") else "пока недоступны"
            )
            if sub.get("panel_sync_state") in {"stale", "denied"}:
                body += "\nПоказаны сохранённые данные. Панель не подтверждена; новый расчёт пока недоступен."
            elif sub.get("panel_sync_state") == "pending":
                body += "\nИзменения подключения выполняются. Новый расчёт пока недоступен."
            if sub.get("panel_sync_state") == "fresh" and panel_limit is None:
                body += "\nЛимит панели не определён; указан сохранённый."
            if sub.get("panel_sync_state") != "unknown" and sub.get("panel_product_state") in {
                "unknown",
                "ambiguous",
            }:
                body += "\nТариф панели требует проверки владельцем."
            if recurring:
                body += (
                    f"\nАвтопродление: {status(recurring.get('state', recurring.get('status', 'unknown')))}"
                )
            nodes = card(title, body)
            nodes[0]["children"] = name
            return await self.show(
                uid,
                title,
                body,
                buttons,
                back="subscriptions",
                back_data={"show_list": True, **({"origin": data["origin"]} if data.get("origin") else {})},
                message_id=mid,
                nodes=nodes,
                header_key="subscription_heading",
                url_buttons=[("Поддержка по подписке", support, "help")]
                if (
                    support := support_draft_url(
                        self.instance.support_url,
                        kind="subscription",
                        identifier=sub["id"],
                        status=sub["status"],
                    )
                )
                else (),
            )
        if action == "connection_qr":
            from io import BytesIO

            import qrcode
            from sunrise_core.errors import DomainError

            sub = subscription_snapshot or await self.subscription(aid, data["sid"])
            url = safe_url(sub.get("connection_url"))
            catalog = await self.call("catalog")
            if (
                not url
                or len(url.encode()) > 2048
                or catalog.get("capabilities", {}).get("provisioning") == "local_test"
            ):
                raise DomainError("connection_unavailable", "Личная ссылка подключения пока недоступна.", 409)
            buffer = BytesIO()
            qrcode.make(url).save(buffer, format="PNG")
            await self.bot.send_photo(
                uid,
                BufferedInputFile(buffer.getvalue(), filename="vpn-qr.png"),
                caption="QR-код подключения.",
            )
            return
        if action in {"connect", "connection_platform", "connection_app"}:
            sub = subscription_snapshot or await self.subscription(aid, data["sid"])
            url = safe_url(sub.get("connection_url"))
            catalog = await self.call("catalog")
            local = catalog.get("capabilities", {}).get("provisioning") == "local_test"
            if local:
                url = None
            if url:
                from sunrise_bot.connection_guide import guide_screen

                guide = (
                    await self.call("connection_instructions", aid, data["sid"])
                    if action == "connect"
                    else data.get("guide", {})
                )
                screen = guide_screen(
                    guide,
                    data["sid"],
                    url,
                    platform=data.get("platform") if action != "connect" else None,
                    app=data.get("app") if action == "connection_app" else None,
                    page=data.get("page", 0),
                    instance=self.instance,
                )
                if screen:
                    screen["buttons"] = [
                        *screen.get("buttons", []),
                        ("QR-код подключения", "connection_qr", {"sid": sub["id"]}),
                    ]
                    return await self.show(
                        uid,
                        copy.CONNECT,
                        "",
                        message_id=mid,
                        header_key="connect_heading",
                        **screen,
                    )
            nodes = card(
                copy.CONNECT,
                f"Локальный тестовый стенд: внешнее {self.instance.terminology.connection_label} не выполняется. Ссылка подключения появится после настройки разрешённого тестового сервиса."
                if local
                else catalog.get("content", {})
                .get("help_center", {})
                .get(
                    "connection_instructions",
                    f"Откройте личную ссылку и импортируйте подписку в совместимое {self.instance.terminology.client_label}.",
                )
                if url
                else "Ссылка пока недоступна. Состояние выдачи доступа можно проверить в подписке.",
            )
            if url:
                nodes.append(
                    {
                        "type": "paragraph",
                        "children": [{"type": "text", "text": copy.copy_connection(self.instance)}],
                    }
                )
                nodes.append(
                    {
                        "type": "paragraph",
                        "children": [{"type": "code", "children": [{"type": "text", "text": url}]}],
                    }
                )
            return await self.show(
                uid,
                copy.CONNECT,
                "",
                back="subscription",
                back_data={"sid": data["sid"]},
                message_id=mid,
                nodes=nodes,
                buttons=[("QR-код подключения", "connection_qr", {"sid": sub["id"]})] if url else (),
                static_header=True,
                header_key="connect_heading",
                url_buttons=[("Открыть подписку", url, "connection_open")] if url else (),
                url_buttons_after=True,
            )
        if action == "products":
            from sunrise_core.telegram_content import telegram_html_to_content

            catalog = await self.call("catalog")
            current = (
                subscription_snapshot or await self.subscription(aid, data["sid"])
                if data.get("sid")
                else None
            )
            current_product = next(
                (p for p in catalog["products"] if current and p["id"] == current["product_id"]), {}
            )
            products = sorted(
                [p for p in catalog["products"] if p.get("available") and p.get("visible_bot", True)],
                key=lambda p: (p.get("sort_order", 0), p["id"]),
            )
            if data["intent"] in {"upgrade", "downgrade"}:
                greater = data["intent"] == "upgrade"
                current_tier = current_product.get("tier_level")
                products = [
                    p
                    for p in products
                    if type(current_tier) is int
                    and type(p.get("tier_level")) is int
                    and (p["tier_level"] > current_tier if greater else p["tier_level"] < current_tier)
                ]
            introduction = catalog.get("content", {}).get("catalog_description_content")
            if introduction:
                nodes = list(introduction)
            elif catalog.get("content", {}).get("catalog_description"):
                nodes = telegram_html_to_content(catalog["content"]["catalog_description"])
            else:
                nodes = card(
                    "Выберите тариф",
                    "" if products else "Нет доступных тарифов.",
                )
            buttons = []
            for product in products:
                tier_context = {}
                if data["intent"] in {"upgrade", "downgrade"}:
                    periods = product.get("periods") or catalog["periods"]
                    if not periods:
                        continue
                    # Общий контракт требует период; перерасчёт использует только оплаченный остаток.
                    tier_context = {"period_days": periods[0]["days"]}
                name = (product.get("content") or {}).get("name") or telegram_html_to_content(product["name"])
                label, icon = button_content(name, product.get("icon"))
                buttons.append(
                    (
                        label[:64],
                        "bonus_choice" if data["intent"] in {"upgrade", "downgrade"} else "periods",
                        {
                            **data,
                            **tier_context,
                            "product_id": product["id"],
                            "icon": icon,
                            "semantic": "primary" if product.get("highlighted") else "neutral",
                        },
                    )
                )
            return await self.show(
                uid,
                "Выберите тариф",
                "",
                buttons,
                back="subscription" if current else "main",
                back_data={"sid": current["id"]} if current else {},
                message_id=mid,
                nodes=nodes,
                static_header=not introduction and not catalog.get("content", {}).get("catalog_description"),
            )
        if action == "periods":
            from sunrise_core.telegram_content import telegram_html_to_content

            catalog = await self.call("catalog")
            product = next(
                (
                    p
                    for p in catalog["products"]
                    if p["id"] == data["product_id"] and p.get("available") and p.get("visible_bot", True)
                ),
                None,
            )
            if product is None:
                return await self.show(
                    uid,
                    "Тариф недоступен",
                    "Вернитесь в каталог и выберите доступный тариф.",
                    back="products",
                    back_data={k: v for k, v in data.items() if k != "product_id"},
                    message_id=mid,
                )
            period_notice = "Дополнительные скидки и бонусы применяются в расчёте. Итоговую сумму покажем перед подтверждением."
            nodes = card("Выберите период", period_notice)
            name = (product.get("content") or {}).get("name") or telegram_html_to_content(product["name"])
            nodes.append({"type": "paragraph", "children": name})
            description = (product.get("content") or {}).get("description")
            nodes.extend(description or telegram_html_to_content(product.get("description", "")))
            return await self.show(
                uid,
                "Выберите период",
                period_notice,
                [
                    (
                        self.period_label(p, product.get("currency")),
                        "bonus_choice",
                        {**data, "period_days": p["days"]},
                    )
                    for p in product.get("periods", catalog["periods"])
                ],
                back="subscription" if data.get("intent") == "renew" and data.get("sid") else "products",
                nodes=nodes,
                back_data={k: v for k, v in data.items() if k != "product_id"},
                message_id=mid,
                columns=2,
                icon_variants=False,
            )
        if action == "bonus_choice":
            data = {key: value for key, value in data.items() if key != "source_step_skipped"}
            dash = await self.call("dashboard", aid)
            has_bonus = Decimal(dash.get("bonus_balance", "0")) > 0
            has_credit = bool(dash.get("credit_currency")) and Decimal(dash.get("credit_available", "0")) > 0
            if not has_bonus and not has_credit:
                return await self.execute(
                    uid,
                    aid,
                    "quote",
                    {**data, "bonus_amount": "0.00", "source_step_skipped": True},
                    mid,
                    token,
                )
            sources = [
                (
                    "Без бонусов" if has_bonus else "Оплатить без остатка",
                    "quote",
                    {**data, "bonus_amount": "0.00"},
                )
            ]
            if has_bonus:
                sources.append(("Использовать допустимые бонусы", "bonus_max", data))
            credit_note = ""
            if has_credit:
                credit_note = (
                    f"\nДенежный остаток: {money(dash['credit_available'], dash['credit_currency'])}."
                )
                sources.append(("Использовать денежный остаток", "cash_max", data))
                if has_bonus:
                    sources.append(("Бонусы и денежный остаток", "bonus_cash_max", data))
            elif Decimal(dash.get("credit_balance", "0")) > 0 and not dash.get("credit_currency"):
                credit_note = "\nДенежный остаток требует проверки валюты владельцем перед использованием."
            return await self.show(
                uid,
                "Источники оплаты",
                (
                    f"Бонусы: {money(dash['bonus_balance'], 'RUB')}. Допустимую сумму проверит сервер."
                    if has_bonus
                    else "Выберите способ использования денежного остатка."
                )
                + credit_note
                + "\n\nАктивированный промокод учитывается сервером при расчёте подходящего заказа.",
                sources,
                back=selection_back,
                back_data=data,
                message_id=mid,
            )
        if action in {"quote", "bonus_max", "cash_max", "bonus_cash_max", "renewal_offer"}:
            methods = catalog.get("payment_methods")
            selected_method = next(
                (
                    method
                    for method in methods or []
                    if method["id"] == data.get("payment_method") and method["available"]
                ),
                None,
            )
            internal_cash_only = (
                methods is not None
                and not any(method["available"] for method in methods)
                and data.get("payment_method") is None
                and action in {"cash_max", "bonus_cash_max"}
            )
            if methods is not None and selected_method is None and not internal_cash_only:
                unavailable = "\n".join(
                    f"{copy.payment_method_label(method)}: {method.get('unavailable_reason') or 'Временно недоступен'}"
                    for method in methods
                    if not method["available"]
                )
                return await self.show(
                    uid,
                    "Способ оплаты",
                    "Выберите способ оплаты. Итоговую стоимость и скидку рассчитает сервер."
                    + ("\n\n" + unavailable if unavailable else "")
                    + ("\nСпособы оплаты временно недоступны." if not methods else ""),
                    [
                        (
                            copy.payment_method_label(method),
                            action,
                            {
                                **data,
                                "payment_method": method["id"],
                                "icon_key": "payment_" + method["id"],
                                "semantic": "neutral",
                            },
                        )
                        for method in methods
                        if method["available"]
                    ],
                    back=selection_back if data.get("source_step_skipped") else "bonus_choice",
                    back_data=data,
                    message_id=mid,
                )
            if action == "renewal_offer":
                quote = await self.call(
                    "renewal_offer",
                    aid,
                    data["sid"],
                    quote_id=data.get("quote_id"),
                    **({"payment_method": selected_method["id"]} if selected_method else {}),
                )
                data = {
                    **data,
                    "product_id": quote["product_id"],
                    "period_days": quote["period_days"],
                    "intent": "renew",
                }
            else:
                kwargs = {
                    "product_id": data["product_id"],
                    "period_days": data["period_days"],
                    "action": data["intent"],
                    "subscription_id": data.get("sid"),
                    "bonus_amount": data.get("bonus_amount", "0.00"),
                    "credit_amount": "0.00",
                    "extra_devices": data.get("extra_devices", 0),
                }
                if selected_method:
                    kwargs["payment_method"] = selected_method["id"]
                if action in {"cash_max", "bonus_cash_max"}:
                    dash = await self.call("dashboard", aid)
                    kwargs["credit_amount"] = dash.get("credit_available", "0.00")
                if action in {"bonus_max", "bonus_cash_max"}:
                    # Backend owns cap/eligibility. Never compute a redemption policy in Telegram.
                    if hasattr(self.commerce, "quote_with_max_bonus"):
                        quote = await self.call("quote_with_max_bonus", aid, **kwargs)
                    else:
                        return await self.show(
                            uid,
                            "Бонусы",
                            "Автоматический выбор суммы пока недоступен. Выберите заказ без бонусов или используйте кабинет.",
                            [("Без бонусов", "quote", {**data, "bonus_amount": "0.00"})],
                            back="bonus_choice",
                            back_data=data,
                            message_id=mid,
                        )
                else:
                    quote = await self.call("quote", aid, **kwargs)
            if internal_cash_only:
                try:
                    total = Decimal(quote["total"])
                    credit = Decimal(quote["credit_amount"])
                    internal_confirmed = (
                        total.is_finite()
                        and total == 0
                        and credit.is_finite()
                        and credit > 0
                        and quote.get("minimum_external_required") is False
                        and quote.get("payment_method") is None
                    )
                except (KeyError, TypeError, InvalidOperation):
                    internal_confirmed = False
                if not internal_confirmed:
                    return await self.show(
                        uid,
                        "Способ оплаты",
                        "Денежный остаток не покрывает заказ целиком. Для оплаты нужен доступный способ оплаты.",
                        message_id=mid,
                        back="bonus_choice",
                        back_data=data,
                    )
            target = (
                (await self.subscription(aid, data["sid"]))["name"]
                if data.get("sid")
                else "Новая отдельная подписка"
            )
            discount_sections = []
            period_text = (
                "Остаток оплаченного срока"
                if data.get("intent") in {"upgrade", "downgrade"}
                else f"{data['period_days']} дней"
            )
            promo_label = f"Промокод {quote.get('promo_code') or ''}".strip()
            selected_promo = quote.get("selected_secondary") or {}
            promo_percent = quote.get("promo_percent") or (
                selected_promo.get("percent") if selected_promo.get("source") == "promo" else None
            )
            if (
                quote.get("promo_reward_amount_rub") is None
                and promo_percent is not None
                and Decimal(str(promo_percent)) > 0
            ):
                promo_label += " (−" + percent(promo_percent) + ")"
            for field, label in (
                ("period_discount_amount", "Скидка за период"),
                ("secondary_discount_amount", "Другие скидки"),
                ("promo_discount_amount", promo_label),
            ):
                if field in quote and Decimal(str(quote[field])) > 0:
                    discount_sections.append(
                        self.section("quote_discount", label, money(quote[field], quote["currency"]))
                    )
            if not discount_sections:
                discount_sections = [
                    self.section(
                        "quote_discount", "Скидка", money(quote["discount_amount"], quote["currency"])
                    )
                ]
            body = (
                f"Подписка: {target}\nПериод: {period_text}\n"
                f"Стоимость: {money(quote['base_amount'], quote['currency'])}\nСкидка: {money(quote['discount_amount'], quote['currency'])}\n"
                f"Бонусы со счёта: {money(quote['bonus_amount'], quote['currency'])}\n"
                f"Денежный остаток: {money(quote.get('credit_amount', '0.00'), quote['currency'])}\nИтого: {money(quote['total'], quote['currency'])}\n"
                f"Устройств: {quote['devices_limit']}\nСрок после операции: {moscow_datetime(quote['resulting_expires_at'])}\nРасчёт действует до: {moscow_datetime(quote['expires_at'])}"
            )
            legal = await self.call("legal_status", aid)
            nodes = card("Проверьте заказ", "") + self.group_sections(
                [
                    self.section("quote_target", "Подписка", target),
                    self.section("quote_period", "Период", period_text),
                    self.section("quote_price", "Стоимость", money(quote["base_amount"], quote["currency"])),
                    *discount_sections,
                    self.section(
                        "quote_bonus", "Бонусы со счёта", money(quote["bonus_amount"], quote["currency"])
                    ),
                    self.section(
                        "quote_credit",
                        "Денежный остаток",
                        money(quote.get("credit_amount", "0.00"), quote["currency"]),
                    ),
                    self.section("quote_total", "Итого", money(quote["total"], quote["currency"])),
                    self.section("quote_devices", "Устройств", quote["devices_limit"]),
                    self.section(
                        "quote_expiry", "Срок после операции", moscow_datetime(quote["resulting_expires_at"])
                    ),
                    self.section(
                        "quote_validity", "Расчёт действует до", moscow_datetime(quote["expires_at"])
                    ),
                ]
            )
            selected_discount = copy.renewal_discount(quote)
            method = quote.get("payment_method")
            if method:
                method_text = (
                    f"Способ оплаты: {copy.payment_method_label(method)}\n"
                    f"Скидка за способ оплаты: {money(quote.get('payment_method_discount_amount', '0.00'), quote['currency'])}"
                )
                body += "\n\n" + method_text
                nodes.append({"type": "paragraph", "children": [{"type": "text", "text": method_text}]})
            if selected_discount:
                body += "\n\n" + selected_discount
                nodes.append({"type": "paragraph", "children": [{"type": "text", "text": selected_discount}]})
            device_pricing = quote.get("device_pricing") or {}
            if device_pricing.get("extra_devices"):
                device_text = (
                    f"Дополнительных устройств: {device_pricing['extra_devices']}\n"
                    f"Цена устройства: {money(device_pricing.get('unit_price'), quote['currency'])}\n"
                    f"Стоимость устройств: {money(device_pricing.get('amount'), quote['currency'])}\n"
                )
                device_text += (
                    f"Месяцев расчёта: {device_pricing.get('billing_months')}"
                    if device_pricing.get("mode") == "monthly_renewal"
                    else "Фиксированная оплата до конца оплаченного срока."
                )
                nodes.append({"type": "paragraph", "children": [{"type": "text", "text": device_text}]})
            nodes.append({"type": "paragraph", "children": [{"type": "text", "text": copy.CHECKOUT_LEGAL}]})
            nodes.extend(self.legal_links(legal))
            return await self.show(
                uid,
                "Проверьте заказ",
                body,
                [
                    (
                        "Создать заказ",
                        "checkout",
                        {
                            "quote_id": quote["id"],
                            "key": "bot:" + token_urlsafe(18),
                            "_checkout_generation": checkout_generation,
                        },
                    )
                ]
                + (
                    [
                        (
                            "Изменить способ оплаты",
                            action,
                            {k: v for k, v in data.items() if k != "payment_method"},
                        )
                    ]
                    if methods is not None
                    else []
                ),
                back=selection_back if data.get("source_step_skipped") else "bonus_choice",
                back_data=data,
                message_id=mid,
                nodes=nodes,
                static_header=True,
            )
        if action in {"checkout", "order"}:
            order = (
                await self.call("create_order", aid, data["quote_id"], data["key"], source="bot")
                if action == "checkout"
                else await self.call("order", aid, data["order_id"])
            )
            if action == "checkout":
                data["_created_order_id"] = order["id"]
            payment_note = None
            if (
                order["status"] == "awaiting_payment"
                and order.get("environment") in {"sandbox", "live", "scoped_live"}
                and not order.get("checkout_url")
                and self.payment_provider is not None
            ):
                from sunrise_core.errors import DomainError
                from sunrise_infrastructure.billing import Billing

                billing = Billing(self.commerce, self.payment_provider.terminal, order["environment"])
                try:
                    result = await asyncio.to_thread(
                        billing.prepare_payment, order["id"], self.payment_provider
                    )
                    if result["state"] in {"outcome_unknown", "review"}:
                        payment_note = (
                            "Платёж уточняется. Заказ сохранён; новый счёт не создаём. "
                            "При необходимости обратитесь в поддержку."
                        )
                except DomainError as failure:
                    payment_note = (
                        "Расчёт истёк. Создайте новый заказ по актуальной цене. Этот заказ сохранён."
                        if failure.code == "quote_expired"
                        else (
                            "Платёжный сервис временно недоступен. Заказ сохранён; новый счёт не создаём. "
                            "При необходимости обратитесь в поддержку."
                        )
                    )
                order = await self.call("order", aid, order["id"])
            paid_pending = order["status"] in {
                "paid_provisioning_pending",
                "provisioning_pending",
                "paid",
                "paid_pending",
                "paid_pending_provisioning",
            }
            body = (
                self.instance.terminology.provisioning_pending_body
                if paid_pending
                else f"Заказ: {order['id']}\nСостояние: {status(order['status'])}\nИтого: {money(order['total'], order['currency'])}"
            )
            if order["status"] == "paid":
                if order.get("provisioning_status") == "ready" and order.get("subscription_id"):
                    body = (
                        f"Оплата получена. Тестовая выдача завершена; внешнее {self.instance.terminology.connection_label} не выполняется."
                        if order.get("environment") == "local_test"
                        else self.instance.terminology.provisioning_ready_body
                    )
                elif order.get("provisioning_status") != "pending":
                    body = "Оплата получена. Подключение требует проверки. Повторно оплачивать не нужно; обратитесь в поддержку."
            nodes = card("Ваш заказ", body)
            nodes.extend(self.legal_links(await self.call("legal_status", aid)))
            url = safe_url(order.get("checkout_url"))
            if url:
                if order.get("payment_expires_at"):
                    nodes.append(
                        self.section(
                            "quote_validity", "Оплатить до", moscow_datetime(order["payment_expires_at"])
                        )
                    )
                nodes.append(
                    {
                        "type": "paragraph",
                        "children": [
                            {
                                "type": "text",
                                "text": "После оплаты статус обновится автоматически. Мы сообщим, когда доступ будет готов.",
                            }
                        ],
                    }
                )
            elif payment_note:
                nodes.append({"type": "paragraph", "children": [{"type": "text", "text": payment_note}]})
            elif (
                order.get("environment") in {"sandbox", "live", "scoped_live"}
                and order["status"] == "awaiting_payment"
            ):
                nodes.append(
                    {
                        "type": "paragraph",
                        "children": [
                            {
                                "type": "text",
                                "text": "Ссылка на оплату пока недоступна. Заказ сохранён. Готовую ссылку пришлём отдельным сообщением.",
                            }
                        ],
                    }
                )
            elif order.get("environment") == "local_test" and order["status"] == "awaiting_payment":
                nodes.append(
                    {
                        "type": "paragraph",
                        "children": [
                            {
                                "type": "text",
                                "text": "Это локальный тестовый заказ. Внешняя оплата недоступна; реальное списание не выполняется.",
                            }
                        ],
                    }
                )
            return await self.show(
                uid,
                "Ваш заказ",
                "",
                [("Мои подписки", "subscriptions", {}), ("История платежей", "history", {})],
                message_id=mid,
                nodes=nodes,
                static_header=True,
                url_buttons=([("Оплатить", url, "checkout")] if url else [])
                + (
                    [("Поддержка по заказу", support, "help")]
                    if (
                        support := support_draft_url(
                            self.instance.support_url,
                            kind="order",
                            identifier=order["id"],
                            status=order["status"],
                        )
                    )
                    else []
                ),
            )
        if action == "devices":
            return await self.devices_screen(uid, aid, data, mid)
        if action == "revoke_confirm":
            await self.device_selection(aid, data)
            return await self.show(
                uid,
                "Отключить устройства?",
                f"Выбрано устройств: {len(data['selection'])}. После вашего подтверждения отвязка выполняется автоматически; одобрение владельца не требуется. Каждая отвязка учитывается отдельно. Оплаченный срок сохраняется.",
                [
                    (
                        "Подтвердить отключение",
                        "revoke",
                        {**data, "key": token_urlsafe(18), "icon_key": "revoke"},
                    )
                ],
                back="devices",
                back_data=data,
                message_id=mid,
            )
        if action == "rotate_confirm":
            sub = await self.subscription(aid, data["sid"])
            inventory = await self.customer_call("inventory", aid, data["sid"])
            if not self.observed_inventory(inventory) or sub.get("credential_rotation_state") in {
                "pending",
                "in_flight",
                "review",
            }:
                from sunrise_core.errors import DomainError

                raise DomainError(
                    "panel_unavailable",
                    "Обновление ссылки сейчас недоступно. Обновите список устройств.",
                    409,
                )
            return await self.show(
                uid,
                "Перевыпустить ссылку?",
                "После подтверждённого перевыпуска старая ссылка перестанет работать. Потребуется заново импортировать новую ссылку во все приложения. Оплаченный срок, тариф и лимит устройств сохраняются.",
                [
                    (
                        "Подтвердить перевыпуск",
                        "rotate",
                        {**data, "key": token_urlsafe(18), "icon_key": "link_confirm"},
                    )
                ],
                back="devices",
                back_data=data,
                message_id=mid,
            )
        if action in {"revoke", "rotate"}:
            from sunrise_core.errors import DomainError

            await self.subscription(aid, data["sid"])
            if not data.get("key"):
                raise DomainError("confirmation_required", "Выберите действие и подтвердите его заново.", 400)
            if action == "revoke":
                if not data.get("submitted"):
                    await self.device_selection(aid, data)
                result = await self.customer_call(
                    "revoke_devices",
                    aid,
                    data["sid"],
                    [did for did, _ in data["selection"]],
                    data["key"],
                    confirmed=True,
                    expected_generations=dict(data["selection"]),
                    operation_mode="bulk" if data.get("bulk") else "single",
                )
            else:
                result = await self.customer_call(
                    "rotate_credentials", aid, data["sid"], data["key"], confirmed=True
                )
            data["submitted"] = True
            state = result.get("status")
            body = (
                "Результат панели требует проверки. Повторный запрос не отправляйте."
                if state in {"review", "review_required", "blocked"}
                else "Отключение подтверждено панелью."
                if state == "revoked"
                else "Запрос сохранён. Ожидается подтверждение панели; действие ещё не завершено."
            )
            return await self.show(
                uid,
                "Устройства" if action == "revoke" else "Обновление ссылки",
                body,
                [("Обновить статус", "devices", {**data, "refresh": True, "icon_key": "order"})],
                back="profile" if data.get("origin") == "profile" else "subscription",
                back_data={"sid": data["sid"]},
                message_id=mid,
            )
        if action == "device_quote":
            sub = subscription_snapshot or await self.subscription(aid, data["sid"])
            catalog = await self.call("catalog")
            if not catalog["periods"]:
                return await self.show(
                    uid, "Устройства", "Расчёт временно недоступен: нет настроенных периодов.", message_id=mid
                )
            return await self.execute(
                uid,
                aid,
                "bonus_choice",
                {
                    **data,
                    "product_id": sub["product_id"],
                    "intent": "devices",
                    "extra_devices": 1,
                    "period_days": catalog["periods"][0]["days"],
                },
                mid,
                token,
                subscription_snapshot=sub,
            )
        if action in {"cancel_confirm", "enroll_confirm"}:
            archived = False
            if action == "cancel_confirm":
                subscription = await self.agreement_subscription(aid, data["sid"])
                archived = "subscription_id" in subscription
            else:
                await self.subscription(aid, data["sid"])
            target = {
                "cancel_confirm": ("Отключить автопродление?", "Подтвердить отмену", "cancel"),
                "enroll_confirm": ("Подключить автопродление?", "Подтвердить подключение", "enroll"),
            }[action]
            return await self.show(
                uid,
                target[0],
                "Оплаченный доступ сохраняется. Действие относится только к выбранной подписке.",
                [(target[1], target[2], data)],
                back="archived_agreement" if archived else "subscription",
                back_data={"sid": data["sid"]},
                message_id=mid,
            )
        if action == "cancel":
            subscription = await self.agreement_subscription(aid, data["sid"])
            archived = "subscription_id" in subscription
            result = await self.call("cancel_agreement", aid, data["sid"])
            state = result.get("state", result.get("status")) if isinstance(result, dict) else None
            return await self.show(
                uid,
                "Автопродление",
                "Отмена подтверждена."
                if state in {"stopped", "cancelled", "canceled"}
                else "Запрос отмены сохранён. Подтверждение провайдера ещё ожидается; проверяйте состояние в подписке.",
                [("Подписки", "subscriptions", {})]
                if archived
                else [("Открыть подписку", "subscription", {"sid": data["sid"]})],
                message_id=mid,
            )
        if action == "enroll":
            catalog = await self.call("catalog")
            if not catalog.get("recurring_enrollment_enabled") or not catalog.get("capabilities", {}).get(
                "recurring"
            ):
                return await self.show(uid, "Автопродление", copy.CAPABILITY, message_id=mid)
            await self.call("enroll", aid, data["sid"])
            return await self.show(
                uid,
                "Автопродление",
                "Подключение требует подтверждения согласия у платёжного провайдера. Проверьте состояние в подписке.",
                message_id=mid,
            )
        if action in {"bonuses", "history", "profile"}:
            dash = await self.call(
                "dashboard", aid, **({"refresh_panel": True} if action == "profile" else {})
            )
            if action == "bonuses":
                body = f"Бонусный баланс: {money(dash['bonus_balance'], 'RUB')}"
                reserved = Decimal(dash.get("bonus_reserved", "0"))
                if reserved > 0:
                    body += "\nЗарезервировано в заказах: " + money(reserved, "RUB")
                referral = dash.get("referral")
                if referral:
                    body += "\nОт рефералов начислено всего: " + money(
                        referral.get("rewarded_amount"), referral.get("currency", "RUB")
                    )
                active = dash.get("active_promo")
                if active:
                    kind = active.get("reward_type", "percent")
                    reward = (
                        money(active.get("amount_rub"), "RUB") + " скидки"
                        if kind == "fixed_rub"
                        else "(−" + percent(active.get("percent")) + ")"
                        if kind == "percent"
                        else promo_reward(active)
                    )
                    body += f"\nПромокод {active['code']}: {reward}"
                    if not active.get("available", True):
                        body += "\n" + (active.get("unavailable_reason") or "Промокод сейчас недоступен.")
                body += "\n\n" + copy.BONUS_GUIDE
                return await self.show(
                    uid,
                    "Бонусы",
                    body,
                    [("Реферальная система", "referral", {})],
                    message_id=mid,
                )
            if action == "history":
                payments = dash["payments"]
                page = max(0, min(int(data.get("page", 0)), len(payments) - 1))
                buttons = []
                support_buttons = []
                body = "Платежей пока нет."
                if payments:
                    p = payments[page]
                    operation = {
                        "purchase": "Покупка",
                        "renew": "Продление",
                        "devices": "Добавление устройств",
                        "upgrade": "Повышение тарифа",
                        "downgrade": "Понижение тарифа",
                    }.get(p.get("action"), "Операция уточняется")
                    fee = (
                        money(p.get("fee"), p["fee_currency"])
                        if p.get("fee") is not None and p.get("fee_currency")
                        else "Валюта комиссии неизвестна"
                        if p.get("fee") is not None
                        else "Комиссия неизвестна"
                    )
                    body = f"Платёж {page + 1} из {len(payments)}\n{str(p.get('product_name') or 'Тариф уточняется')[:300]}\n{operation}\n{money(p['amount'], p['currency'])} · {status(p['status'])}\n{moscow_datetime(p.get('paid_at'))}\nКомиссия: {fee}"
                    if p.get("period_days") is not None:
                        body += f"\nПериод: {p['period_days']} дней"
                    if p.get("devices") is not None:
                        body += f"\nУстройств: {p['devices']}"
                    if p.get("payment_method"):
                        method = {"bank_card": "Банковская карта", "sbp": "СБП"}.get(
                            p["payment_method"], str(p["payment_method"])[:80]
                        )
                        body += "\nСпособ оплаты: " + method
                    if p.get("provisioning_status"):
                        body += "\nДоступ: " + status(p["provisioning_status"])
                    if page:
                        buttons.append(
                            ("Предыдущий", "history", {"page": page - 1, "icon_key": "admin_prev"})
                        )
                    if page + 1 < len(payments):
                        buttons.append(("Далее", "history", {"page": page + 1, "icon_key": "admin_next"}))
                    if support := support_draft_url(
                        self.instance.support_url,
                        kind="order",
                        identifier=p.get("order_id"),
                        status=p.get("status"),
                    ):
                        support_buttons.append(("Поддержка по заказу", support, "help"))
                return await self.show(
                    uid, "История платежей", body, buttons, message_id=mid, url_buttons=support_buttons
                )
            account = dash["account"]
            dashboard = safe_url(getattr(getattr(self.accounts, "settings", None), "dashboard_url", None))
            return await self.show(
                uid,
                "Профиль",
                "",
                [
                    ("Реферальная система", "referral", {}),
                    ("Устройства", "subscriptions", {"next_action": "devices", "origin": "profile"}),
                ]
                + ([("Связать аккаунты", "link", {})] if not account.get("email_verified") else []),
                url_buttons=[
                    ("Email, Telegram и активные сеансы", dashboard.split("#", 1)[0] + "#account", "profile")
                ]
                if dashboard
                else (),
                message_id=mid,
                nodes=card("Профиль", "")
                + self.group_sections(
                    [
                        self.section("profile_id", "Аккаунт", account["id"]),
                        self.section("profile_email", "Email", account.get("email") or "Не привязан"),
                        self.section("profile_bonus", "Бонусный баланс", money(dash["bonus_balance"], "RUB")),
                    ]
                ),
                static_header=True,
            )
        if action == "referral":
            referral = await self.call("referral_info", aid)
            nodes = card("Реферальная система", copy.REFERRAL_GUIDE)
            nodes.extend(
                [
                    self.section(
                        "referral_reward", "Вознаграждение", percent(referral.get("reward_percent"))
                    ),
                    self.section("referral_count", "Приглашено", referral.get("invited_count", 0)),
                    self.section(
                        "profile_bonus",
                        "Начислено",
                        money(referral.get("rewarded_amount"), referral.get("currency", "RUB")),
                    ),
                ]
            )
            url = None
            code = referral.get("referral_code", "")
            if referral.get("enrollment_available") and re.fullmatch(r"ref_[1-9][0-9]{0,18}", code):
                try:
                    identity = await self.bot.get_me()
                    username = identity.username or ""
                    if str(identity.id) in self.safety.bot_ids and re.fullmatch(
                        r"[A-Za-z0-9_]{5,32}", username
                    ):
                        url = f"https://t.me/{username}?start={code}"
                except TelegramAPIError:
                    pass
            if url:
                nodes.append(
                    {
                        "type": "paragraph",
                        "children": [
                            header_emoji(self.theme, self.emoji_registry, "referral_link"),
                            {"type": "text", "text": " Личная ссылка приглашения\n"},
                            {"type": "link", "href": url, "children": [{"type": "text", "text": url}]},
                        ],
                    }
                )
            else:
                nodes.append(
                    {"type": "paragraph", "children": [{"type": "text", "text": copy.REFERRAL_UNAVAILABLE}]}
                )
            return await self.show(
                uid,
                "Реферальная система",
                "",
                back="profile",
                message_id=mid,
                nodes=nodes,
                static_header=True,
                copy_buttons=[("Скопировать ссылку", url)] if url else (),
            )
        if action == "link":
            return await self.show(
                uid,
                "Связывание аккаунтов",
                copy.LINK + "\n\nОтправьте: /link полученное-подтверждение"
                if self.accounts
                else copy.LINK_BLOCKED,
                message_id=mid,
            )
        if action == "link_confirm" and self.accounts:
            await asyncio.to_thread(self.accounts.confirm_telegram_link, aid, data["proof"])
            # The backend invalidates the exact pair proof. Drop this local copy after success.
            self.actions.pop(token, None)
            return await self.show(
                uid,
                "Аккаунты связаны",
                "Профиль обновлён. Подписки доступны из обоих интерфейсов.",
                message_id=mid,
            )
        return await self.show(uid, "Меню обновлено", copy.STALE, message_id=mid)

    async def faq(self, uid, aid, action, mid):
        catalog = await self.call("catalog")
        content = catalog.get("content", {}).get("help_center", {})
        legal = catalog.get("legal") or await self.call("legal_status", aid)
        icons = {
            "purchase": "purchase",
            "recurring": "enroll_confirm",
            "one_click": "history",
            "promos": "promo",
            "referrals": "referral",
        }
        practical_icons = {
            "help_payment_access": ("faq_payment_access", "order", "order_heading"),
            "help_vpn_connection": ("faq_vpn_connection", "connect", "connect_heading"),
            "help_device_limit": ("faq_device_limit", "devices", "devices_heading"),
            "help_email_code": ("faq_email_code", "profile_email", "profile_heading"),
            "help_cancel_recurring": ("faq_cancel_recurring", "cancel_confirm", "recurring_heading"),
            "help_refund_request": ("faq_refund_request", "quote_credit", "history_heading"),
        }

        def practical_icon(topic_id, *, heading=False):
            selected = practical_icons.get(topic_id)
            if not selected:
                return "help_heading" if heading else "help"
            key, button_fallback, heading_fallback = selected
            if heading:
                key += "_heading"
            if key in self.theme.get("icons", {}) or heading and key in self.emoji_registry:
                return key
            return heading_fallback if heading else button_fallback

        topics = {
            "faq_" + item["id"]: (item["title"], item["body"])
            for item in content.get("faq", [])
            if item.get("id") in icons
        }
        published = {
            "faq_" + item["id"]: item
            for item in content.get("custom_faq", [])
            if isinstance(item, dict)
            and isinstance(item.get("id"), str)
            and isinstance(item.get("question"), str)
            and isinstance(item.get("answer"), list)
        }
        topic_nodes = None
        header_key = "help_heading"
        urls = []
        buttons = []
        if action == "help":
            labels = {
                "offer": ("Публичная оферта", "offer"),
                "privacy": ("Политика конфиденциальности", "privacy"),
            }
            docs = {d.get("document_type"): d for d in legal.get("documents", [])}
            for kind in ("offer", "privacy"):
                url = safe_url(docs.get(kind, {}).get("url"))
                if url:
                    urls.append((*labels[kind][:1], url, labels[kind][1]))
            if url := safe_url(content.get("support_url")):
                urls.append(("Поддержка", url, "help"))
            buttons = [
                (item["title"], "faq_" + item["id"], {"icon_key": icons[item["id"]]})
                for item in content.get("faq", [])
                if item.get("id") in icons
            ]
            buttons.extend(
                (item["question"], key, {"icon_key": practical_icon(item["id"])})
                for key, item in published.items()
            )
            title, body = "FAQ", "Документы, поддержка и ответы на частые вопросы. Выберите тему."
        elif action in published:
            item = published[action]
            title, body = item["question"], ""
            header_key = practical_icon(item["id"], heading=True)
            topic_nodes = [*card(title, ""), *item["answer"]]
            target = {
                "help_payment_access": ("Платежи", "history", {}),
                "help_vpn_connection": ("Мои подписки", "subscriptions", {}),
                "help_device_limit": ("Открыть устройства", "subscriptions", {"next_action": "devices"}),
                "help_email_code": ("Профиль", "profile", {}),
                "help_cancel_recurring": ("Мои подписки", "subscriptions", {}),
                "help_refund_request": ("Платежи", "history", {}),
            }.get(item["id"])
            if target:
                buttons.append(target)
            if url := safe_url(content.get("support_url")):
                urls.append(("Поддержка", url, "help"))
        elif action in topics:
            title, body = topics[action]
            if action == "faq_purchase" and (url := safe_url(content.get("dashboard_url"))):
                urls.append(("Открыть кабинет", url, "profile"))
            if action == "faq_promos":
                urls.extend(
                    (item["title"], url, "help" if index == 0 else "history")
                    for index, item in enumerate(content.get("promo_channels", []))
                    if (url := safe_url(item.get("url")))
                )
                buttons.append(("Активировать промокод", "promo", {}))
            if action == "faq_referrals":
                buttons.append(("Мои приглашения", "referral", {}))
        else:
            return await self.show(uid, "FAQ", copy.STALE, back="help", message_id=mid)
        return await self.show(
            uid,
            title,
            body,
            buttons,
            back="main" if action == "help" else "help",
            message_id=mid,
            url_buttons=urls,
            header_key=header_key,
            nodes=topic_nodes,
            static_header=bool(topic_nodes),
        )

    @staticmethod
    def legal_links(legal):
        labels = {"offer": "Публичная оферта", "privacy": "Политика конфиденциальности"}
        return [
            {
                "type": "paragraph",
                "children": [
                    {
                        "type": "link",
                        "href": doc["url"],
                        "children": [{"type": "text", "text": labels[doc["document_type"]]}],
                    }
                ],
            }
            for doc in legal.get("documents", [])
            if doc.get("document_type") in labels and safe_url(doc.get("url"))
        ]


def traffic(value):
    if value is None or isinstance(value, bool) or not str(value).isascii() or not str(value).isdigit():
        return "недоступен"
    number = Decimal(str(value))
    if number > 2**63 - 1:
        return "недоступен"
    for unit in ("Б", "КиБ", "МиБ", "ГиБ", "ТиБ", "ПиБ", "ЭиБ"):
        if number < 1024 or unit == "ЭиБ":
            return (
                (
                    format(number.quantize(Decimal(".1")), "f").replace(".", ",")
                    if unit != "Б"
                    else str(int(number))
                )
                + " "
                + unit
            )
        number /= 1024
    return "недоступен"


def percent(value):
    if value is None:
        return "Не настроено"
    try:
        return format(Decimal(str(value)).normalize(), "f").replace(".", ",") + "%"
    except InvalidOperation:
        return "Не настроено"


def promo_reward(promo):
    kind = promo.get("reward_type", "percent")
    if kind == "fixed_rub":
        return "Скидка " + money(promo.get("amount_rub"), "RUB")
    if kind == "free_days":
        return f"Бесплатный доступ: {promo.get('reward_days', '—')} дней"
    return "Скидка " + percent(promo.get("percent"))


def money(value, currency):
    if value is None:
        return "Неизвестно"
    try:
        return f"{Decimal(str(value)):,.2f}".replace(",", " ").replace(".", ",") + (
            " ₽" if currency == "RUB" else f" {currency}"
        )
    except InvalidOperation:
        return "Неизвестно"


def status(value):
    return {
        "active": "Действует",
        "pending": "Ожидает оплаты",
        "paid": "Оплачено",
        "awaiting_payment": "Ожидает оплаты",
        "enabled": "Действует",
        "stop_pending": "Ожидается подтверждение отмены",
        "expired": "Срок истёк",
        "provisioning_pending": "Доступ подключается",
        "provisioned": "Доступ выдан",
        "completed": "Выполнено",
        "failed": "Не удалось выполнить",
        "cancel_pending": "Ожидается подтверждение отмены",
        "cancellation_pending": "Ожидается подтверждение отмены",
        "stopped": "Отключено",
        "cancelled": "Отменено",
        "canceled": "Отменено",
    }.get(value, "Уточняется")
