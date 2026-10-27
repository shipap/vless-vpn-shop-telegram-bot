import asyncio
from types import SimpleNamespace

import pytest


class Console:
    def __init__(self):
        self.allowed = True
        self.calls = []

    def authorize_admin(self, actor):
        return self.allowed

    def capabilities(self, actor):
        from sunrise_infrastructure.staff_permissions import OWNER_CAPABILITIES

        return {"staff_role": "owner", "capabilities": sorted(OWNER_CAPABILITIES)}

    def bot_statistics(self, actor, **dates):
        self.calls.append(("statistics", dates))
        money = [{"currency": "RUB", "gross": "100.00", "actual_net": "90.00", "unknown_fee_count": 1}]
        return dict(
            period=dict(start_date="2026-10-01", end_date="2026-10-08", timezone="Europe/Moscow"),
            period_money=money,
            today_money=money,
            all_time_money=money,
            purchase_count=2,
            telegram_users=3,
            recurring_users=1,
            popular_products=[dict(name="Обычный", count=2, percent="100")],
            payment_methods=[dict(method="card", count=1)],
            unknown_method_count=1,
        )

    def bot_products(self, actor):
        return {
            "items": [
                dict(
                    id="p1",
                    name="Тариф",
                    price="100",
                    currency="RUB",
                    version=1,
                    description="Описание",
                    internal_squad_uuid=None,
                    content={},
                )
            ],
            "total": 1,
        }

    def bot_product_save(self, actor, product, patch, reason, confirmed=False):
        self.calls.append(("save", product, patch, reason, confirmed))
        return patch

    def bot_broadcast_preview(self, actor, nodes):
        return {"nodes": nodes, "recipient_count": 2}

    def bot_broadcast_create(self, actor, nodes, reason, confirmed=False):
        self.calls.append(("broadcast", nodes, reason, confirmed))
        return {"id": "j1", "state": "draft", "recipient_count": 2}

    def bot_broadcast_detail(self, actor, identifier):
        return {
            "id": identifier,
            "state": "draft",
            "recipient_count": 2,
            "delivery": "local_artifact_only",
            "delivered_count": 0,
            "failed_count": 0,
        }

    def bot_settings(self, actor):
        return {
            "legal": {
                "documents": [
                    dict(document_type="offer", url="https://example.com/offer", version="1"),
                    dict(document_type="privacy", url="https://example.com/privacy", version="1"),
                ]
            },
            "recurring": {"enabled": False},
            "fee_estimates": {"bank_card": {"percent": "2", "fixed": "0", "currency": "RUB"}},
        }

    def bot_settings_update(self, actor, section, values, reason, confirmed=False):
        self.calls.append(("setting", section, values, reason, confirmed))

    def bot_catalog_intro_save(self, actor, content, reason, confirmed=False):
        self.calls.append(("intro", content, reason, confirmed))


class App:
    def __init__(self):
        from sunrise_infrastructure.instance_config import InstanceConfig

        self.instance = InstanceConfig()
        self.admin = SimpleNamespace(bot_console=Console())
        self.commerce = SimpleNamespace()
        self.views = []

    async def call(self, method, *args):
        return {"id": "a1", "telegram_id": "101"}

    def admin_allowed(self, account):
        return True

    async def show(self, uid, title, body, buttons=(), **kwargs):
        self.views.append(dict(title=title, body=body, buttons=buttons, **kwargs))


def flow():
    from sunrise_bot.admin_flow import AdminFlow

    app = App()
    return app, AdminFlow(app)


def chosen(app, action):
    return next(data for _, candidate, data in app.views[-1]["buttons"] if candidate == action)


def message(text, entities=()):
    return SimpleNamespace(text=text, entities=list(entities))


@pytest.mark.parametrize("kind", ["settings", "product", "backup", "message", "broadcast", "utm", "grant"])
def test_completed_admin_fields_go_directly_to_confirmation_with_automatic_reason(kind):
    async def run():
        app, admin = flow()
        values = (
            {"price": "10.00"} if kind == "product" else {"nodes": [{"type": "text", "text": "Сообщение"}]}
        )
        await admin.draft_start("101", "a1", kind, [], values=values)
        draft = admin.drafts["101"]
        assert draft.step == "review"
        assert 5 <= len(draft.reason) <= 500
        assert app.views[-1]["title"] == "Проверьте изменение"
        assert "Причина" not in app.views[-1]["body"]

    asyncio.run(run())


def test_admin_menu_groups_all_operational_sections():
    async def run():
        app, admin = flow()
        await admin.execute("101", "a1", "admin", {}, None)
        actions = {action for _, action, _ in app.views[-1]["buttons"]}
        assert {
            "admin_stats",
            "admin_referrals",
            "admin_products",
            "admin_broadcasts",
            "admin_promos",
            "admin_users",
            "admin_utm",
            "admin_settings",
        } <= actions
        assert app.views[-1]["compact"]
        assert app.views[-1]["back"] == "main"

    asyncio.run(run())


@pytest.mark.parametrize("field", ["name", "description"])
def test_product_copy_goes_directly_to_confirmation_with_audited_reason(field):
    async def run():
        app, admin = flow()
        await admin.execute("101", "a1", "admin_product_edit", {"product_id": "p1", "field": field}, None)
        await admin.message("101", {"id": "a1"}, message("Новое описание"))
        assert admin.drafts["101"].step == "review"
        assert app.views[-1]["back"] == "admin_products"
        await admin.execute("101", "a1", "admin_save", chosen(app, "admin_save"), None)
        assert len(app.admin.bot_console.calls[-1][3]) >= 5
        assert app.admin.bot_console.calls[-1][2][field] == "Новое описание"

    asyncio.run(run())


def test_catalog_intro_goes_directly_to_confirmation():
    async def run():
        app, admin = flow()
        await admin.execute("101", "a1", "admin_catalog_intro", {}, None)
        await admin.message("101", {"id": "a1"}, message("Описание каталога"))
        assert admin.drafts["101"].step == "review"
        await admin.execute("101", "a1", "admin_save", chosen(app, "admin_save"), None)
        assert app.admin.bot_console.calls[-1][0] == "intro"

    asyncio.run(run())


def test_statistics_names_unknown_fees_without_fabricating_profit():
    async def run():
        app, admin = flow()
        await admin.execute("101", "a1", "admin_stats", {}, None)
        body = app.views[-1]["body"]
        assert "Сегодня" in body and "За всё время" in body and "После эквайринга" in body
        assert "Комиссия неизвестна: 1" in body and "Не определён: 1" in body
        assert "100" in body and "100%" in body
        assert "прибыль" not in body.lower()

    asyncio.run(run())


def test_product_change_needs_reason_confirmation_and_rejects_old_save():
    async def run():
        app, admin = flow()
        await admin.execute("101", "a1", "admin_product_edit", {"product_id": "p1", "field": "price"}, None)
        await admin.message("101", {"id": "a1"}, message("150,50"))
        assert not app.admin.bot_console.calls
        await admin.message("101", {"id": "a1"}, message("Изменение цены тарифа"))
        save = chosen(app, "admin_save")
        await admin.execute("101", "a1", "admin_product_edit", {"product_id": "p1", "field": "price"}, None)
        await admin.execute("101", "a1", "admin_save", save, None)
        assert not app.admin.bot_console.calls
        await admin.message("101", {"id": "a1"}, message("200"))
        await admin.message("101", {"id": "a1"}, message("Новая цена тарифа"))
        await admin.execute("101", "a1", "admin_save", chosen(app, "admin_save"), None)
        assert app.admin.bot_console.calls[-1][2]["price"] == "200.00"
        assert app.admin.bot_console.calls[-1][-1] is True

    asyncio.run(run())


def test_revoked_admin_cannot_save_current_draft():
    async def run():
        app, admin = flow()
        await admin.execute("101", "a1", "admin_product_edit", {"product_id": "p1", "field": "name"}, None)
        app.admin.bot_console.allowed = False
        await admin.message("101", {"id": "a1"}, message("Новое имя"))
        assert app.views[-1]["body"] == "404" and not admin.drafts

    asyncio.run(run())


def test_broadcast_custom_emoji_preserved_after_utf16_surrogate():
    async def run():
        app, admin = flow()
        await admin.execute("101", "a1", "admin_broadcast_compose", {}, None)
        entity = SimpleNamespace(type="custom_emoji", offset=3, length=2, custom_emoji_id="123456", url=None)
        await admin.message("101", {"id": "a1"}, message("😀 🎁 Привет", [entity]))
        await admin.message("101", {"id": "a1"}, message("Информация о тарифах"))
        await admin.execute("101", "a1", "admin_save", chosen(app, "admin_save"), None)
        from sunrise_bot.content import render_plain

        nodes = app.admin.bot_console.calls[-1][1]
        assert render_plain(nodes) == "😀 🎁 Привет"
        assert any(node.get("id") == "123456" for node in nodes[0]["children"])

    asyncio.run(run())


@pytest.mark.parametrize("text", ["NaN", "Infinity", "-2", "0", "1.001"])
def test_invalid_product_price_does_not_advance(text):
    async def run():
        app, admin = flow()
        await admin.execute("101", "a1", "admin_product_edit", {"product_id": "p1", "field": "price"}, None)
        await admin.message("101", {"id": "a1"}, message(text))
        assert admin.drafts["101"].step == "fields"
        assert not app.admin.bot_console.calls

    asyncio.run(run())


def test_legal_editor_saves_document_version_contract():
    async def run():
        app, admin = flow()
        await admin.execute("101", "a1", "admin_setting_legal", {}, None)
        for value in [
            "https://example.org/offer",
            "2",
            "https://example.org/privacy",
            "3",
            "Обновление документов",
        ]:
            await admin.message("101", {"id": "a1"}, message(value))
        await admin.execute("101", "a1", "admin_save", chosen(app, "admin_save"), None)
        payload = app.admin.bot_console.calls[-1][2]
        assert payload == {
            "documents": [
                dict(document_type="offer", url="https://example.org/offer", version="2"),
                dict(document_type="privacy", url="https://example.org/privacy", version="3"),
            ]
        }

    asyncio.run(run())


def test_fee_estimate_updates_selected_method_preserving_other_estimates():
    async def run():
        app, admin = flow()
        await admin.execute("101", "a1", "admin_setting_fee_edit", {"method": "sbp"}, None)
        for value in ["0,7", "0", "Оценка комиссии СБП"]:
            await admin.message("101", {"id": "a1"}, message(value))
        await admin.execute("101", "a1", "admin_save", chosen(app, "admin_save"), None)
        payload = app.admin.bot_console.calls[-1][2]
        assert payload["sbp"] == {"percent": "0.7", "fixed": "0.00", "currency": "RUB"}
        assert payload["bank_card"]["percent"] == "2"

    asyncio.run(run())


def test_intro_custom_emoji_saved_as_ast():
    async def run():
        app, admin = flow()
        await admin.execute("101", "a1", "admin_catalog_intro", {}, None)
        await admin.message(
            "101",
            {"id": "a1"},
            message(
                "Каталог 🎁",
                [
                    SimpleNamespace(
                        type="custom_emoji", offset=8, length=2, custom_emoji_id="987654", url=None
                    )
                ],
            ),
        )
        await admin.message("101", {"id": "a1"}, message("Оформление каталога"))
        await admin.execute("101", "a1", "admin_save", chosen(app, "admin_save"), None)
        assert any(node.get("id") == "987654" for node in app.admin.bot_console.calls[-1][1][0]["children"])

    asyncio.run(run())


def test_local_broadcast_results_do_not_claim_telegram_delivery():
    async def run():
        app, admin = flow()
        await admin.execute("101", "a1", "admin_broadcast", {"job_id": "j1"}, None)
        assert "Локально обработано" in app.views[-1]["body"]
        assert "Реальная отправка не выполнялась" in app.views[-1]["body"]
        assert "Доставлено" not in app.views[-1]["body"]

    asyncio.run(run())


def test_text_on_review_preserves_working_confirmation_button():
    async def run():
        app, admin = flow()
        await admin.execute("101", "a1", "admin_product_edit", {"product_id": "p1", "field": "price"}, None)
        await admin.message("101", {"id": "a1"}, message("150"))
        await admin.message("101", {"id": "a1"}, message("Новая цена тарифа"))
        await admin.message("101", {"id": "a1"}, message("Сохранить"))
        await admin.execute("101", "a1", "admin_save", chosen(app, "admin_save"), None)
        assert app.admin.bot_console.calls[-1][0] == "save"

    asyncio.run(run())


def test_all_time_control_requests_historical_period():
    async def run():
        app, admin = flow()
        await admin.execute("101", "a1", "admin_stats", {"period": "all"}, None)
        assert app.admin.bot_console.calls[-1][1]["start_date"] == "1900-01-01"

    asyncio.run(run())


def test_statistics_uses_actual_dto_method_percent_and_unknown_net():
    async def run():
        app, admin = flow()
        app.admin.bot_console.bot_statistics = lambda aid, **dates: dict(
            period_money=[
                dict(currency="RUB", gross="100", actual_net=None, actual_net_count=0, unknown_fee_count=2)
            ],
            today_money=[],
            all_time_money=[],
            purchase_count=4,
            popular_products=[dict(name="Универсальный", count=4, percent="100.00")],
            payment_methods=[dict(method="bank_card", count=3, percent="75.00", denominator=4)],
            unknown_method_count=1,
        )
        await admin.execute("101", "a1", "admin_stats", {}, None)
        body = app.views[-1]["body"]
        assert "Карта: 3 · 75.00%" in body
        assert "Универсальный: 4 · 100.00%" in body
        assert "неизвестно" in body and "Комиссия неизвестна: 2" in body

    asyncio.run(run())


def test_product_catalog_pagination_keeps_every_product_reachable():
    async def run():
        app, admin = flow()
        app.admin.bot_console.bot_products = lambda aid: {
            "items": [dict(id=f"p{i}", name=f"Тариф {i}") for i in range(31)],
            "total": 31,
        }
        await admin.execute("101", "a1", "admin_products", {}, None)
        assert chosen(app, "admin_products")["page"] == 2
        await admin.execute("101", "a1", "admin_products", {"page": 4}, None)
        assert any(data.get("product_id") == "p30" for _, _, data in app.views[-1]["buttons"])

    asyncio.run(run())


def test_user_section_pagination_uses_largest_selected_section_and_safe_bonus_field():
    async def run():
        app, admin = flow()
        app.admin.bot_console.bot_user_detail = lambda aid, target, **kwargs: dict(
            account=dict(id="9", telegram_id="999", bonus_balance="12.34"),
            payments=dict(items=[], total=0, page=1, page_size=5),
            subscriptions=dict(
                items=[dict(id="s", name="Подписка", status="active")], total=8, page=1, page_size=5
            ),
            ledger=dict(items=[], total=0, page=1, page_size=5),
        )
        await admin.execute("101", "a1", "admin_user", {"target_id": "9", "section": "subscriptions"}, None)
        assert "12,34 RUB" in app.views[-1]["body"]
        assert any(
            data.get("page") == 2 and data.get("section") == "subscriptions"
            for _, _, data in app.views[-1]["buttons"]
        )

    asyncio.run(run())


def test_test_to_self_sends_separate_preview_and_preserves_current_confirmation():
    async def run():
        app, admin = flow()
        await admin.execute("101", "a1", "admin_broadcast_compose", {}, None)
        await admin.message("101", {"id": "a1"}, message("Тестовый текст"))
        await admin.message("101", {"id": "a1"}, message("Проверка сообщения"))
        previous = len(app.views)
        await admin.execute("101", "a1", "admin_broadcast_test", chosen(app, "admin_broadcast_test"), 77)
        assert len(app.views) == previous + 2
        assert app.views[-2]["message_id"] is None
        assert app.views[-2]["nodes"] == admin.drafts["101"].values["nodes"]
        assert chosen(app, "admin_save")["draft_revision"] == admin.drafts["101"].revision
        assert not app.admin.bot_console.calls

    asyncio.run(run())


def test_user_summary_is_compact_and_exposes_safe_credit_identity_and_audit():
    async def run():
        app, admin = flow()
        app.admin.bot_console.bot_user_detail = lambda aid, target, **kwargs: dict(
            account=dict(
                id="9",
                telegram_id="999",
                bonus_balance="12.34",
                credit_available="120",
                credit_currency="EUR",
            ),
            identities=dict(email="admin@example.org", email_verified=True, aliases=["9007199254740999"]),
            subscriptions=dict(
                items=[dict(id=str(i), name="x" * 200, status="active") for i in range(5)],
                total=25,
                page=1,
                page_size=5,
            ),
            audit=dict(items=[], total=1, page=1, page_size=5),
        )
        await admin.execute("101", "a1", "admin_user", {"target_id": "9"}, None)
        body = app.views[-1]["body"]
        assert "120,00 EUR" in body and "9007199254740999" in body
        assert "Подписки: 25" in body and "x" * 121 not in body
        assert body.count("x" * 120) == 3
        assert any(data.get("section") == "audit" for _, _, data in app.views[-1]["buttons"])

    asyncio.run(run())


def test_statistics_sales_title_server_period_russian_dates_and_referral_navigation():
    async def run():
        app, admin = flow()
        await admin.execute("101", "a1", "admin_stats", {}, None)
        view = app.views[-1]
        assert view["title"] == "Статистика продаж"
        assert "01.10.2026 — 08.10.2026 · МСК" in view["body"]
        assert any(action == "admin_referrals" for _, action, _ in view["buttons"])
        icons = [data["icon_key"] for _, _, data in view["buttons"]]
        assert len(icons) == len(set(icons))

    asyncio.run(run())


def test_referral_sort_controls_have_distinct_semantic_icons():
    async def run():
        app, admin = flow()
        app.admin.bot_console.bot_referrals = lambda aid, **kwargs: dict(
            items=[], total=0, page=1, page_size=20
        )
        await admin.execute("101", "a1", "admin_referrals", {}, None)
        assert "15%" not in app.views[-1]["body"]
        assert "Начисленные вознаграждения" in app.views[-1]["body"]
        assert {data["icon_key"] for _, _, data in app.views[-1]["buttons"]} == {
            "referral_reward",
            "referral_count",
        }

    asyncio.run(run())


def test_dynamic_catalog_page_uses_eight_verified_icon_variants():
    async def run():
        app, admin = flow()
        app.admin.bot_console.bot_products = lambda aid: dict(
            items=[dict(id=f"p{i}", name=f"Тариф {i}") for i in range(12)], total=12
        )
        await admin.execute("101", "a1", "admin_products", {}, None)
        rows = [data for _, action, data in app.views[-1]["buttons"] if action == "admin_product"]
        assert len(rows) == 8 and all(data["icon_key"] == "product_select" for data in rows)

    asyncio.run(run())


def test_user_detail_section_buttons_have_distinct_semantic_icons():
    async def run():
        app, admin = flow()
        section = dict(items=[], total=0, page=1, page_size=5)
        app.admin.bot_console.bot_user_detail = lambda aid, target, **kwargs: dict(
            account=dict(id="9"),
            **{
                key: section
                for key in [
                    "subscriptions",
                    "payments",
                    "ledger",
                    "devices",
                    "agreements",
                    "orders",
                    "sessions",
                    "blocks",
                    "credit_ledger",
                    "aliases",
                    "audit",
                ]
            },
        )
        await admin.execute("101", "a1", "admin_user", {"target_id": "9"}, None)
        keys = [data["icon_key"] for _, _, data in app.views[-1]["buttons"] if "section" in data]
        assert len(keys) == 11 and len(set(keys)) == 11

    asyncio.run(run())
