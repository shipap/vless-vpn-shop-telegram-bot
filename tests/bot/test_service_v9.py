import asyncio
from types import SimpleNamespace

import pytest
from sunrise_bot.safety import BotSafety
from test_admin_flow import chosen, flow, message
from test_dispatcher import callback_update, find_button, message_update, setup


def test_connect_exposes_open_control_and_html_copyable_url_without_preview():
    async def run():
        app, service, session = setup()
        url = "https://vpn.example.test/synthetic?one=1&two=2"
        service.subscriptions[0]["connection_url"] = url
        await app.execute(
            "101", "9223372036854775700", "connect", {"sid": service.subscriptions[0]["id"]}, None, ""
        )
        result = session.calls[-1]
        assert result.parse_mode == "HTML"
        assert "<code>https://vpn.example.test/synthetic?one=1&amp;two=2</code>" in result.text
        assert "Incy" in result.text and "Happ" in result.text
        from sunrise_infrastructure.service_content import CONNECTION_NOTICE

        assert CONNECTION_NOTICE not in result.text
        assert "скопир" in result.text.lower()
        buttons = [button for row in result.reply_markup.inline_keyboard for button in row]
        qr = next(button for button in buttons if "QR-код" in button.text)
        opened = next(button for button in buttons if button.url == url)
        assert buttons.index(qr) < buttons.index(opened)
        assert qr.style == "primary" and opened.style is None
        assert result.link_preview_options.is_disabled

    asyncio.run(run())


def test_user_summary_includes_referral_inviter_unlink_state_and_separate_invitees():
    async def run():
        app, admin = flow()
        result = dict(
            account=dict(
                id="9007199254740999",
                telegram_id="999",
                telegram_username="synthetic_user",
                bonus_balance="12.34",
            ),
            inviter=dict(id="9007199254740998", telegram_username="synthetic_inviter"),
            referral=dict(balance="25.50", earned_total="100.00", invited_total=4),
            active_subscriptions_total=2,
            moderation=dict(
                unlink_month=3,
                unlink_total=12,
                month="2026-10",
                current_warnings=1,
                total_warnings=3,
                unlink_blocked=True,
            ),
            subscriptions=dict(items=[], total=4, page=1, page_size=5),
        )
        app.admin.bot_console.bot_user_detail = lambda aid, target, **kwargs: result
        await admin.execute("101", "a1", "admin_user", {"target_id": "9007199254740999"}, None)
        view = app.views[-1]
        assert "@synthetic_user" in view["body"] and "@synthetic_inviter" in view["body"]
        assert "25,50 RUB" in view["body"] and "100,00 RUB" in view["body"]
        assert "Активные подписки: 2" in view["body"]
        assert "2026-10" in view["body"] and "12" in view["body"]
        assert any(a == "admin_user_invitees" for _, a, _ in view["buttons"])
        app.admin.bot_console.bot_user_invitees = lambda aid, target, **kwargs: dict(
            items=[dict(id="9007199254740900", telegram_username="synthetic_friend")],
            total=1,
            page=1,
            page_size=10,
        )
        await admin.execute("101", "a1", "admin_user_invitees", {"target_id": "9007199254740999"}, None)
        assert "@synthetic_friend" in app.views[-1]["body"]

    asyncio.run(run())


def test_owner_moderation_requires_reason_confirmation_and_rechecks_owner():
    async def run():
        app, admin = flow()
        app.accounts = SimpleNamespace(settings=SimpleNamespace(owner_id="101"))
        app.admin.bot_console.bot_user_detail = lambda aid, target: {"can_moderate": True}
        app.admin.bot_console.bot_user_moderate = lambda aid, target, action, reason, key, confirmed=False: (
            app.admin.bot_console.calls.append((target, action, reason, key, confirmed))
        )
        await admin.execute(
            "101", "a1", "admin_user_moderate", {"target_id": "9", "moderation_action": "warn"}, None
        )
        await admin.message("101", {"id": "a1"}, message("Частые отвязки устройств"))
        assert not app.admin.bot_console.calls
        stale = chosen(app, "admin_save")
        current_capabilities = app.admin.bot_console.capabilities
        app.admin.bot_console.capabilities = lambda aid: {
            "staff_role": "admin",
            "capabilities": ["users_read"],
        }
        await admin.execute("101", "a1", "admin_save", stale, None)
        assert not app.admin.bot_console.calls and app.views[-1]["body"] == "404"
        app.admin.bot_console.capabilities = current_capabilities
        await admin.execute(
            "101", "a1", "admin_user_moderate", {"target_id": "9", "moderation_action": "warn"}, None
        )
        await admin.message("101", {"id": "a1"}, message("Частые отвязки устройств"))
        await admin.execute("101", "a1", "admin_save", chosen(app, "admin_save"), None)
        assert app.admin.bot_console.calls[-1][0:2] == ("9", "warn")
        assert app.admin.bot_console.calls[-1][-1] is True

    asyncio.run(run())


def test_telegram_username_refresh_comes_only_from_trusted_update_user():
    async def run():
        app, service, session = setup()
        original = service.resolve_telegram
        observed = []

        def resolve(uid, **kwargs):
            observed.append(kwargs)
            return original(uid)

        service.resolve_telegram = resolve
        update = message_update(text="/start")
        update = update.model_copy(
            update={
                "message": update.message.model_copy(
                    update={
                        "from_user": update.message.from_user.model_copy(
                            update={"username": "synthetic_owner"}
                        )
                    }
                )
            }
        )
        await app.dispatcher.feed_update(app.bot, update)
        assert observed[0]["username"] == "synthetic_owner"
        update = callback_update(find_button(session, "Профиль"))
        await app.dispatcher.feed_update(app.bot, update)
        assert observed[-1]["username"] is None

    asyncio.run(run())


@pytest.mark.parametrize("fail_delivery", [False, True])
def test_owner_alert_acknowledges_after_delivery_without_raw_hardware_or_credentials(fail_delivery):
    async def run():
        app, _, session = setup()
        app.accounts = SimpleNamespace(
            settings=SimpleNamespace(owner_id="101", admin_telegram_allowlist=["101"])
        )
        delivered = []
        payload = dict(
            account_id="9007199254740999",
            telegram_id="999",
            telegram_username="synthetic_customer",
            email=None,
            month="2026-10",
            unlink_month=2,
            unlink_total=7,
            current_warnings=1,
            total_warnings=4,
            unlink_blocked=False,
            connection_url="https://vpn.example.test/must-not-render",
            hwid="SYNTHETIC-OWNER-HWID",
        )
        app.admin_console = SimpleNamespace(
            authorize_admin=lambda aid: True,
            pending_owner_alerts=lambda aid, limit=10: {
                "items": [{"id": "job1", "target_id": "101", "payload": payload}]
            },
            acknowledge_owner_alert=lambda aid, job: delivered.append(job),
            claim_owner_alert=lambda aid, job: {"id": job, "target_id": "101", "payload": payload},
            fail_owner_alert=lambda *args, **kwargs: None,
        )
        if fail_delivery:

            async def fail(*args, **kwargs):
                raise RuntimeError("synthetic delivery failure")

            app.presenter.show = fail
            with pytest.raises(RuntimeError):
                await app.deliver_owner_alerts()
            assert not delivered
        else:
            await app.deliver_owner_alerts()
            assert delivered == ["job1"]
            result = session.calls[-1]
            assert "@synthetic_customer" in result.text and "2026-10" in result.text
            assert "HWID:" not in result.text and "SYNTHETIC-OWNER-HWID" not in result.text
            assert "По одному" in result.text and "Все устройства" in result.text
            assert "must-not-render" not in result.text
            actions = [
                app.actions[b.callback_data]
                for row in result.reply_markup.inline_keyboard
                for b in row
                if b.callback_data and app.actions[b.callback_data].action == "admin_user_moderate"
            ]
            assert {a.data["moderation_action"] for a in actions} == {"warn", "block"}
            assert all(a.owner == "101" for a in actions)
        app.safety = BotSafety(frozenset({"700001"}), frozenset({"102"}))
        app.presenter.show = lambda *args, **kwargs: pytest.fail("unapproved alert recipient")
        assert await app.deliver_owner_alerts() == 0

    asyncio.run(run())


def test_configured_owner_receives_alert_without_stale_email_instruction():
    from sunrise_core.errors import missing

    async def run():
        app, service, session = setup()
        app.accounts = SimpleNamespace(
            settings=SimpleNamespace(owner_id="101", admin_telegram_allowlist=[], admin_email_allowlist=[])
        )
        owner = service.resolve_telegram(101)
        service.account_info = lambda aid: owner
        acknowledged = []
        app.admin_console = SimpleNamespace(
            pending_owner_alerts=lambda aid, limit=10: {
                "items": [
                    {
                        "id": "job-without-email",
                        "target_id": "101",
                        "payload": {
                            "account_id": "9007199254740999",
                            "telegram_username": "synthetic_customer",
                            "month": "2026-10",
                            "unlink_month": 1,
                            "unlink_total": 1,
                        },
                    }
                ]
            },
            acknowledge_owner_alert=lambda aid, job: acknowledged.append(job),
            claim_owner_alert=lambda aid, job: {
                "id": job,
                "target_id": "101",
                "payload": {
                    "account_id": "9007199254740999",
                    "telegram_username": "synthetic_customer",
                    "month": "2026-10",
                    "unlink_month": 1,
                    "unlink_total": 1,
                },
            },
            bot_user_moderate=lambda *args, **kwargs: pytest.fail("unverified owner must not moderate"),
        )
        assert await app.deliver_owner_alerts() == 1
        assert acknowledged == ["job-without-email"]
        assert "@synthetic_customer" in session.calls[-1].text
        assert "подтвердите разрешённый email" not in session.calls[-1].text

        def backend_denied(aid):
            raise missing()

        app.admin_console.authorize_admin = backend_denied
        await app.dispatcher.feed_update(app.bot, callback_update(find_button(session, "Предупредить")))
        assert "Страница не найдена" in session.calls[-1].text

    asyncio.run(run())


def test_renewal_quote_renders_authoritative_device_months_and_amount_without_recalculating():
    async def run():
        app, service, session = setup()
        original = service.quote
        service.quote = lambda aid, **kwargs: (
            original(aid, **kwargs)
            | {
                "base_amount": "250.00",
                "discount_amount": "10.00",
                "payment_method_discount_amount": "10.00",
                "total": "240.00",
                "payment_method": {
                    "id": "crypto",
                    "label": "Криптовалюта",
                    "discount_percent": "4",
                    "available": True,
                    "unavailable_reason": None,
                },
                "device_pricing": {
                    "amount": "150.00",
                    "unit_price": "50.00",
                    "extra_devices": 1,
                    "billing_months": 3,
                    "mode": "monthly_renewal",
                },
            }
        )
        await app.execute(
            "101",
            "9223372036854775700",
            "quote",
            {
                "product_id": "standard",
                "period_days": 90,
                "intent": "renew",
                "sid": service.subscriptions[0]["id"],
            },
            None,
            "",
        )
        assert "150,00 ₽" in session.calls[-1].text and "50,00 ₽" in session.calls[-1].text
        assert "Месяцев расчёта: 3" in session.calls[-1].text
        assert "Дополнительных устройств: 1" in session.calls[-1].text
        assert "Криптовалюта (−4%)" in session.calls[-1].text
        assert "Скидка за способ оплаты: 10,00 ₽" in session.calls[-1].text
        assert "240,00 ₽" in session.calls[-1].text
        assert "без скидок" not in session.calls[-1].text

    asyncio.run(run())


def test_faq_starts_with_current_legal_urls_and_shared_links_then_topic_controls():
    async def run():
        app, service, session = setup()
        original = service.catalog

        def catalog():
            result = original()
            result["content"]["help_center"].update(
                support_url="https://t.me/synthetic_support",
                dashboard_url="https://dashboard.example.test",
                promo_channels=[
                    {"title": "Канал с кодами", "url": "https://example.test/promo-channel"},
                    {"title": "Видеоканал", "url": "https://example.test/video-channel"},
                ],
            )
            return result

        service.catalog = catalog
        await app.main("101", {})
        assert any("FAQ" in b.text for row in session.calls[-1].reply_markup.inline_keyboard for b in row)
        await app.execute("101", "9223372036854775700", "help", {}, None, "")
        rows = session.calls[-1].reply_markup.inline_keyboard
        assert [rows[0][0].url, rows[1][0].url] == [
            "https://example.test/offer",
            "https://example.test/privacy",
        ]
        assert any(b.url == "https://t.me/synthetic_support" for row in rows for b in row)
        actions = {app.actions[b.callback_data].action for row in rows for b in row if b.callback_data}
        assert {"faq_purchase", "faq_recurring", "faq_one_click", "faq_promos", "faq_referrals"} <= actions
        await app.execute("101", "9223372036854775700", "faq_one_click", {}, None, "")
        assert "недоступ" in session.calls[-1].text.lower()
        await app.execute("101", "9223372036854775700", "faq_promos", {}, None, "")
        urls = {b.url for row in session.calls[-1].reply_markup.inline_keyboard for b in row}
        assert {"https://example.test/promo-channel", "https://example.test/video-channel"} <= urls

    asyncio.run(run())


def test_compact_pagination_keeps_both_directions_on_one_row():
    async def run():
        app, _, session = setup()
        await app.show(
            "101",
            "Пользователи",
            "",
            [
                ("Карточка", "admin_user", {"target_id": "9"}),
                ("Предыдущая", "admin_users_results", {"page": 1, "icon_key": "admin_prev"}),
                ("Следующая", "admin_users_results", {"page": 3, "icon_key": "admin_next"}),
            ],
            compact=True,
        )
        rows = session.calls[-1].reply_markup.inline_keyboard
        directions = [
            row for row in rows if any("Предыдущая" in b.text or "Следующая" in b.text for b in row)
        ]
        assert len(directions) == 1 and len(directions[0]) == 2
        await app.show(
            "101",
            "Пользователи",
            "",
            [
                ("Предыдущая", "admin_users_results", {"page": 1, "icon_key": "admin_prev"}),
                ("Следующая", "admin_users_results", {"page": 3, "icon_key": "admin_next"}),
            ],
            compact=True,
        )
        rows = session.calls[-1].reply_markup.inline_keyboard
        assert len(rows[0]) == 2

    asyncio.run(run())


def test_privacy_can_be_edited_independently_preserving_offer_and_confirmed_audit():
    async def run():
        app, admin = flow()
        await admin.execute("101", "a1", "admin_settings", {}, None)
        assert {"admin_setting_offer", "admin_setting_privacy"} <= {a for _, a, _ in app.views[-1]["buttons"]}
        await admin.execute("101", "a1", "admin_setting_privacy", {}, None)
        for value in ["https://example.org/new-privacy", "v9", "Обновление политики"]:
            await admin.message("101", {"id": "a1"}, message(value))
        assert not app.admin.bot_console.calls
        await admin.execute("101", "a1", "admin_save", chosen(app, "admin_save"), None)
        result = app.admin.bot_console.calls[-1]
        assert result[2] == {
            "documents": [
                {"document_type": "privacy", "url": "https://example.org/new-privacy", "version": "v9"},
            ],
            "partial": True,
        }
        assert result[-1] is True

    asyncio.run(run())
