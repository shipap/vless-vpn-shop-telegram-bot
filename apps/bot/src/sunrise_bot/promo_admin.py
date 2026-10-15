"""Conversation adapter for the shared audited bot-admin promo service."""

import asyncio
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from secrets import token_urlsafe
from time import monotonic

from sunrise_bot.adapter import promo_reward


@dataclass
class Draft:
    owner: str
    expires: float
    step: str = "code"
    values: dict = field(default_factory=dict)
    identifier: str = field(default_factory=lambda: token_urlsafe(16))
    revision: int = 0


class PromoAdminFlow:
    def __init__(self, app):
        self.app = app
        self.drafts = {}

    def cancel(self, uid):
        self.drafts.pop(uid, None)

    async def authorized(self, uid, aid):
        account = (
            await self.app.call("account_info", aid)
            if hasattr(self.app.commerce, "account_info")
            else await self.app.call("resolve_telegram", int(uid))
        )
        return (
            str(account.get("id")) == str(aid)
            and self.app.admin_allowed(account)
            and self.app.admin is not None
        )

    async def show(self, uid, body, buttons=(), mid=None, *, back="admin_promos", back_data=None):
        draft = self.drafts.get(uid)
        if draft and any(
            action.startswith("admin_promo_") and action != "admin_promo_create" for _, action, _ in buttons
        ):
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
                if action.startswith("admin_promo_") and action != "admin_promo_create"
                else (label, action, data)
                for label, action, data in buttons
            ]
        await self.app.show(
            uid,
            "Промокоды",
            body,
            buttons,
            back=back,
            back_data=back_data,
            message_id=mid,
            header_key="admin_promos_heading",
        )

    async def message(self, uid, account, text):
        draft = self.drafts.get(uid)
        if not draft:
            return False
        if draft.owner != str(account["id"]) or draft.expires < monotonic() or text.startswith("/"):
            self.cancel(uid)
            return False
        if not await self.authorized(uid, account["id"]):
            self.cancel(uid)
            await self.app.show(uid, "Страница не найдена", "404")
            return True
        if draft.step == "delete":
            await self.show(
                uid,
                "Подтвердите удаление кнопкой или вернитесь к промокодам.",
                [
                    (
                        "Удалить промокод",
                        "admin_promo_delete_confirm",
                        {"icon_key": "admin_promo_confirm", "semantic": "danger"},
                    )
                ],
                back="admin_promo_detail",
                back_data={"code": draft.values["code"], "page": draft.values["page"]},
            )
            return True
        if draft.step == "code":
            code = text.upper()
            if not re.fullmatch(r"[A-Z0-9_-]{1,40}", code):
                await self.show(uid, "Код: 1–40 латинских букв, цифр, дефисов или подчёркиваний.")
                return True
            draft.values = {"code": code, "enabled": True, "percent": "0", "per_account_limit": 1}
            draft.step = "type"
            await self.show(
                uid,
                "Выберите награду.",
                [
                    (
                        "Скидка в процентах",
                        "admin_promo_type",
                        {"kind": "percent", "icon_key": "admin_promo_percent"},
                    ),
                    (
                        "Скидка в рублях",
                        "admin_promo_type",
                        {"kind": "fixed_rub", "icon_key": "admin_promo_fixed"},
                    ),
                    (
                        "Бесплатные дни",
                        "admin_promo_type",
                        {"kind": "free_days", "icon_key": "admin_promo_free"},
                    ),
                ],
            )
        elif draft.step == "value":
            try:
                value = Decimal(text.replace(",", "."))
                kind = draft.values["reward_type"]
                if not value.is_finite() or value <= 0 or kind == "percent" and value > 100:
                    raise ValueError
                if kind == "free_days":
                    if value != value.to_integral_value() or value > 3650:
                        raise ValueError
                    draft.values["reward_days"] = int(value)
                elif kind == "fixed_rub":
                    if value != value.quantize(Decimal("0.01")):
                        raise ValueError
                    draft.values["amount_rub"] = format(value.quantize(Decimal("0.01")), "f")
                else:
                    draft.values["percent"] = format(value, "f")
            except (ValueError, InvalidOperation):
                await self.show(
                    uid, "Проверьте значение: положительная сумма или число дней; скидка до 100%."
                )
                return True
            if kind == "free_days":
                draft.step = "product"
                catalog = await self.app.call("catalog")
                products = [product for product in catalog["products"] if product.get("available")]
                await self.show(
                    uid,
                    "Выберите тариф для новой подписки. Если у клиента уже есть подписка, сохраняется её тариф.",
                    [
                        (
                            product["name"],
                            "admin_promo_product",
                            {"product_id": product["id"], "icon_key": "product_select"},
                        )
                        for product in products
                    ],
                )
            else:
                await self.audience(uid, draft)
        elif draft.step == "limit":
            if not text.isdecimal() or not 1 <= int(text) <= 2147483647:
                await self.show(
                    uid,
                    "Введите положительное целое число активаций или выберите «Без лимита».",
                    [("Без лимита ♾", "admin_promo_infinity", {"icon_key": "infinity"})],
                )
                return True
            draft.values["total_limit"] = int(text)
            await self.review(uid, draft)
        else:
            await self.show(uid, "Продолжите выбор кнопками или вернитесь в админ-панель.")
        return True

    async def audience(self, uid, draft, mid=None):
        draft.step = "audience"
        await self.show(
            uid,
            "Кто может использовать код? Каждый аккаунт — один раз.",
            [
                ("Все клиенты", "admin_promo_audience", {"audience": "all", "icon_key": "admin_promo_all"}),
                (
                    "Только новые",
                    "admin_promo_audience",
                    {"audience": "first_purchase", "icon_key": "admin_promo_new"},
                ),
                (
                    "При продлении",
                    "admin_promo_audience",
                    {"audience": "renewal", "icon_key": "admin_promo_renewal"},
                ),
            ],
            mid,
        )

    async def review(self, uid, draft, mid=None):
        draft.step = "review"
        audience = {
            "all": "Все клиенты",
            "first_purchase": "Только новые клиенты",
            "renewal": "Продление",
        }[draft.values["audience"]]
        limit = "Без лимита" if draft.values["total_limit"] is None else str(draft.values["total_limit"])
        body = f"Код: {draft.values['code']}\nНаграда: {promo_reward(draft.values)}\nКому: {audience}\nВсего активаций: {limit}\nОдин раз на аккаунт"
        await self.show(
            uid,
            body,
            [
                (
                    "Сохранить промокод",
                    "admin_promo_save",
                    {"icon_key": "admin_promo_confirm", "semantic": "success"},
                )
            ],
            mid=mid,
        )

    async def execute(self, uid, aid, action, data, mid):
        if not await self.authorized(uid, aid):
            self.cancel(uid)
            return await self.app.show(uid, "Страница не найдена", "404", message_id=mid)
        if action == "admin_promos":
            self.cancel(uid)
            promos = await asyncio.to_thread(self.app.admin.bot_promos, aid)
            page = max(1, int(data.get("page", 1)))
            visible = promos[(page - 1) * 5 : page * 5]
            body = "Выберите промокод." if visible else "Промокодов пока нет."
            buttons = [("Создать промокод", "admin_promo_create", {"icon_key": "admin_promo_create"})]
            buttons += [
                (
                    f"{promo['code']} ({promo_reward(promo)}) "
                    f"({promo.get('redeemed_count', 0)}/{promo.get('total_limit') if promo.get('total_limit') is not None else 'без лимита'})",
                    "admin_promo_detail",
                    {"code": promo["code"], "page": page, "icon_key": "admin_promos"},
                )
                for promo in visible
            ]
            if page > 1:
                buttons.append(("Предыдущая", "admin_promos", {"page": page - 1, "icon_key": "admin_prev"}))
            if page * 5 < len(promos):
                buttons.append(("Следующая", "admin_promos", {"page": page + 1, "icon_key": "admin_next"}))
            return await self.show(
                uid,
                body,
                buttons,
                mid,
                back="admin",
            )
        if action in {"admin_promo_detail", "admin_promo_delete"}:
            promos = await asyncio.to_thread(self.app.admin.bot_promos, aid)
            promo = next((item for item in promos if item["code"] == data.get("code")), None)
            if not promo:
                return await self.show(
                    uid,
                    "Промокод не найден. Обновите список.",
                    mid=mid,
                    back_data={"page": max(1, int(data.get("page", 1)))},
                )
            self.cancel(uid)
            page = max(1, int(data.get("page", 1)))
            if action == "admin_promo_detail":
                body = (
                    f"{promo['code']} · {promo_reward(promo)}\n{'Включён' if promo.get('enabled') else 'Выключен'}"
                    f"\nАктивировали: {promo.get('activated_users', 0)} чел. · Использовано: {promo.get('redeemed_count', 0)}"
                    f"\nОбщий лимит: {promo.get('total_limit') if promo.get('total_limit') is not None else 'без лимита'} · "
                    f"Осталось: {promo.get('remaining_uses') if promo.get('remaining_uses') is not None else 'без лимита'}"
                    f"\nНа аккаунт: {promo.get('per_account_limit', 1)}"
                )
                if promo.get("reserved_count"):
                    body += f"\nРезервы неоплаченных заказов: {promo['reserved_count']}. Они учитываются в остатке, но не в использованных."
                if promo.get("historical_usage_unknown"):
                    body += "\nСтарые данные неполные: показаны подтверждённые активации и использования; остаток учитывает исторические резервы."
                return await self.show(
                    uid,
                    body,
                    [
                        (
                            "Удалить промокод",
                            "admin_promo_delete",
                            {
                                "code": promo["code"],
                                "page": page,
                                "icon_key": "admin_promo_confirm",
                                "semantic": "danger",
                            },
                        )
                    ],
                    mid,
                    back_data={"page": page},
                )
            self.drafts[uid] = Draft(
                str(aid),
                monotonic() + self.app.menu_ttl,
                step="delete",
                values={"code": promo["code"], "page": page},
            )
            return await self.show(
                uid,
                f"Удалить промокод {promo['code']}?\nНовые активации прекратятся. История использования и финансовые операции сохранятся.",
                [
                    (
                        "Удалить промокод",
                        "admin_promo_delete_confirm",
                        {"icon_key": "admin_promo_confirm", "semantic": "danger"},
                    )
                ],
                mid,
                back="admin_promo_detail",
                back_data={"code": promo["code"], "page": page},
            )
        if action == "admin_promo_create":
            if len(self.drafts) >= 1000:
                self.drafts.pop(next(iter(self.drafts)))
            self.drafts[uid] = Draft(str(aid), monotonic() + self.app.menu_ttl)
            return await self.show(uid, "Отправьте код нового промокода одним сообщением.", mid=mid)
        draft = self.drafts.get(uid)
        if not draft or draft.owner != str(aid) or draft.expires < monotonic():
            self.cancel(uid)
            return await self.show(
                uid,
                "Черновик устарел. Создайте промокод заново.",
                [("Создать промокод", "admin_promo_create", {"icon_key": "admin_promo_create"})],
                mid,
            )
        if (
            data.get("draft_id") != draft.identifier
            or data.get("draft_revision") != draft.revision
            or data.get("draft_owner") != draft.owner
        ):
            return await self.app.show(
                uid,
                "Промокоды",
                "Кнопка устарела. Текущий черновик не изменён; продолжите с его актуальными кнопками.",
                back="admin",
                message_id=mid,
                header_key="admin_promos_heading",
            )
        if action == "admin_promo_delete_confirm" and draft.step == "delete":
            code = draft.values["code"]
            page = draft.values["page"]
            await asyncio.to_thread(
                self.app.admin.delete_bot_promo, aid, code, "Удаление промокода " + code + " через Telegram"
            )
            self.cancel(uid)
            return await self.show(
                uid,
                "Промокод удалён. История использования сохранена.",
                [("Промокоды", "admin_promos", {"page": page, "icon_key": "admin_promos"})],
                mid,
                back_data={"page": page},
            )
        if (
            action == "admin_promo_type"
            and draft.step == "type"
            and data.get("kind") in {"percent", "fixed_rub", "free_days"}
        ):
            draft.values["reward_type"] = data["kind"]
            draft.step = "value"
            label = {
                "percent": "Введите процент скидки: от 0 до 100.",
                "fixed_rub": "Введите сумму скидки в рублях.",
                "free_days": "Введите количество бесплатных дней.",
            }[data["kind"]]
            return await self.show(uid, label, mid=mid)
        if action == "admin_promo_product" and draft.step == "product":
            catalog = await self.app.call("catalog")
            if any(
                product["id"] == data.get("product_id") and product.get("available")
                for product in catalog["products"]
            ):
                draft.values["product_id"] = data["product_id"]
                return await self.audience(uid, draft, mid)
        if (
            action == "admin_promo_audience"
            and draft.step == "audience"
            and data.get("audience") in {"all", "first_purchase", "renewal"}
        ):
            draft.values["audience"] = data["audience"]
            draft.step = "limit"
            return await self.show(
                uid,
                "Введите общий лимит активаций или выберите «Без лимита». На один аккаунт — одна активация.",
                [("Без лимита ♾", "admin_promo_infinity", {"icon_key": "infinity"})],
                mid,
            )
        if action == "admin_promo_infinity" and draft.step == "limit":
            draft.values["total_limit"] = None
            return await self.review(uid, draft, mid)
        if action == "admin_promo_save" and draft.step == "review":
            await asyncio.to_thread(
                self.app.admin.save_bot_promo,
                aid,
                draft.values,
                "Создание промокода " + draft.values["code"] + " через Telegram",
            )
            self.cancel(uid)
            return await self.show(
                uid,
                "Промокод сохранён. Изменение записано в журнал.",
                [("Промокоды", "admin_promos", {"icon_key": "admin_promos"})],
                mid,
            )
        return await self.show(uid, "Этот шаг уже изменился. Продолжите текущее создание промокода.", mid=mid)
