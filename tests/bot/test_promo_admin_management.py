import asyncio
from types import SimpleNamespace

import pytest
from sunrise_bot.promo_admin import PromoAdminFlow
from test_admin_flow import App, chosen


def setup():
    app = App()
    app.menu_ttl = 900
    app.commerce = SimpleNamespace(account_info=True)
    app.admin.bot_promos = lambda aid: [
        dict(
            code="GIFT",
            enabled=True,
            reward_type="percent",
            percent="10",
            per_account_limit=1,
            total_limit=20,
            redeemed_count=7,
            activated_users=5,
            remaining_uses=13,
        )
    ]
    app.deleted = []
    app.admin.delete_bot_promo = lambda aid, code, reason: app.deleted.append((aid, code, reason))
    return app, PromoAdminFlow(app)


@pytest.mark.parametrize("limit", ["3", None])
def test_promo_creation_limit_goes_directly_to_review_without_reason(limit):
    async def run():
        app, flow = setup()
        await flow.execute("101", "a1", "admin_promo_create", {}, None)
        await flow.message("101", {"id": "a1"}, "NEWCODE")
        await flow.execute(
            "101", "a1", "admin_promo_type", {**chosen(app, "admin_promo_type"), "kind": "percent"}, None
        )
        await flow.message("101", {"id": "a1"}, "5")
        await flow.execute("101", "a1", "admin_promo_audience", chosen(app, "admin_promo_audience"), None)
        if limit is None:
            await flow.execute("101", "a1", "admin_promo_infinity", chosen(app, "admin_promo_infinity"), None)
        else:
            await flow.message("101", {"id": "a1"}, limit)
        assert flow.drafts["101"].step == "review"
        assert "причин" not in app.views[-1]["body"].lower()
        assert chosen(app, "admin_promo_save")

    asyncio.run(run())


def test_promo_list_is_compact_and_detail_shows_accurate_usage():
    async def run():
        app, admin = setup()
        await admin.execute("101", "a1", "admin_promos", {}, None)
        assert "Активировали" not in app.views[-1]["body"]
        assert app.views[-1]["buttons"][1][0] == "GIFT (Скидка 10%) (7/20)"
        await admin.execute("101", "a1", "admin_promo_detail", chosen(app, "admin_promo_detail"), None)
        body = app.views[-1]["body"]
        assert "Активировали: 5" in body
        assert "Использовано: 7" in body
        assert "Осталось: 13" in body and "Общий лимит: 20" in body
        assert "На аккаунт: 1" in body
        assert chosen(app, "admin_promo_delete")["code"] == "GIFT"

    asyncio.run(run())


def test_promo_delete_requires_current_confirmation_and_retains_history_notice():
    async def run():
        app, admin = setup()
        await admin.execute("101", "a1", "admin_promos", {}, None)
        await admin.execute("101", "a1", "admin_promo_detail", chosen(app, "admin_promo_detail"), None)
        await admin.execute("101", "a1", "admin_promo_delete", chosen(app, "admin_promo_delete"), None)
        assert not app.deleted
        assert "истори" in app.views[-1]["body"].lower()
        assert app.views[-1]["back"] == "admin_promo_detail"
        confirmation = chosen(app, "admin_promo_delete_confirm")
        await admin.execute("101", "a1", "admin_promo_delete_confirm", {}, None)
        assert not app.deleted
        await admin.execute("101", "a1", "admin_promo_delete_confirm", confirmation, None)
        assert app.deleted[0][:2] == ("a1", "GIFT")
        await admin.execute("101", "a1", "admin_promo_delete_confirm", confirmation, None)
        assert len(app.deleted) == 1

    asyncio.run(run())


def test_revoked_admin_cannot_confirm_promo_deletion():
    async def run():
        app, admin = setup()
        await admin.execute("101", "a1", "admin_promo_delete", {"code": "GIFT"}, None)
        confirmation = chosen(app, "admin_promo_delete_confirm")
        app.admin_allowed = lambda account: False
        await admin.execute("101", "a1", "admin_promo_delete_confirm", confirmation, None)
        assert not app.deleted
        assert not admin.drafts
        assert app.views[-1]["body"] == "404"

    asyncio.run(run())


def test_unlimited_promo_and_pagination_do_not_hide_older_codes():
    async def run():
        app, admin = setup()
        app.admin.bot_promos = lambda aid: [
            dict(
                code=f"P{i}",
                reward_type="percent",
                percent="10",
                total_limit=None,
                remaining_uses=None,
                per_account_limit=2,
                activated_users=0,
                redeemed_count=0,
            )
            for i in range(7)
        ]
        await admin.execute("101", "a1", "admin_promos", {}, None)
        next_page = chosen(app, "admin_promos")
        assert next_page["page"] == 2
        await admin.execute("101", "a1", "admin_promos", next_page, None)
        assert app.views[-1]["buttons"][2][0] == "P6 (Скидка 10%) (0/без лимита)"
        detail = chosen(app, "admin_promo_detail")
        await admin.execute("101", "a1", "admin_promo_detail", detail, None)
        assert app.views[-1]["back_data"] == {"page": 2}
        assert "Осталось: без лимита" in app.views[-1]["body"]
        assert "На аккаунт: 2" in app.views[-1]["body"]

    asyncio.run(run())


def test_fixed_discount_reservation_is_separate_from_completed_use():
    async def run():
        app, admin = setup()
        app.admin.bot_promos = lambda aid: [
            dict(
                code="111TEST",
                enabled=True,
                reward_type="fixed_rub",
                amount_rub="2222",
                total_limit=1,
                redeemed_count=0,
                activated_users=1,
                remaining_uses=0,
                reserved_count=1,
            )
        ]
        await admin.execute("101", "a1", "admin_promos", {}, None)
        assert app.views[-1]["buttons"][1][0] == "111TEST (Скидка 2 222,00 ₽) (0/1)"
        assert "резерв" not in app.views[-1]["body"].lower()
        await admin.execute("101", "a1", "admin_promo_detail", chosen(app, "admin_promo_detail"), None)
        body = app.views[-1]["body"]
        assert "Использовано: 0" in body and "Осталось: 0" in body
        assert "Резервы неоплаченных заказов: 1" in body
        assert not app.deleted

    asyncio.run(run())


def test_delete_cancel_returns_to_detail_and_original_list_page():
    async def run():
        app, admin = setup()
        await admin.execute("101", "a1", "admin_promo_detail", {"code": "GIFT", "page": 2}, None)
        await admin.execute("101", "a1", "admin_promo_delete", chosen(app, "admin_promo_delete"), None)
        assert app.views[-1]["back"] == "admin_promo_detail"
        assert app.views[-1]["back_data"] == {"code": "GIFT", "page": 2}
        await admin.message("101", {"id": "a1"}, "не удалять")
        assert app.views[-1]["back"] == "admin_promo_detail"
        assert app.views[-1]["back_data"] == {"code": "GIFT", "page": 2}
        await admin.execute("101", "a1", "admin_promo_detail", app.views[-1]["back_data"], None)
        assert not admin.drafts
        assert app.views[-1]["back_data"] == {"page": 2}
        assert not app.deleted

    asyncio.run(run())
