"""Регрессии редактора каталога и безопасной карточки пользователя."""

import asyncio
from types import SimpleNamespace

import pytest
from sunrise_bot.content import render_classic, render_rich
from test_admin_flow import chosen, flow, message


@pytest.mark.parametrize("field", ["name", "description"])
def test_product_edit_sends_only_changed_ast_and_previews_custom_emoji(field):
    async def run():
        app, admin = flow()
        products = app.admin.bot_console.bot_products

        def catalog(actor):
            result = products(actor)
            result["items"][0]["content"] = {
                "legacy_item_data_preserved_in": "private-import-record",
                "description": [{"type": "text", "text": "Старое"}],
            }
            return result

        app.admin.bot_console.bot_products = catalog
        await admin.execute("101", "a1", "admin_product_edit", {"product_id": "p1", "field": field}, None)
        entity = SimpleNamespace(type="custom_emoji", offset=3, length=1, custom_emoji_id="9007199254740993")
        await admin.message("101", {"id": "a1"}, message("🚀 ⭐ Тариф", [entity]))
        preview = app.views[-1]
        rendered = render_classic(preview.get("nodes") or [], custom_emoji=True)
        assert '<tg-emoji emoji-id="9007199254740993">⭐ ' not in rendered
        assert '<tg-emoji emoji-id="9007199254740993">⭐</tg-emoji>' in rendered
        await admin.execute("101", "a1", "admin_save", chosen(app, "admin_save"), None)
        patch = app.admin.bot_console.calls[-1][2]
        assert set(patch["content"]) == {field}
        assert "private-import-record" not in rendered

    asyncio.run(run())


def test_admin_product_card_uses_authored_name_and_description_ast():
    async def run():
        app, admin = flow()
        products = app.admin.bot_console.bot_products

        def catalog(actor):
            result = products(actor)
            result["items"][0]["content"] = {
                "name": [{"type": "emoji", "id": "9007199254740993", "fallback": "⭐", "label": "Звезда"}],
                "description": [{"type": "bold", "children": [{"type": "text", "text": "Описание AST"}]}],
            }
            return result

        app.admin.bot_console.bot_products = catalog
        await admin.execute("101", "a1", "admin_product", {"product_id": "p1"}, None)
        rendered = render_classic(app.views[-1].get("nodes") or [], custom_emoji=True)
        assert 'emoji-id="9007199254740993"' in rendered
        assert "<b>Описание AST</b>" in rendered
        assert "100,00 RUB" in rendered

    asyncio.run(run())


def test_product_card_keeps_flat_content_in_separate_readable_blocks():
    async def run():
        app, admin = flow()
        products = app.admin.bot_console.bot_products

        def catalog(actor):
            result = products(actor)
            result["items"][0]["content"] = {
                "name": [{"type": "text", "text": "Название"}],
                "description": [{"type": "text", "text": "Описание"}],
            }
            return result

        app.admin.bot_console.bot_products = catalog
        await admin.execute("101", "a1", "admin_product", {"product_id": "p1"}, None)
        rendered = render_classic(app.views[-1]["nodes"], custom_emoji=True)
        assert "НазваниеОписание" not in rendered
        assert "Название\n" in rendered and "Описание\n" in rendered
        blocks = render_rich(app.views[-1]["nodes"], custom_emoji=True).blocks
        assert any(getattr(block, "text", "").strip() == "Название" for block in blocks)
        assert any(getattr(block, "text", "").strip() == "Описание" for block in blocks)

    asyncio.run(run())


def test_user_summary_shows_registration_and_subscription_details_without_secrets():
    async def run():
        app, admin = flow()
        result = {
            "account": {
                "id": "9007199254740993",
                "telegram_id": "9007199254740995",
                "created_at": "2026-10-01T22:30:00+00:00",
            },
            "active_agreements_total": 2,
            "subscriptions": {
                "items": [
                    {
                        "name": "Личный VPN",
                        "status": "active",
                        "expires_at": "2026-11-01T22:30:00+00:00",
                        "devices_limit": 6,
                        "panel_uuid": "secret-panel-id",
                        "url": "https://secret.test/token",
                    }
                ],
                "total": 1,
                "page": 1,
                "page_size": 5,
            },
        }
        await admin.user_detail("101", result, "9007199254740993", None)
        body = app.views[-1]["body"]
        for expected in [
            "9007199254740993",
            "9007199254740995",
            "02.10.2026 01:30",
            "МСК",
            "Личный VPN",
            "02.11.2026 01:30",
            "6",
            "Активные автопродления: 2",
        ]:
            assert expected in body
        assert "secret" not in body

    asyncio.run(run())


@pytest.mark.parametrize(
    ("section", "item", "expected"),
    [
        (
            "devices",
            {"name": "Телефон", "revoked": True, "last_seen_at": "2026-10-01T22:30:00+00:00"},
            ["Отозвано: да", "02.10.2026 01:30"],
        ),
        (
            "sessions",
            {"confirmed_at": "2026-10-01T22:30:00+00:00", "mfa_verified": False},
            ["Подтверждён", "MFA: нет"],
        ),
        ("credit_ledger", {"amount": "12", "provenance": "Возврат"}, ["Основание: Возврат"]),
    ],
)
def test_user_sections_display_safe_fields_with_readable_dates(section, item, expected):
    async def run():
        app, admin = flow()
        record = item | {"token": "secret-token", "hwid": "secret-hwid"}
        result = {"account": {"id": "9"}, section: {"items": [record], "total": 6, "page": 1, "page_size": 5}}
        await admin.user_detail("101", result, "9", None, section)
        view = app.views[-1]
        assert all(text in view["body"] for text in expected)
        assert "secret" not in view["body"]
        next_page = next(data for _, _, data in view["buttons"] if data.get("page") == 2)
        assert next_page["target_id"] == "9" and next_page["section"] == section

    asyncio.run(run())
