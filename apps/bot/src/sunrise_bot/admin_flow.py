"""Russian Telegram administration over the shared, authorized console facade."""

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from secrets import token_urlsafe
from time import monotonic
from uuid import UUID
from zoneinfo import ZoneInfo

from sunrise_core.content import validate_content
from sunrise_core.errors import DomainError

from sunrise_bot import copy
from sunrise_bot.content import card, render_plain, safe_url
from sunrise_bot.theme import header_emoji


@dataclass
class AdminDraft:
    owner: str
    kind: str
    fields: list = field(default_factory=list)
    values: dict = field(default_factory=dict)
    context: dict = field(default_factory=dict)
    step: str = "fields"
    reason: str = ""
    identifier: str = field(default_factory=lambda: token_urlsafe(16))
    revision: int = 0
    expires: float = field(default_factory=lambda: monotonic() + 900)


def telegram_content(message):
    """Map UTF-16 entity ranges to the existing safe semantic AST, preserving emoji."""
    text = message.text or getattr(message, "caption", None) or ""
    if not text.strip() or len(text) > 3500:
        raise ValueError("Введите текст сообщения: от 1 до 3500 символов.")
    raw = text.encode("utf-16-le")
    entities = []
    for entity in (message.entities if message.text else getattr(message, "caption_entities", None)) or []:
        kind = str(entity.type)
        if kind.startswith("MessageEntityType."):
            kind = kind.split(".")[-1].lower()
        if kind in {"bold", "italic", "underline", "code", "pre", "text_link", "custom_emoji", "blockquote"}:
            begin, end = entity.offset * 2, (entity.offset + entity.length) * 2
            if not 0 <= begin < end <= len(raw):
                raise ValueError("Не удалось прочитать форматирование сообщения.")
            entities.append((begin, end, kind, entity))

    def nodes(begin, end, remaining, depth=0):
        if depth > 7:
            raise ValueError("Слишком сложное форматирование.")
        result, position = [], begin
        selected = sorted(
            (item for item in remaining if begin <= item[0] and item[1] <= end),
            key=lambda item: (item[0], -item[1]),
        )
        for start, stop, kind, entity in selected:
            if start < position:
                continue
            if start > position:
                result.append({"type": "text", "text": raw[position:start].decode("utf-16-le")})
            own = (start, stop, kind, entity)
            children = nodes(
                start, stop, [item for item in selected if item is not own and item != own], depth + 1
            )
            if kind == "custom_emoji":
                result.append(
                    {
                        "type": "emoji",
                        "id": str(entity.custom_emoji_id),
                        "fallback": raw[start:stop].decode("utf-16-le"),
                        "label": "Эмодзи",
                    }
                )
            elif kind == "text_link":
                if not safe_url(entity.url) or not entity.url.startswith("https://"):
                    raise ValueError("В сообщении разрешены только HTTPS-ссылки.")
                result.append({"type": "link", "href": entity.url, "children": children})
            else:
                result.append(
                    {"type": {"pre": "code", "blockquote": "quote"}.get(kind, kind), "children": children}
                )
            position = stop
        if position < end:
            result.append({"type": "text", "text": raw[position:end].decode("utf-16-le")})
        return result

    return validate_content([{"type": "paragraph", "children": nodes(0, len(raw), entities)}])


def money(value, currency="RUB"):
    if value is None:
        return "неизвестно"
    try:
        number = Decimal(str(value))
        return f"{number:,.2f}".replace(",", " ").replace(".", ",") + " " + currency
    except InvalidOperation:
        return "неизвестно"


class AdminFlow:
    def __init__(self, app):
        self.app = app
        self.drafts = {}
        self.capabilities_by_uid = {}

    @property
    def console(self):
        return getattr(self.app, "admin_console", None) or getattr(self.app.admin, "bot_console", None)

    def cancel(self, uid):
        self.drafts.pop(uid, None)

    @staticmethod
    def media_kwargs(draft):
        return {"media": draft.values["media"]} if draft.values.get("media") else {}

    def media_authorizer(self, uid, aid, draft, *, step, reference=None):
        expected = (draft.owner, draft.identifier, draft.revision, draft.kind, draft.step, draft.expires)
        expected_media = dict(reference) if reference is not None else None

        def current():
            return (
                self.drafts.get(uid) is draft
                and draft.owner == str(aid)
                and draft.kind == "broadcast"
                and draft.step == step
                and draft.expires > monotonic()
                and (draft.owner, draft.identifier, draft.revision, draft.kind, draft.step, draft.expires)
                == expected
                and (expected_media is None or draft.values.get("media") == expected_media)
            )

        async def authorize():
            if not current() or not await self.authorized(uid, aid):
                return False
            return current() and "broadcasts_write" in self.capabilities_by_uid.get(uid, set())

        return authorize

    async def upload_media(self, uid, aid, message, draft):
        if (
            draft.kind != "broadcast"
            or draft.step != "fields"
            or not draft.fields
            or draft.fields[0][0] != "nodes"
        ):
            raise ValueError("Прикрепите файл при вводе текста новой рассылки.")
        # Сначала валидируем подпись: не скачиваем файл, который нельзя сохранить в рассылке.
        nodes = telegram_content(message)
        bot = getattr(self.app, "bot", None)
        guard = getattr(getattr(bot, "session", None), "media", None)
        if guard is None:
            raise ValueError("Загрузка медиа для этого бота недоступна.")
        authorize = self.media_authorizer(uid, aid, draft, step="fields")

        try:
            async with guard.authorized_upload_scope(
                bot,
                message,
                owner_id=str(aid),
                draft=draft,
                authorize=authorize,
            ) as scope:
                file = await bot.get_file(scope.file_id)
                content = await guard.download_approved_file(bot, file)
                mime = scope.mime
            if not await authorize():
                raise PermissionError()
            reference = await self.call("register_media", aid, [content], mime)
            if not await authorize():
                raise PermissionError()
        except PermissionError:
            raise ValueError("Файл не связан с текущим разрешённым черновиком рассылки.") from None
        draft.values.update(nodes=nodes, media=reference)
        draft.fields.pop(0)
        await self.prompt(uid, draft)

    async def test_media(self, uid, aid, draft):
        bot = getattr(self.app, "bot", None)
        guard = getattr(getattr(bot, "session", None), "media", None)
        if guard is None:
            raise ValueError("Предпросмотр медиа для этого бота недоступен.")
        reference = draft.values["media"]
        authorize = self.media_authorizer(uid, aid, draft, step="review", reference=reference)

        async def resolve(ref):
            if not await authorize():
                raise PermissionError("Черновик рассылки недоступен.")
            result = await self.call("resolve_media", aid, ref)
            if not await authorize():
                raise PermissionError("Черновик рассылки недоступен.")
            return result

        async with guard.registered_asset_send_scope(
            bot, uid, reference, resolve=resolve, authorize=authorize
        ) as local:
            if reference["mime"] == "video/mp4":
                await bot.send_video(uid, local)
            else:
                await bot.send_photo(uid, local)

    async def call(self, method, aid, *args, **kwargs):
        return await asyncio.to_thread(getattr(self.console, method), aid, *args, **kwargs)

    async def authorized(self, uid, aid):
        if self.console is None:
            return False
        account = await self.app.call("account_info", aid)
        if (
            str(account.get("id")) != str(aid)
            or str(account.get("telegram_id")) != str(uid)
            or not self.app.admin_allowed(account)
        ):
            return False
        allowed = bool(await self.call("authorize_admin", aid))
        if allowed:
            self.capabilities_by_uid[uid] = set((await self.call("capabilities", aid))["capabilities"])
        return allowed

    def can_moderate(self, uid):
        return "device_unlinks_manage" in self.capabilities_by_uid.get(uid, set())

    @staticmethod
    def action_capability(action):
        if action.startswith("admin_backup"):
            return "backups_manage"
        if action == "admin_user_message":
            return "messages_write"
        if action == "admin_user_gift" or action == "admin_gift_product":
            return "subscriptions_manage"
        if action.startswith("admin_product"):
            return "catalog_write"
        if action == "admin_catalog_intro":
            return "content_write"
        if action.startswith("admin_setting"):
            return "settings_write"
        if action == "admin_user_moderate":
            return "device_unlinks_manage"
        if action.startswith("admin_user"):
            return "users_read"
        if action.startswith("admin_broadcast"):
            return "broadcasts_write"
        if action.startswith("admin_utm"):
            return "campaigns_read"
        if action.startswith("admin_promo"):
            return "promos_write"
        if action == "admin_referrals":
            return "referrals_read"
        if action == "admin_stats" or action == "admin_period" or action.startswith("admin_calendar"):
            return "statistics_read"
        return None

    async def show(self, uid, title, body, buttons=(), mid=None, *, nodes=None, back="admin"):
        draft = self.drafts.get(uid)
        if draft and back == "admin":
            back = {
                "product": "admin_products",
                "intro": "admin_products",
                "setting": "admin_settings",
                "broadcast": "admin_broadcasts",
                "broadcast_start": "admin_broadcasts",
                "broadcast_stop": "admin_broadcasts",
                "utm": "admin_utm",
                "backup": "admin_backups",
                "message": "admin_users",
                "gift": "admin_users",
                "moderation": "admin_users",
                "calendar": "admin_stats",
            }.get(draft.kind, back)
        if draft and buttons:
            draft.revision += 1
            buttons = [
                (
                    label,
                    action,
                    {
                        **data,
                        "draft_id": draft.identifier,
                        "draft_revision": draft.revision,
                        "draft_owner": draft.owner,
                    },
                )
                for label, action, data in buttons
            ]
        await self.app.show(
            uid,
            title,
            body,
            buttons,
            back=back,
            message_id=mid,
            compact=True,
            header_key="admin_heading",
            nodes=nodes,
        )

    @staticmethod
    def button(label, action, **data):
        return label, action, {"icon_key": action, **data}

    @staticmethod
    def pages(result, action, **data):
        page, total, size = result.get("page", 1), result.get("total", 0), result.get("page_size", 10)
        buttons = []
        if page > 1:
            buttons.append(("Предыдущая", action, {**data, "page": page - 1, "icon_key": "admin_prev"}))
        if page * size < total:
            buttons.append(("Следующая", action, {**data, "page": page + 1, "icon_key": "admin_next"}))
        return buttons

    async def draft_start(self, uid, aid, kind, fields, context=None, values=None, mid=None):
        self.cancel(uid)
        if hasattr(self.app, "admin_promos"):
            self.app.admin_promos.cancel(uid)
        draft = AdminDraft(str(aid), kind, fields, values or {}, context or {})
        self.drafts[uid] = draft
        await self.prompt(uid, draft, mid)

    async def prompt(self, uid, draft, mid=None):
        if draft.fields:
            await self.show(uid, "Изменение настроек", draft.fields[0][1], mid=mid)
        else:
            if draft.kind == "moderation":
                draft.reason = f"Подтверждение действия {draft.context['action']} в карточке пользователя {draft.context['target_id']} через Telegram"
                if draft.context.get("notice_event_id"):
                    draft.reason += "; событие отвязки " + str(draft.context["notice_event_id"])
                return await self.review(uid, draft, mid)
            if draft.kind == "intro" or (
                draft.kind == "product" and set(draft.values) <= {"name", "description", "content"}
            ):
                draft.reason = (
                    "Редактирование описания каталога через Telegram"
                    if draft.kind == "intro"
                    else "Редактирование текста товара через Telegram"
                )
                return await self.review(uid, draft, mid)
            draft.reason = copy.ADMIN_AUDIT_REASON
            return await self.review(uid, draft, mid)

    async def review(self, uid, draft, mid=None):
        draft.step = "review"
        labels = {
            "name": "Название",
            "description": "Описание",
            "price": "Цена",
            "code": "Код",
            "tier_level": "Уровень",
            "internal_squad_uuid": "UUID внутренней группы",
            "project_prefix": "Префикс",
            "external_squad_uuid": "UUID внешней группы",
            "enabled": "Новые подключения автопродления",
            "intro": "Описание каталога",
            "source": "Источник",
            "medium": "Тип размещения",
            "campaign": "Кампания",
            "percent": "Оценка комиссии, %",
            "fixed": "Фиксированная часть, ₽",
            "days": "Дней в подарок",
            "target_id": "Аккаунт получателя",
            "offer_url": "Публичная оферта",
            "offer_version": "Версия оферты",
            "privacy_url": "Политика конфиденциальности",
            "privacy_version": "Версия политики",
        }
        body = "\n".join(
            f"{labels.get(key, key)}: {render_plain(value) if isinstance(value, list) else ('включены' if value else 'выключены') if isinstance(value, bool) else value}"
            for key, value in draft.values.items()
            if key not in {"nodes", "content", "media"}
        )
        buttons = [self.button("Сохранить", "admin_save", semantic="success")]
        nodes = None
        if draft.kind == "broadcast":
            preview = await self.call(
                "bot_broadcast_preview", draft.owner, draft.values["nodes"], **self.media_kwargs(draft)
            )
            body = f"Получателей рассылки: {preview['recipient_count']}"
            if media := draft.values.get("media"):
                body += "\nВложение: " + ("видео MP4" if media["mime"] == "video/mp4" else "фото")
            nodes = card("Предпросмотр рассылки", body) + draft.values["nodes"]
            if getattr(getattr(self.console, "settings", None), "local_test", True):
                buttons.insert(0, self.button("Тест себе", "admin_broadcast_test"))
        elif draft.kind == "message":
            nodes = card("Предпросмотр сообщения", body) + draft.values["nodes"]
        await self.show(uid, "Проверьте изменение", body.strip(), buttons, mid, nodes=nodes)

    async def message(self, uid, account, message):
        draft = self.drafts.get(uid)
        if not draft:
            return False
        text = (message.text or "").strip()
        if draft.owner != str(account["id"]) or draft.expires < monotonic() or text.startswith("/"):
            self.cancel(uid)
            return False
        if not await self.authorized(uid, account["id"]):
            self.cancel(uid)
            await self.show(uid, "Страница не найдена", "404")
            return True
        try:
            if getattr(message, "photo", None) or getattr(message, "video", None):
                await self.upload_media(uid, account["id"], message, draft)
            elif draft.kind == "users":
                self.cancel(uid)
                await self.users(uid, account["id"], text)
            elif draft.step == "fields" and draft.fields:
                key, _, kind = draft.fields[0]
                value = self.parse_value(text, kind)
                if kind == "nodes":
                    value = telegram_content(message)
                draft.values[key] = value
                if key in {"name", "description"} and draft.kind == "product":
                    draft.values.setdefault("content", dict(draft.context.get("content", {})))[key] = (
                        telegram_content(message)
                    )
                draft.fields.pop(0)
                await self.prompt(uid, draft)
            else:
                await self.review(uid, draft)
        except (ValueError, InvalidOperation) as exc:
            await self.show(uid, "Проверьте данные", str(exc) or "Проверьте введённое значение.")
        except DomainError as exc:
            await self.show(uid, "Проверьте данные", exc.message)
        return True

    @staticmethod
    def parse_value(text, kind):
        if kind == "days":
            if not text.isascii() or not text.isdigit() or not 1 <= int(text) <= 3660:
                raise ValueError("Введите число дней: от 1 до 3660.")
            return int(text)
        if kind in {"price", "fixed"}:
            try:
                value = Decimal(text.replace(",", "."))
                if (
                    not value.is_finite()
                    or value < 0
                    or kind == "price"
                    and value == 0
                    or value != value.quantize(Decimal("0.01"))
                ):
                    raise ValueError()
                return format(value.quantize(Decimal("0.01")), "f")
            except (InvalidOperation, ValueError):
                raise ValueError("Введите положительную цену с точностью до копейки.") from None
        if kind == "percent":
            value = Decimal(text.replace(",", "."))
            if not value.is_finite() or not 0 <= value <= 100:
                raise ValueError("Процент должен быть от 0 до 100.")
            return format(value, "f")
        if kind == "uuid":
            try:
                return str(UUID(text)) if text != "—" else None
            except ValueError:
                raise ValueError("Введите корректный UUID или «—», чтобы убрать привязку.") from None
        if kind == "url":
            if not safe_url(text) or not text.startswith("https://"):
                raise ValueError("Введите HTTPS-ссылку без логина и пароля.")
        if kind == "level":
            if not text.isascii() or not text.isdigit() or not 0 <= int(text) <= 100:
                raise ValueError("Введите уровень тарифа от 0 до 100.")
            return int(text)
        if kind == "code" and not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", text):
            raise ValueError("Код: латинские строчные буквы, цифры, дефис; начните с буквы.")
        if kind == "prefix" and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{1,23}", text):
            raise ValueError("Префикс: 2–24 латинских символа, цифры, подчёркивание.")
        limit = 200 if kind == "name" else 3500
        if not text or len(text) > limit:
            raise ValueError(f"Введите текст: от 1 до {limit} символов.")
        return text

    async def save(self, uid, aid, draft, mid):
        if draft.step != "review":
            return await self.show(uid, "Изменение настроек", "Сначала заполните поля.", mid=mid)
        if draft.kind == "product":
            await self.call(
                "bot_product_save",
                aid,
                draft.context.get("product_id"),
                draft.values,
                draft.reason,
                confirmed=True,
            )
        elif draft.kind == "message":
            await self.call(
                "bot_message",
                aid,
                draft.context["target_id"],
                draft.values["nodes"],
                "bot-message:" + draft.identifier,
                draft.reason,
                confirmed=True,
            )
            self.cancel(uid)
            return await self.show(
                uid,
                "Сообщение сохранено",
                "Сообщение поставлено в общую очередь уведомлений пользователя.",
                mid=mid,
            )
        elif draft.kind == "gift":
            await self.call(
                "bot_grant",
                aid,
                draft.context["target_id"],
                draft.context["product_id"],
                draft.values["days"],
                "bot-gift:" + draft.identifier,
                draft.reason,
                confirmed=True,
            )
            self.cancel(uid)
            return await self.show(
                uid,
                "Подарок сохранён",
                "Дни сохранены. Доступ подключается через общую очередь; оплаченные подписки сохранены.",
                mid=mid,
            )
        elif draft.kind == "backup":
            await self.call(
                "bot_request_backup", aid, "bot-backup:" + draft.identifier, draft.reason, confirmed=True
            )
            self.cancel(uid)
            return await self.show(
                uid,
                "Копия запрошена",
                "Задача сохранена. Копия разрешённой тестовой базы будет доставлена только владельцу.",
                mid=mid,
            )
        elif draft.kind == "intro":
            await self.call(
                "bot_catalog_intro_save", aid, draft.values["intro"], draft.reason, confirmed=True
            )
        elif draft.kind == "utm":
            await self.call("bot_utm_create", aid, draft.values, draft.reason, confirmed=True)
        elif draft.kind == "setting":
            section = draft.context["section"]
            values = draft.context.get("before", {}) | draft.values
            if section == "legal":
                docs = {}
                for kind in ("offer", "privacy"):
                    if kind + "_url" in draft.values:
                        docs[kind] = {
                            "document_type": kind,
                            "url": draft.values[kind + "_url"],
                            "version": draft.values[kind + "_version"],
                        }
                values = {"documents": [docs[kind] for kind in ("offer", "privacy") if kind in docs]}
                if len(docs) == 1:
                    values["partial"] = True
            elif section == "fee_estimates":
                values = draft.context.get("before", {}) | {
                    draft.context["method"]: draft.values | {"currency": "RUB"}
                }
            await self.call(
                "bot_settings_update", aid, draft.context["section"], values, draft.reason, confirmed=True
            )
        elif draft.kind == "moderation":
            if not self.can_moderate(uid):
                self.cancel(uid)
                return await self.show(uid, "Страница не найдена", "404", mid=mid)
            result = await self.call(
                "bot_user_moderate",
                aid,
                draft.context["target_id"],
                draft.context["action"],
                draft.reason,
                "bot-moderation:" + draft.identifier,
                confirmed=True,
            )
            self.cancel(uid)
            state = result or {}
            return await self.show(
                uid,
                "Ограничение сохранено",
                f"Предупреждения: {state.get('current_warnings', 0)}/2 · всего: {state.get('total_warnings', 0)}\nОтвязка устройств: {'заблокирована' if state.get('unlink_blocked') else 'разрешена'}\nОплаченный доступ, покупки и продления доступны. Причина записана в журнал.",
                [
                    self.button(
                        "Карточка пользователя",
                        "admin_user",
                        target_id=draft.context["target_id"],
                        icon_key="profile",
                    )
                ],
                mid,
            )
        elif draft.kind == "broadcast":
            result = await self.call(
                "bot_broadcast_create",
                aid,
                draft.values["nodes"],
                draft.reason,
                confirmed=True,
                **self.media_kwargs(draft),
            )
            self.cancel(uid)
            return await self.broadcast_detail(uid, aid, result["id"], mid)
        elif draft.kind in {"broadcast_start", "broadcast_stop"}:
            await self.call("bot_" + draft.kind, aid, draft.context["job_id"], draft.reason, confirmed=True)
            self.cancel(uid)
            return await self.broadcast_detail(uid, aid, draft.context["job_id"], mid)
        self.cancel(uid)
        await self.show(
            uid,
            "Изменение сохранено",
            "Сохранено в общей базе. Изменения записаны в журнал.",
            mid=mid,
        )

    async def statistics(self, uid, aid, data, mid):
        result = await self.call(
            "bot_statistics", aid, start_date=data.get("start_date"), end_date=data.get("end_date")
        )
        lines = []
        for heading, key in [
            ("Сегодня", "today_money"),
            ("Выбранный период", "period_money"),
            ("За всё время", "all_time_money"),
        ]:
            lines.append(heading)
            for row in result.get(key, []):
                currency = row["currency"]
                lines += [
                    "Поступления: " + money(row.get("gross"), currency),
                    "После эквайринга (известные комиссии): " + money(row.get("actual_net"), currency),
                    "Комиссия неизвестна: " + str(row.get("unknown_fee_count", 0)),
                ]
                if row.get("estimated_count"):
                    lines.append("После эквайринга, оценка: " + money(row.get("estimated_net"), currency))
            if not result.get(key):
                lines.append("Нет платежей")
            lines.append("")
        lines += [
            f"Покупок: {result.get('purchase_count', 0)}",
            f"Telegram-пользователей: {result.get('telegram_users', 0)}",
            f"Пользователей с автопродлением: {result.get('recurring_users', 0)}",
            "",
            "Способы оплаты",
        ]
        methods = {
            "card": "Карта",
            "bank_card": "Карта",
            "sbp": "СБП",
            "crypto": "Криптовалюта",
            "unknown": "Не определён",
        }
        for row in result.get("payment_methods", []):
            lines.append(
                f"{methods.get(row.get('method'), row.get('method', 'Не определён'))}: {row.get('count', 0)} · {row.get('percent', 0)}%"
            )
        lines += [f"Не определён: {result.get('unknown_method_count', 0)}", "", "Популярные тарифы"]
        for row in result.get("popular_products", [])[:10]:
            lines.append(
                f"{row.get('name', row.get('product_id', 'Тариф'))}: {row.get('count', 0)} · {row.get('percent', 0)}%"
            )
        period = result.get("period", {})
        today = datetime.now(ZoneInfo("Europe/Moscow")).date().isoformat()
        first = period.get("start_date") or data.get("start_date") or today
        last = period.get("end_date") or data.get("end_date") or today
        dates = [datetime.strptime(value, "%Y-%m-%d").strftime("%d.%m.%Y") for value in (first, last)]
        lines.insert(0, f"{dates[0]} — {dates[1]} · МСК\n")
        await self.show(
            uid,
            "Статистика продаж",
            "\n".join(lines),
            [
                self.button("Сегодня", "admin_stats", period="today", icon_key="admin_today"),
                self.button("Указать свой период", "admin_period"),
                self.button("Всё время", "admin_stats", period="all", icon_key="admin_all"),
                self.button("Топ рефералов", "admin_referrals"),
            ],
            mid,
        )

    async def users(self, uid, aid, query, page=1, mid=None):
        result = await self.call("bot_users_search", aid, query, page=page, limit=8)
        buttons = [
            self.button(
                str(item.get("email") or item.get("telegram_id") or item["id"]),
                "admin_user",
                target_id=str(item["id"]),
                icon_key="subscription",
            )
            for item in result["items"]
        ]
        buttons += self.pages(result, "admin_users_results", query=query)
        await self.show(uid, "Пользователи", f"Найдено: {result['total']}\nЗапрос: {query}", buttons, mid)

    async def broadcast_detail(self, uid, aid, identifier, mid=None):
        result = await self.call("bot_broadcast_detail", aid, identifier)
        durable = result.get("delivery") == "durable_notifications"
        states = {
            "draft": "Черновик",
            "pending": "В очереди доставки" if durable else "В очереди (локальная проверка)",
            "running": "Обработка очереди" if durable else "Локальная обработка",
            "done": "Уведомления поставлены в очередь" if durable else "Локальная проверка завершена",
            "stopped": "Остановлена",
            "blocked": "Заблокирована",
        }
        body = f"Статус: {states.get(result.get('state'), 'Неизвестен')}\n"
        for key, label in [
            ("recipient_count", "Получателей"),
            ("delivered_count", "Доставлено" if durable else "Локально обработано"),
            ("failed_count", "Ошибок"),
        ]:
            body += f"{label}: {result.get(key, 0)}\n"
        body += (
            "Доставка через общую очередь уведомлений.\n"
            if durable
            else "Реальная отправка не выполнялась.\n"
        )
        buttons = [self.button("Обновить результаты", "admin_broadcast", job_id=identifier)]
        if result.get("state") == "draft":
            buttons.append(
                self.button("Начать рассылку", "admin_broadcast_start", job_id=identifier, semantic="primary")
            )
        if result.get("state") in {"pending", "running"}:
            buttons.append(
                self.button("Остановить", "admin_broadcast_stop", job_id=identifier, semantic="danger")
            )
        await self.show(uid, "Рассылка", body, buttons, mid, back="admin_broadcasts")

    async def execute(self, uid, aid, action, data, mid):
        if not await self.authorized(uid, aid):
            self.cancel(uid)
            return await self.show(uid, "Страница не найдена", "404", mid=mid)
        draft = self.drafts.get(uid)
        capability = self.action_capability(action)
        if capability and capability not in self.capabilities_by_uid.get(uid, set()):
            self.cancel(uid)
            return await self.show(
                uid, "Действие недоступно", "Это действие доступно только владельцу.", mid=mid
            )
        guarded = action in {
            "admin_save",
            "admin_broadcast_test",
            "admin_calendar_update",
            "admin_calendar_apply",
        }
        if guarded:
            if (
                not draft
                or draft.expires < monotonic()
                or draft.owner != str(aid)
                or data.get("draft_id") != draft.identifier
                or data.get("draft_revision") != draft.revision
                or data.get("draft_owner") != draft.owner
            ):
                return await self.app.show(
                    uid,
                    "Меню обновлено",
                    "Кнопка устарела. Используйте актуальные кнопки.",
                    back="admin",
                    message_id=mid,
                )
        else:
            self.cancel(uid)
        if action == "admin_save":
            return await self.save(uid, aid, draft, mid)
        if action == "admin":
            return await self.show(
                uid,
                "Админ-панель",
                copy.admin_brand(self.app.instance),
                [
                    self.button(label, target)
                    for label, target in [
                        ("Статистика", "admin_stats"),
                        ("Топ рефералов", "admin_referrals"),
                        ("Товары", "admin_products"),
                        ("Рассылки", "admin_broadcasts"),
                        ("Промокоды", "admin_promos"),
                        ("Пользователи", "admin_users"),
                        ("UTM-ссылки", "admin_utm"),
                        ("Настройки", "admin_settings"),
                        ("Резервные копии", "admin_backups"),
                    ]
                    if self.action_capability(target) in self.capabilities_by_uid.get(uid, set())
                ],
                mid,
                back="main",
            )
        if action == "admin_stats":
            if data.get("period") == "today":
                today = datetime.now(ZoneInfo("Europe/Moscow")).date().isoformat()
                data = {"start_date": today, "end_date": today}
            elif data.get("period") == "all":
                today = datetime.now(ZoneInfo("Europe/Moscow")).date().isoformat()
                data = {"start_date": "1900-01-01", "end_date": today}
            return await self.statistics(uid, aid, data, mid)
        if action == "admin_period" or action.startswith("admin_calendar_"):
            return await self.calendar(uid, aid, action, data, mid)
        if action == "admin_referrals":
            metric = data.get("metric", "rewards")
            result = await self.call("bot_referrals", aid, metric=metric, page=data.get("page", 1), limit=20)
            lines = [
                "Начисленные вознаграждения за вычетом отмен."
                if metric == "rewards"
                else "По числу приглашённых пользователей."
            ]
            for index, item in enumerate(result["items"], start=1 + (result.get("page", 1) - 1) * 20):
                name = item.get("telegram_id") or item.get("email") or item["account_id"]
                lines.append(
                    f"{index}. {str(name)[:80]} · {money(item.get('rewards'))} · {item.get('invited_users', 0)} чел."
                )
            buttons = [
                self.button(
                    "По вознаграждениям", "admin_referrals", metric="rewards", icon_key="referral_reward"
                ),
                self.button(
                    "По приглашённым", "admin_referrals", metric="invited", icon_key="referral_count"
                ),
            ]
            return await self.show(
                uid,
                "Топ-20 рефералов",
                "\n".join(lines),
                buttons + self.pages(result, action, metric=metric),
                mid,
            )
        if action == "admin_products":
            result = await self.call("bot_products", aid)
            page = max(1, int(data.get("page", 1)))
            buttons = [
                self.button("Добавить товар", "admin_product_add"),
                self.button("Описание каталога", "admin_catalog_intro"),
            ]
            buttons += [
                self.button(item["name"], "admin_product", product_id=item["id"], icon_key="product_select")
                for item in result["items"][(page - 1) * 8 : page * 8]
            ]
            buttons += self.pages({"page": page, "total": result["total"], "page_size": 8}, "admin_products")
            return await self.show(
                uid,
                "Товары",
                f"Товаров: {result['total']}\n" + (result.get("catalog_description") or ""),
                buttons,
                mid,
            )
        if action in {"admin_product", "admin_product_edit"}:
            result = await self.call("bot_products", aid)
            product = next((item for item in result["items"] if item["id"] == data.get("product_id")), None)
            if not product:
                return await self.show(uid, "Товар", "Товар не найден.", mid=mid)
            fields = {
                "name": ("Введите название товара с нужными эмодзи.", "name"),
                "description": ("Введите описание товара. Форматирование и эмодзи сохраняются.", "text"),
                "price": ("Введите базовую цену в рублях.", "price"),
                "internal_squad_uuid": (
                    "Введите UUID внутренней группы Remnawave или «—» для снятия привязки.",
                    "uuid",
                ),
            }
            if action == "admin_product_edit" and data.get("field") in fields:
                key = data["field"]
                return await self.draft_start(
                    uid,
                    aid,
                    "product",
                    [(key, *fields[key])],
                    {"product_id": product["id"], "content": product.get("content", {})},
                    mid=mid,
                )
            buttons = [
                self.button(
                    label,
                    "admin_product_edit",
                    product_id=product["id"],
                    field=key,
                    icon_key="admin_edit_" + ("uuid" if key == "internal_squad_uuid" else key),
                )
                for key, label in [
                    ("name", "Название"),
                    ("description", "Описание"),
                    ("price", "Цена"),
                    ("internal_squad_uuid", "UUID внутренней группы"),
                ]
            ]
            return await self.show(
                uid,
                product["name"],
                f"{product['description']}\n\nЦена: {money(product['price'], product['currency'])}\n"
                f"Внутренняя группа: {product.get('internal_squad_uuid') or 'не задана'}",
                buttons,
                mid,
                back="admin_products",
            )
        if action == "admin_product_add":
            return await self.draft_start(
                uid,
                aid,
                "product",
                [
                    ("code", "Введите постоянный внутренний код товара.", "code"),
                    ("name", "Введите название товара.", "name"),
                    ("description", "Введите описание товара.", "text"),
                    ("tier_level", "Введите уровень тарифа: 0–100. Чем выше, тем шире возможности.", "level"),
                    ("price", "Введите базовую цену в рублях.", "price"),
                    ("internal_squad_uuid", "Введите UUID внутренней группы Remnawave или «—».", "uuid"),
                ],
                mid=mid,
            )
        if action == "admin_catalog_intro":
            return await self.draft_start(
                uid,
                aid,
                "intro",
                [
                    (
                        "intro",
                        "Введите общее описание каталога для бота и сайта. Форматирование и эмодзи сохраняются.",
                        "nodes",
                    )
                ],
                mid=mid,
            )
        if action == "admin_users":
            return await self.draft_start(
                uid,
                aid,
                "users",
                [("query", "Введите внутренний ID, Telegram ID, @username или email пользователя.", "text")],
                mid=mid,
            )
        if action == "admin_users_results":
            return await self.users(uid, aid, data["query"], data.get("page", 1), mid)
        if action == "admin_user":
            result = await self.call(
                "bot_user_detail", aid, data["target_id"], page=data.get("page", 1), limit=5
            )
            return await self.user_detail(uid, result, data["target_id"], mid, data.get("section"))
        if action == "admin_user_message":
            return await self.draft_start(
                uid,
                aid,
                "message",
                [("nodes", "Введите сообщение пользователю с нужным форматированием.", "nodes")],
                {"target_id": data["target_id"]},
                {"target_id": data["target_id"]},
                mid,
            )
        if action == "admin_user_gift":
            catalog = await self.app.call("catalog")
            return await self.show(
                uid,
                "Подарить доступ",
                "Выберите тариф новой подарочной подписки. Действующие оплаченные подписки сохраняются.",
                [
                    self.button(
                        product["name"],
                        "admin_gift_product",
                        target_id=data["target_id"],
                        product_id=product["id"],
                    )
                    for product in catalog["products"]
                    if product.get("available")
                ],
                mid,
            )
        if action == "admin_gift_product":
            return await self.draft_start(
                uid,
                aid,
                "gift",
                [("days", "Сколько дней добавить в подарок? От 1 до 3660.", "days")],
                {"target_id": data["target_id"], "product_id": data["product_id"]},
                {"target_id": data["target_id"]},
                mid,
            )
        if action == "admin_backups":
            result = await self.call("bot_backups", aid)
            body = (
                "Ежедневно в "
                + result["schedule"]
                + " МСК. Получатель: только владелец.\n"
                + (
                    "\n".join("Копия " + item["id"][:8] + ": " + item["state"] for item in result["items"])
                    or "Задач пока нет."
                )
            )
            return await self.show(
                uid,
                "Резервные копии",
                body,
                [self.button("Запросить копию", "admin_backup_request", semantic="danger")],
                mid,
            )
        if action == "admin_backup_request":
            return await self.draft_start(uid, aid, "backup", [], mid=mid)
        if action == "admin_user_invitees":
            result = await self.call(
                "bot_user_invitees", aid, data["target_id"], page=data.get("page", 1), limit=10
            )
            lines = ["Приглашено: " + str(result.get("total", 0))]
            buttons = []
            for item in result.get("items", []):
                identity = (
                    "@" + item["telegram_username"]
                    if item.get("telegram_username")
                    else item.get("email") or item.get("telegram_id") or item["id"]
                )
                lines.append(f"{identity} · ID {item['id']}")
                buttons.append(
                    self.button(
                        str(identity)[:80], "admin_user", target_id=str(item["id"]), icon_key="profile"
                    )
                )
            return await self.show(
                uid,
                "Приглашённые пользователи",
                "\n".join(lines),
                buttons + self.pages(result, action, target_id=data["target_id"]),
                mid,
                back="admin_users",
            )
        if action == "admin_user_moderate":
            if not self.can_moderate(uid) or data.get("moderation_action") not in {
                "warn",
                "block",
                "unblock",
                "remove_warning",
            }:
                return await self.show(uid, "Страница не найдена", "404", mid=mid)
            detail = await self.call("bot_user_detail", aid, data["target_id"])
            if not detail.get("can_moderate"):
                return await self.show(uid, "Страница не найдена", "404", mid=mid)
            label = {
                "warn": "Предупредить пользователя",
                "block": "Заблокировать отвязку устройств",
                "unblock": "Разблокировать отвязку устройств",
                "remove_warning": "Снять одно предупреждение",
            }[data["moderation_action"]]
            return await self.draft_start(
                uid,
                aid,
                "moderation",
                [],
                {
                    "target_id": str(data["target_id"]),
                    "action": data["moderation_action"],
                    **({"notice_event_id": data["notice_event_id"]} if data.get("notice_event_id") else {}),
                },
                {"action": label, "target_id": str(data["target_id"])},
                mid,
            )
        if action == "admin_broadcasts":
            result = await self.call("bot_broadcast_list", aid, page=data.get("page", 1), limit=8)
            buttons = [self.button("Создать рассылку", "admin_broadcast_compose")]
            buttons += [
                self.button(
                    f"Рассылка {item['id'][:8]}",
                    "admin_broadcast",
                    job_id=item["id"],
                    icon_key="subscription",
                )
                for item in result["items"]
            ]
            return await self.show(
                uid,
                "Рассылки",
                "Аудитория определяется автоматически перед отправкой.",
                buttons + self.pages(result, action),
                mid,
            )
        if action == "admin_broadcast_compose":
            guard = getattr(getattr(getattr(self.app, "bot", None), "session", None), "media", None)
            photo_limit = f"{getattr(guard, 'photo_bytes', 2_000_000):,}".replace(",", " ")
            video_limit = f"{getattr(guard, 'video_bytes', 8_000_000):,}".replace(",", " ")
            return await self.draft_start(
                uid,
                aid,
                "broadcast",
                [
                    (
                        "nodes",
                        "Отправьте текст рассылки с форматированием и premium-эмодзи либо фото/видео MP4 с подписью. "
                        f"Фото: до {photo_limit} байт, видео: до {video_limit} байт. Один файл на рассылку.",
                        "nodes",
                    )
                ],
                mid=mid,
            )
        if action == "admin_broadcast_test" and draft.kind == "broadcast" and draft.step == "review":
            await self.call("bot_broadcast_preview", aid, draft.values["nodes"], **self.media_kwargs(draft))
            if draft.values.get("media"):
                await self.test_media(uid, aid, draft)
            await self.app.show(
                uid,
                "Тест рассылки",
                "",
                nodes=draft.values["nodes"],
                back="admin_broadcasts",
                message_id=None,
                header_key="admin_heading",
            )
            return await self.review(uid, draft, mid)
        if action == "admin_broadcast":
            return await self.broadcast_detail(uid, aid, data["job_id"], mid)
        if action in {"admin_broadcast_start", "admin_broadcast_stop"}:
            return await self.draft_start(
                uid, aid, action.removeprefix("admin_"), [], {"job_id": data["job_id"]}, mid=mid
            )
        if action.startswith("admin_utm"):
            return await self.utm(uid, aid, action, data, mid)
        if action.startswith("admin_setting"):
            return await self.settings(uid, aid, action, data, mid)
        return await self.show(
            uid, "Админ-панель", "Раздел недоступен. Вернитесь к списку разделов.", mid=mid
        )

    async def calendar(self, uid, aid, action, data, mid):
        from sunrise_bot.admin_calendar import CalendarState, render_calendar, transition

        if action == "admin_period":
            draft = AdminDraft(str(aid), "calendar", context={"calendar": CalendarState.current()})
            self.drafts[uid] = draft
        else:
            draft = self.drafts[uid]
            if draft.kind != "calendar":
                return
            if action == "admin_calendar_apply":
                state = draft.context["calendar"]
                if not state.start or not state.end:
                    return
                self.cancel(uid)
                return await self.statistics(
                    uid, aid, {"start_date": state.start.isoformat(), "end_date": state.end.isoformat()}, mid
                )
            draft.context["calendar"] = transition(
                draft.context["calendar"], data.get("operation"), data.get("value")
            )
        view = render_calendar(draft.context["calendar"])
        draft.revision += 1
        rows = []
        for row in view.rows:
            rows.append(
                [
                    self.app.button(
                        uid,
                        label,
                        target,
                        **values,
                        draft_id=draft.identifier,
                        draft_revision=draft.revision,
                        draft_owner=draft.owner,
                    )
                    for label, target, values in row
                ]
            )
        rows.append(
            [
                self.app.button(uid, "Назад", "admin_stats", icon_key="back"),
                self.app.button(uid, "Главное меню", "main"),
            ]
        )
        emoji = header_emoji(self.app.theme, self.app.emoji_registry, "period_heading")
        await self.app.presenter.show(uid, card("Укажите период", view.body, emoji), rows, mid)

    async def user_detail(self, uid, result, target, mid, selected=None):
        account = result["account"]
        lines = [
            f"Внутренний ID: {account.get('id')}",
            f"Telegram ID: {account.get('telegram_id') or 'не привязан'}",
            f"Email: {account.get('email') or 'не указан'}",
            f"Бонусы: {money(account.get('bonus_balance'))}",
            f"Денежные средства: {money(account.get('credit_available'), account.get('credit_currency') or 'RUB')}",
            f"Зарезервировано: {money(account.get('credit_reserved'), account.get('credit_currency') or 'RUB')}",
            f"Email подтверждён: {'да' if account.get('email_verified') else 'нет'}",
        ]
        if account.get("telegram_username"):
            lines.insert(2, "Telegram: @" + account["telegram_username"])
        if "active_subscriptions_total" in result:
            lines.append("Активные подписки: " + str(result["active_subscriptions_total"]))
        if "inviter" in result:
            inviter = result["inviter"]
            identity = (
                (
                    "@" + inviter["telegram_username"]
                    if inviter.get("telegram_username")
                    else inviter.get("telegram_id") or inviter.get("email") or inviter.get("id")
                )
                if inviter
                else "нет"
            )
            lines.append("Пригласил: " + str(identity))
        if referral := result.get("referral"):
            lines += [
                "Реферальный баланс: " + money(referral.get("balance")),
                "Всего заработано: " + money(referral.get("earned_total")),
                "Приглашено: " + str(referral.get("invited_total", 0)),
            ]
        if moderation := result.get("moderation"):
            lines += [
                f"Отвязки за {moderation.get('month', 'текущий месяц')} · МСК: {moderation.get('unlink_month', 0)}",
                f"Отвязки за всё время: {moderation.get('unlink_total', 0)}",
                f"По одному · за месяц: {moderation.get('unlink_individual_month', 0)} · всего: {moderation.get('unlink_individual_total', 0)}",
                f"Все устройства · за месяц: {moderation.get('unlink_bulk_month', 0)} · всего: {moderation.get('unlink_bulk_total', 0)}",
                f"Предупреждения: {moderation.get('current_warnings', 0)}/2 · всего: {moderation.get('total_warnings', 0)}",
                "Отвязка устройств: "
                + ("заблокирована" if moderation.get("unlink_blocked") else "разрешена"),
            ]
            if moderation.get("unlink_unknown_total") or moderation.get("unlink_unknown_month"):
                lines.append(
                    f"Без сохранённого способа · за месяц: {moderation.get('unlink_unknown_month', 0)} · всего: {moderation.get('unlink_unknown_total', 0)}"
                )
        aliases = result.get("identities", {}).get("aliases", [])
        if aliases:
            lines.append("Прежние ID: " + ", ".join(str(item) for item in aliases[:20]))
        sections = [
            ("subscriptions", "Подписки"),
            ("payments", "Платежи"),
            ("ledger", "Бонусные операции"),
            ("devices", "Устройства"),
            ("agreements", "Автопродления"),
            ("orders", "Заказы"),
            ("sessions", "Сеансы"),
            ("blocks", "Ограничения"),
            ("credit_ledger", "Денежные операции"),
            ("aliases", "Объединённые аккаунты"),
            ("audit", "Журнал действий"),
        ]
        for key, title in sections:
            if key not in result or selected and key != selected:
                continue
            section = result.get(key, {})
            if not selected:
                total = section.get("total", 0) if isinstance(section, dict) else len(section)
                lines.append(f"{title}: {total}")
                continue
            items = section.get("items", []) if isinstance(section, dict) else section
            lines.append("\n" + title)
            for item in items[:5]:
                allowed = {
                    "id",
                    "name",
                    "status",
                    "state",
                    "amount",
                    "currency",
                    "expiry",
                    "created_at",
                    "kind",
                    "expires_at",
                    "operation",
                    "devices_limit",
                    "next_charge_at",
                    "revoked_at",
                    "ends_at",
                    "count",
                    "threshold",
                    "rule",
                    "active",
                    "old_id",
                    "actor_id",
                    "action",
                    "target",
                    "reason",
                }
                labels = {
                    "id": "ID",
                    "name": "Название",
                    "status": "Статус",
                    "state": "Состояние",
                    "amount": "Сумма",
                    "currency": "Валюта",
                    "expiry": "До",
                    "created_at": "Дата",
                    "kind": "Операция",
                    "expires_at": "До",
                    "operation": "Операция",
                    "devices_limit": "Лимит устройств",
                    "next_charge_at": "Следующее списание",
                    "revoked_at": "Отозван",
                    "ends_at": "До",
                    "count": "Попыток",
                    "threshold": "Порог",
                    "rule": "Правило",
                    "active": "Активно",
                    "old_id": "Прежний ID",
                    "actor_id": "Администратор",
                    "action": "Действие",
                    "target": "Объект",
                    "reason": "Причина",
                }
                lines.append(
                    " · ".join(
                        f"{labels[key]}: {str(value)[:80]}" for key, value in item.items() if key in allowed
                    )
                )
            if not items:
                lines.append("Нет записей")
        section_icons = {
            "subscriptions": "subscriptions",
            "payments": "history",
            "ledger": "bonuses",
            "devices": "devices",
            "agreements": "recurring_heading",
            "orders": "order",
            "sessions": "profile",
            "blocks": "cancel",
            "credit_ledger": "cash_max",
            "aliases": "link",
            "audit": "admin",
        }
        buttons = [
            self.button(title, "admin_user", target_id=target, section=key, icon_key=section_icons[key])
            for key, title in sections
            if key in result
        ]
        if "referral" in result:
            buttons.append(
                self.button("Приглашённые", "admin_user_invitees", target_id=target, icon_key="referral")
            )
        if "moderation" in result and result.get("can_moderate") and self.can_moderate(uid):
            buttons += [
                self.button(
                    "Предупредить",
                    "admin_user_moderate",
                    target_id=target,
                    moderation_action="warn",
                    icon_key="error_heading",
                ),
                self.button(
                    "Разблокировать отвязку"
                    if result["moderation"].get("unlink_blocked")
                    else "Блокировать отвязку",
                    "admin_user_moderate",
                    target_id=target,
                    moderation_action="unblock" if result["moderation"].get("unlink_blocked") else "block",
                    icon_key="revoke",
                    semantic="danger",
                ),
            ]
            if result["moderation"].get("current_warnings", 0) > 0:
                buttons.append(
                    self.button(
                        "Снять предупреждение",
                        "admin_user_moderate",
                        target_id=target,
                        moderation_action="remove_warning",
                        icon_key="accept_legal",
                        semantic="neutral",
                    )
                )
        capabilities = self.capabilities_by_uid.get(uid, set())
        if "messages_write" in capabilities:
            buttons.append(self.button("Сообщение пользователю", "admin_user_message", target_id=target))
        if "subscriptions_manage" in capabilities:
            buttons.append(self.button("Подарить доступ", "admin_user_gift", target_id=target))
        if selected and isinstance(result.get(selected), dict):
            buttons += self.pages(result[selected], "admin_user", target_id=target, section=selected)
        elif not selected:
            pagination = max(
                (value for key, value in result.items() if key != "account" and isinstance(value, dict)),
                key=lambda value: value.get("total", 0),
                default={},
            )
            buttons += self.pages(pagination, "admin_user", target_id=target)
        await self.show(uid, "Карточка пользователя", "\n".join(lines), buttons, mid, back="admin_users")

    async def utm(self, uid, aid, action, data, mid):
        if action == "admin_utm_add":
            return await self.draft_start(
                uid,
                aid,
                "utm",
                [
                    ("name", "Введите название ссылки.", "name"),
                    ("source", "Источник трафика, например telegram.", "text"),
                    ("medium", "Тип размещения, например post.", "text"),
                    ("campaign", "Название кампании.", "text"),
                ],
                mid=mid,
            )
        if action == "admin_utm_stats":
            result = await self.call("bot_utm_stats", aid, data["link_id"])
            lines = [
                f"Запусков: {result.get('starts', 0)}",
                f"Пользователей: {result.get('unique_users', 0)}",
                f"Оплаченных заказов: {result.get('paid_orders', 0)}",
            ]
            lines += [
                "Поступления: " + money(row.get("amount", row.get("gross")), row["currency"])
                for row in result.get("gross", [])
            ]
            lines += [
                "Веб-переходов: " + str(result.get("website_visits", "недоступны")),
                "Уникальных посетителей сайта: " + str(result.get("website_unique_visitors", "недоступны")),
                "Оплаченных заказов после веб-перехода: "
                + str(result.get("website_paid_orders", "недоступны")),
            ]
            return await self.show(uid, "Статистика ссылки", "\n".join(lines), mid=mid, back="admin_utm")
        result = await self.call("bot_utm_list", aid, page=data.get("page", 1), limit=8)
        lines = [
            f"{item['name']}\nTelegram: {item.get('url') or 'недоступна'}\nСайт: {item.get('website_url') or 'недоступна'}"
            for item in result["items"]
        ]
        buttons = [self.button("Создать ссылку", "admin_utm_add")]
        buttons += [
            self.button(item["name"], "admin_utm_stats", link_id=item["id"], icon_key="product_select")
            for item in result["items"]
        ]
        await self.show(
            uid,
            "UTM-ссылки",
            "\n\n".join(lines) or "Ссылок пока нет.",
            buttons + self.pages(result, "admin_utm"),
            mid,
        )

    async def settings(self, uid, aid, action, data, mid):
        result = await self.call("bot_settings", aid)
        if action == "admin_settings":
            enabled = result.get("recurring", {}).get("enabled", False)
            return await self.show(
                uid,
                "Настройки",
                f"Новые подключения автопродления: {'включены' if enabled else 'выключены'}\n"
                "Действующие соглашения продолжают обслуживаться.",
                [
                    self.button("Новые автопродления", "admin_setting_recurring", icon_key="admin_toggle"),
                    self.button("Оценка комиссии", "admin_setting_fee", icon_key="admin_fee"),
                    self.button("Внешняя группа / префикс", "admin_setting_panel", icon_key="admin_external"),
                    self.button("Публичная оферта", "admin_setting_offer", icon_key="offer"),
                    self.button("Политика конфиденциальности", "admin_setting_privacy", icon_key="privacy"),
                ],
                mid,
            )
        section, fields, values = None, [], None
        if action == "admin_setting_recurring":
            section = "recurring"
            values = {"enabled": not result.get(section, {}).get("enabled", False)}
        elif action == "admin_setting_panel":
            section = "panel_mapping"
            fields = [
                ("external_squad_uuid", "Введите UUID внешней группы Remnawave или «—».", "uuid"),
                ("project_prefix", "Введите префикс пользователей: 2–24 латинских символа.", "prefix"),
            ]
        elif action == "admin_setting_fee":
            return await self.show(
                uid,
                "Оценки комиссий",
                "Оценки используются только в статистике и не изменяют суммы платежей.\n"
                "Неизвестная комиссия остаётся неизвестной без явно заданной оценки.",
                [
                    self.button(
                        label, "admin_setting_fee_edit", method=method, icon_key=icon, semantic="neutral"
                    )
                    for method, label, icon in [
                        ("bank_card", "Карта", "payment_bank_card"),
                        ("sbp", "СБП", "payment_sbp"),
                        ("crypto", "Криптовалюта", "payment_crypto"),
                        ("xrocket", "xRocket", "payment_xrocket"),
                    ]
                ],
                mid,
                back="admin_settings",
            )
        elif action == "admin_setting_fee_edit" and data.get("method") in {
            "bank_card",
            "sbp",
            "crypto",
            "xrocket",
        }:
            section = "fee_estimates"
            fields = [
                ("percent", "Введите оценку комиссии в процентах: 0–100.", "percent"),
                ("fixed", "Введите фиксированную часть оценки комиссии в рублях: от 0.", "fixed"),
            ]
        elif action in {"admin_setting_legal", "admin_setting_offer", "admin_setting_privacy"}:
            section = "legal"
            fields = [
                ("offer_url", "Введите HTTPS-ссылку на публичную оферту.", "url"),
                ("offer_version", "Введите версию оферты.", "text"),
                ("privacy_url", "Введите HTTPS-ссылку на политику конфиденциальности.", "url"),
                ("privacy_version", "Введите версию политики.", "text"),
            ]
            if action != "admin_setting_legal":
                kind = "offer" if action == "admin_setting_offer" else "privacy"
                fields = [field for field in fields if field[0].startswith(kind + "_")]
        if section:
            return await self.draft_start(
                uid,
                aid,
                "setting",
                fields,
                {"section": section, "before": result.get(section, {}), "method": data.get("method")},
                values,
                mid,
            )
