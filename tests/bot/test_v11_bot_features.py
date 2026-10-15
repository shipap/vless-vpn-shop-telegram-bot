import asyncio

import pytest
from aiogram import Bot
from aiogram.types import BufferedInputFile
from sqlalchemy import select
from sunrise_bot.adapter import BotApplication
from sunrise_bot.safety import BotSafety
from sunrise_core.errors import DomainError
from sunrise_infrastructure.admin import Admin
from sunrise_infrastructure.bot_admin import BotAdmin
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import Account, Session, Subscription
from test_dispatcher import callback_update, find_button, message_update
from test_presentation import TOKEN, FakeSession

from tests.integration.test_v11_account_features import CURRENT, TG

pytest_plugins = ["tests.integration.test_v11_account_features"]


def test_v11_bot_unlink_requires_explicit_actual_sender_confirmation_and_never_recreates_account(
    account_features_context,
):
    db, accounts, features, _ = account_features_context
    proof = features.request_unlink(101, CURRENT, "127.0.0.1")["proof"]

    async def run():
        transport = FakeSession()
        bot = Bot(TOKEN, session=transport)
        app = BotApplication(
            bot,
            Commerce(db),
            BotSafety(frozenset({"700001"}), frozenset({str(TG), str(TG + 1)})),
            accounts=accounts,
        )
        await app.dispatcher.feed_update(bot, message_update(TG, "/unlink " + proof))
        with db.transaction() as tx:
            assert tx.get(Account, 101).telegram_id == TG
        token = find_button(transport, "Подтвердить отвязку")
        await app.dispatcher.feed_update(bot, callback_update(token, TG + 1))
        with db.transaction() as tx:
            assert tx.get(Account, 101).telegram_id == TG
            assert len(list(tx.scalars(select(Account)))) == 3
        await app.dispatcher.feed_update(bot, callback_update(token, TG))
        await app.dispatcher.feed_update(bot, callback_update(token, TG))
        await app.dispatcher.feed_update(bot, message_update(TG, "/unlink " + proof))
        with db.transaction() as tx:
            assert tx.get(Account, 101).telegram_id is None
            assert tx.get(Session, CURRENT) is not None
            assert tx.get(Subscription, "v11-paid-sub").account_id == 101
            assert len(list(tx.scalars(select(Account)))) == 3
        await bot.session.close()

    asyncio.run(run())


def test_v11_bot_ordinary_admin_cannot_invoke_owner_mutations_and_owner_needs_no_web_mfa(
    account_features_context,
):
    db, accounts, _, _ = account_features_context
    accounts.settings.admin_email_allowlist.append("old@account.test")
    accounts.settings.admin_telegram_allowlist.append(str(TG))
    commerce = Commerce(db)
    commerce.accounts = accounts
    facade = BotAdmin(Admin(commerce, accounts.guard, accounts), accounts.settings)
    assert facade.bot_users_search(101, "old@account.test")["total"] == 1
    with pytest.raises(DomainError) as failure:
        facade.bot_settings(101)
    assert failure.value.code == "staff_permission_denied"
    with pytest.raises(DomainError) as failure:
        facade.bot_product_save(101, "standard", {"name": "Запрещено"}, "Проверка запрета", confirmed=True)
    assert failure.value.code == "staff_permission_denied"
    facade.bot_product_save(102, "standard", {"name": "Bot edit"}, "Bot configuration edit", confirmed=True)


def test_v11_bot_photo_transport_accepts_only_local_png_and_same_guarded_recipient():
    async def run():
        transport = FakeSession()
        bot = Bot(TOKEN, session=transport)
        BotApplication(bot, object(), BotSafety(frozenset({"700001"}), frozenset({"101"})))
        with pytest.raises(PermissionError):
            await bot.send_photo(101, "https://example.test/qr.png")
        with pytest.raises(PermissionError):
            await bot.send_photo(102, BufferedInputFile(b"\x89PNG\r\n\x1a\n", filename="qr.png"))
        await bot.send_photo(101, BufferedInputFile(b"\x89PNG\r\n\x1a\n", filename="qr.png"))
        assert len(transport.calls) == 1
        await bot.session.close()

    asyncio.run(run())


def test_v11_bot_admin_menu_exposes_only_actual_current_capabilities(account_features_context):
    db, accounts, _, _ = account_features_context
    accounts.settings.admin_email_allowlist.append("old@account.test")
    accounts.settings.admin_telegram_allowlist.append(str(TG))
    commerce = Commerce(db)
    commerce.accounts = accounts
    facade = BotAdmin(Admin(commerce, accounts.guard, accounts), accounts.settings)

    async def run():
        transport = FakeSession()
        bot = Bot(TOKEN, session=transport)
        app = BotApplication(
            bot,
            commerce,
            BotSafety(frozenset({"700001"}), frozenset({str(TG)})),
            accounts=accounts,
            admin_console=facade,
        )
        await app.admin_flow.execute(str(TG), "101", "admin", {}, None)
        actions = {
            app.actions[button.callback_data].action
            for row in transport.calls[-1].reply_markup.inline_keyboard
            for button in row
        }
        assert {"admin_stats", "admin_users", "admin_broadcasts", "admin_promos"} <= actions
        assert not {"admin_settings", "admin_products"} & actions
        await app.admin_flow.execute(str(TG), "101", "admin_product_add", {}, None)
        assert "доступно только владельцу" in transport.calls[-1].text
        await bot.session.close()

    asyncio.run(run())


def test_v11_bot_qr_is_generated_locally_for_owned_connection_only():
    from aiogram.methods import SendPhoto
    from test_dispatcher import FakeCommerce

    async def run():
        commerce, transport = FakeCommerce(), FakeSession()
        bot = Bot(TOKEN, session=transport)
        app = BotApplication(bot, commerce, BotSafety(frozenset({"700001"}), frozenset({"101"})))
        sid = commerce.subscriptions[0]["id"]
        await app.execute("101", "a1", "connection_qr", {"sid": sid}, None, "")
        photos = [call for call in transport.calls if isinstance(call, SendPhoto)]
        assert len(photos) == 1 and isinstance(photos[0].photo, BufferedInputFile)
        assert photos[0].photo.data.startswith(b"\x89PNG\r\n\x1a\n")
        assert photos[0].caption == "QR-код подключения."
        with pytest.raises(DomainError):
            await app.execute("101", "a1", "connection_qr", {"sid": "foreign-sub"}, None, "")
        assert len([call for call in transport.calls if isinstance(call, SendPhoto)]) == 1
        await bot.session.close()

    asyncio.run(run())


def test_v11_bot_inbox_uses_owned_common_notifications_and_cannot_mark_foreign_read(account_features_context):
    from sunrise_infrastructure.db import Notification
    from sunrise_infrastructure.notifications import NotificationService

    db, accounts, _, _ = account_features_context
    with db.transaction() as tx:
        NotificationService(db, accounts.settings).emit_in(
            tx, 101, "own-note", "test", "Своя заметка", "Свой текст", channel="inbox"
        )
        foreign = (
            NotificationService(db, accounts.settings)
            .emit_in(tx, 102, "foreign-note", "test", "Чужая заметка", "Чужой текст", channel="inbox")
            .id
        )

    async def run():
        transport = FakeSession()
        bot = Bot(TOKEN, session=transport)
        app = BotApplication(
            bot, Commerce(db), BotSafety(frozenset({"700001"}), frozenset({str(TG)})), accounts=accounts
        )
        await app.execute(str(TG), "101", "notifications", {}, None, "")
        assert "Своя заметка" in transport.calls[-1].text and "Чужая заметка" not in transport.calls[-1].text
        with pytest.raises(DomainError) as failure:
            await app.execute(str(TG), "101", "notification_read", {"id": foreign}, None, "")
        assert failure.value.status == 404
        await bot.session.close()

    asyncio.run(run())
    with db.transaction() as tx:
        assert tx.get(Notification, foreign).read_at is None


def test_v11_bot_broadcast_delegates_to_common_consent_and_durable_delivery(account_features_context):
    from sunrise_infrastructure.db import Job

    db, accounts, _, _ = account_features_context
    accounts.settings.admin_email_allowlist.append("old@account.test")
    accounts.settings.admin_telegram_allowlist.append(str(TG))
    accounts.settings.test_telegram_recipients = str(TG)
    commerce = Commerce(db)
    commerce.accounts = accounts
    documents = [
        {"document_type": kind, "version": "v11", "url": "https://bot.example.test/" + kind}
        for kind in ("offer", "privacy")
    ]
    commerce.legal.configure(None, documents, "Синтетические документы")
    commerce.accept_legal(101, documents, "telegram", "first_use")
    facade = BotAdmin(Admin(commerce, accounts.guard, accounts), accounts.settings)
    nodes = [{"type": "text", "text": "Разрешённая рассылка"}]
    preview = facade.bot_broadcast_preview(101, nodes)
    assert preview["recipient_count"] == 1 and preview["eligible_total"] == 1
    job = facade.bot_broadcast_create(101, nodes, "Проверка общей рассылки", confirmed=True)
    assert job["delivery"] == "durable_notifications" and job["state"] == "draft"
    with db.transaction() as tx:
        assert tx.get(Job, job["id"]).payload["account_ids"] == ["101"]


def test_v11_bot_dangerous_change_does_not_depend_on_web_totp_freshness(
    account_features_context, monkeypatch
):
    from datetime import timedelta

    import pyotp
    from sunrise_infrastructure.db import AdminAuthenticator, Product, now

    from tests.integration.test_v11_account_features import FOREIGN

    db, accounts, _, _ = account_features_context
    moment = now()
    monkeypatch.setattr("sunrise_infrastructure.admin_mfa.now", lambda: moment)
    seed = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
    accounts.settings.admin_totp_secrets = {"owner@account.test": seed}
    accounts.admin_mfa.verify(102, FOREIGN, pyotp.TOTP(seed).at(moment), "127.0.0.1")
    commerce = Commerce(db)
    commerce.accounts = accounts
    facade = BotAdmin(Admin(commerce, accounts.guard, accounts), accounts.settings)
    facade.bot_product_save(
        102, "standard", {"name": "Подтверждённое изменение"}, "Проверена свежая MFA", confirmed=True
    )
    with db.transaction() as tx:
        assert tx.get(Product, "standard").name == "Подтверждённое изменение"
        tx.get(AdminAuthenticator, 102).trust_verified_at = moment - timedelta(minutes=6)
    facade.bot_product_save(102, "standard", {"name": "Bot edit without fresh web TOTP"}, "Bot configuration edit", confirmed=True)
    with db.transaction() as tx:
        assert tx.get(Product, "standard").name == "Bot edit without fresh web TOTP"

def test_v11_bot_unknown_traffic_stays_unknown_and_observed_zero_is_real_zero():
    from sunrise_bot.adapter import traffic

    assert traffic(None) == "недоступен"
    assert traffic("0") == "0 Б"
    assert traffic("1073741824") == "1,0 ГиБ"
    assert traffic(True) == traffic("-1") == traffic("9223372036854775808") == "недоступен"


def test_v11_bot_campaign_uses_real_web_visits_without_changing_telegram_first_touch(
    account_features_context,
):
    from sunrise_infrastructure.campaigns import Campaigns

    db, accounts, _, _ = account_features_context
    accounts.settings.admin_email_allowlist.append("old@account.test")
    accounts.settings.admin_telegram_allowlist.append(str(TG))
    accounts.settings.bot_url = "https://t.me/SyntheticTestBot"
    accounts.settings.landing_url = "https://landing.example.test"
    commerce = Commerce(db)
    commerce.accounts = accounts
    facade = BotAdmin(Admin(commerce, accounts.guard, accounts), accounts.settings)
    campaign = facade.bot_utm_create(
        101,
        {"name": "Своя кампания", "source": "synthetic", "medium": "post", "campaign": "v11"},
        "Проверка веб-переходов",
        confirmed=True,
    )
    assert facade.bot_utm_list(101)["items"][0]["website_url"].startswith(
        "https://landing.example.test/?utm_id="
    )
    visits = Campaigns(commerce, accounts.settings)
    visits.visit(campaign["id"], "synthetic-browser-visitor")
    visits.visit(campaign["id"], "synthetic-browser-visitor")
    facade.record_utm_start(101, campaign["start_code"])
    statistics = facade.bot_utm_stats(101, campaign["id"])
    assert statistics["website_visits"] == 2 and statistics["website_unique_visitors"] == 1
    assert statistics["starts"] == 1 and statistics["unique_users"] == 1
    assert statistics["attribution_model"] == "canonical_account_first_touch"


def test_v11_bot_message_allows_admin_but_gift_and_backups_remain_owner_only(account_features_context):
    from sunrise_infrastructure.db import Notification

    db, accounts, _, _ = account_features_context
    accounts.settings.admin_email_allowlist.append("old@account.test")
    accounts.settings.admin_telegram_allowlist.append(str(TG))
    commerce = Commerce(db)
    commerce.accounts = accounts
    facade = BotAdmin(Admin(commerce, accounts.guard, accounts), accounts.settings)
    nodes = [
        {"type": "emoji", "id": "123456789012345678", "fallback": "☀️", "label": "Солнце"},
        {"type": "text", "text": " Сообщение поддержки"},
    ]
    result = facade.bot_message(101, 103, nodes, "v11-message", "Обращение пользователя", confirmed=True)
    assert result["state"] == "queued"
    with db.transaction() as tx:
        assert tx.get(Notification, result["notification_id"]).nodes == nodes
    with pytest.raises(DomainError) as failure:
        facade.bot_grant(101, 103, "standard", 7, "v11-gift", "Недопустимый подарок", confirmed=True)
    assert failure.value.code == "staff_permission_denied"
    with pytest.raises(DomainError) as failure:
        facade.bot_backups(101)
    assert failure.value.code == "staff_permission_denied"
    assert facade.bot_backups(102)["schedule"] == "16:00"


def test_v11_bot_owner_gift_and_premium_message_require_review_before_real_service_write(
    account_features_context, monkeypatch
):
    import pyotp
    from aiogram.types import MessageEntity
    from sunrise_infrastructure.db import Notification, Segment, now

    from tests.integration.test_v11_account_features import FOREIGN, OWNER_TG

    db, accounts, _, _ = account_features_context
    moment = now()
    monkeypatch.setattr("sunrise_infrastructure.admin_mfa.now", lambda: moment)
    seed = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
    accounts.settings.admin_totp_secrets = {"owner@account.test": seed}
    accounts.admin_mfa.verify(102, FOREIGN, pyotp.TOTP(seed).at(moment), "127.0.0.1")
    commerce = Commerce(db, local_test=True)
    commerce.accounts = accounts
    facade = BotAdmin(Admin(commerce, accounts.guard, accounts), accounts.settings)
    with db.transaction() as tx:
        paid = tx.get(Subscription, "v11-paid-sub")
        paid_snapshot = (paid.id, paid.account_id, paid.expires_at, paid.devices_limit, paid.version)

    async def run():
        transport = FakeSession()
        bot = Bot(TOKEN, session=transport)
        app = BotApplication(
            bot,
            commerce,
            BotSafety(frozenset({"700001"}), frozenset({str(OWNER_TG)})),
            accounts=accounts,
            admin_console=facade,
        )
        uid = str(OWNER_TG)

        def current_action(action):
            return next(
                app.actions[button.callback_data].data
                for row in transport.calls[-1].reply_markup.inline_keyboard
                for button in row
                if button.callback_data and app.actions[button.callback_data].action == action
            )

        await app.admin_flow.execute(uid, "102", "admin_user_gift", {"target_id": "101"}, None)
        product = current_action("admin_gift_product")
        await app.admin_flow.execute(uid, "102", "admin_gift_product", product, None)
        await app.admin_flow.message(uid, {"id": "102"}, message_update(OWNER_TG, "7").message)
        await app.admin_flow.message(
            uid, {"id": "102"}, message_update(OWNER_TG, "Синтетический подарок владельца").message
        )
        with db.transaction() as tx:
            assert len(list(tx.scalars(select(Subscription).where(Subscription.account_id == 101)))) == 1
        save = current_action("admin_save")
        await app.admin_flow.execute(uid, "102", "admin_save", save, None)
        await app.admin_flow.execute(uid, "102", "admin_save", save, None)
        with db.transaction() as tx:
            paid = tx.get(Subscription, "v11-paid-sub")
            assert (
                paid.id,
                paid.account_id,
                paid.expires_at,
                paid.devices_limit,
                paid.version,
            ) == paid_snapshot
            gifts = list(tx.scalars(select(Segment).where(Segment.kind == "owner_gift")))
            assert len(gifts) == 1 and gifts[0].paid_value == 0
            assert (gifts[0].ends_at - gifts[0].starts_at).days == 7

        await app.admin_flow.execute(uid, "102", "admin_user_message", {"target_id": "101"}, None)
        message = message_update(OWNER_TG, "☀️ Поддержка").message.model_copy(
            update={
                "entities": [
                    MessageEntity(
                        type="custom_emoji", offset=0, length=2, custom_emoji_id="123456789012345678"
                    )
                ]
            }
        )
        await app.admin_flow.message(uid, {"id": "102"}, message)
        await app.admin_flow.message(
            uid, {"id": "102"}, message_update(OWNER_TG, "Ответ синтетической поддержки").message
        )
        with db.transaction() as tx:
            assert not list(tx.scalars(select(Notification).where(Notification.kind == "support")))
        await app.admin_flow.execute(uid, "102", "admin_save", current_action("admin_save"), None)
        with db.transaction() as tx:
            notifications = list(tx.scalars(select(Notification).where(Notification.kind == "support")))
            assert len(notifications) == 1
            assert notifications[0].nodes[0]["type"] == "paragraph"
            emoji = notifications[0].nodes[0]["children"][0]
            assert emoji["type"] == "emoji"
            assert emoji["id"] == "123456789012345678"
        await bot.session.close()

    asyncio.run(run())
