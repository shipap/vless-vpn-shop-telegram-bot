import asyncio

from sunrise_infrastructure.admin import Admin
from sunrise_infrastructure.bot_admin import BotAdmin
from test_dispatcher import callback_update, find_button, message_update
from test_shared_backend import shared_backend  # noqa: F401 -- imported pytest fixture


def bind_console(app, service, accounts, telegram, *, verify_email=True):
    if verify_email:
        import json
        from pathlib import Path

        from sunrise_infrastructure.db import Account

        email = "bot-read-admin@example.test"
        accounts.settings.email_allowlist = [email]
        accounts.settings.admin_email_allowlist = [email]
        with service.db.transaction() as tx:
            actor = tx.query(Account).filter(Account.telegram_id == telegram).one()
            actor.email, actor.email_verified = email, False
        challenge = accounts.request_code(email, "127.0.0.1")
        code = json.loads(
            (Path(accounts.settings.mail_dir) / (challenge["challenge_id"] + ".json")).read_text()
        )["code"]
        accounts.verify_code(
            challenge["challenge_id"],
            code,
            "127.0.0.1",
            legal_accepted=True,
            documents=service.legal.public()["documents"],
        )
    accounts.settings.admin_telegram_allowlist = [str(telegram)]
    accounts.settings.bot_url = "https://t.me/SyntheticTestBot"
    app.admin = Admin(service, None, accounts=accounts)
    app.admin_console = BotAdmin(
        app.admin, accounts.settings, telegram_recipient_allowlist=app.safety.recipient_ids
    )
    return app.admin_console


def test_shared_admin_menu_statistics_and_calendar_survive_real_dispatcher(shared_backend):  # noqa: F811 -- fixture import
    async def run():
        app, service, accounts, session, _, telegram = shared_backend
        bind_console(app, service, accounts, telegram)
        await app.dispatcher.feed_update(app.bot, message_update(uid=telegram))
        await app.dispatcher.feed_update(
            app.bot, callback_update(find_button(session, "Админ-панель"), uid=telegram)
        )
        for label in (
            "Статистика",
            "Рассылки",
            "Промокоды",
            "Пользователи",
            "UTM-ссылки",
        ):
            find_button(session, label)
        actions = {
            app.actions[button.callback_data].action
            for row in session.calls[-1].reply_markup.inline_keyboard
            for button in row
        }
        assert not {"admin_products", "admin_settings"} & actions
        await app.dispatcher.feed_update(
            app.bot, callback_update(find_button(session, "Статистика"), uid=telegram)
        )
        assert "Статистика продаж" in session.calls[-1].text
        await app.dispatcher.feed_update(
            app.bot, callback_update(find_button(session, "Указать свой период"), uid=telegram)
        )
        for day in ("01", "08"):
            candidates = [
                button.callback_data
                for row in session.calls[-1].reply_markup.inline_keyboard
                for button in row
                if app.actions[button.callback_data].action == "admin_calendar_update"
                and app.actions[button.callback_data].data.get("operation") == "select_day"
                and app.actions[button.callback_data].data.get("value", "").endswith("-" + day)
            ]
            assert len(candidates) == 1
            await app.dispatcher.feed_update(app.bot, callback_update(candidates[0], uid=telegram))
        selected = [
            button
            for row in session.calls[-1].reply_markup.inline_keyboard
            for button in row
            if app.actions[button.callback_data].data.get("operation") == "select_day"
            and button.style == "success"
        ]
        assert len(selected) == 8
        accounts.settings.admin_telegram_allowlist = []
        await app.dispatcher.feed_update(app.bot, callback_update(selected[0].callback_data, uid=telegram))
        assert "404" in session.calls[-1].text

    asyncio.run(run())


def test_telegram_only_admin_opens_menu_with_current_canonical_account_without_email(shared_backend):  # noqa: F811
    async def run():
        app, service, accounts, session, _, telegram = shared_backend
        bind_console(app, service, accounts, telegram, verify_email=False)
        await app.dispatcher.feed_update(app.bot, message_update(uid=telegram))
        await app.dispatcher.feed_update(
            app.bot, callback_update(find_button(session, "Админ-панель"), uid=telegram)
        )
        find_button(session, "Статистика")
        accounts.settings.admin_telegram_allowlist = []
        await app.dispatcher.feed_update(
            app.bot, callback_update(find_button(session, "Статистика"), uid=telegram)
        )
        assert "404" in session.calls[-1].text

    asyncio.run(run())


def test_shared_utm_start_records_repeat_entry_without_fake_customer_data(shared_backend):  # noqa: F811 -- fixture import
    async def run():
        app, service, accounts, session, aid, telegram = shared_backend
        console = bind_console(app, service, accounts, telegram)
        link = console.bot_utm_create(
            str(aid),
            {
                "name": "Синтетическая кампания",
                "source": "synthetic",
                "medium": "telegram",
                "campaign": "local",
            },
            "Локальная проверка источника",
            confirmed=True,
        )
        start_code = link["url"].split("start=", 1)[1]
        for _ in range(2):
            await app.dispatcher.feed_update(
                app.bot, message_update(uid=telegram, text="/start " + start_code)
            )
        stats = console.bot_utm_stats(str(aid), link["id"])
        assert stats["starts"] == 2
        assert stats["unique_users"] == 1
        assert session.calls[-1].chat_id == str(telegram)

    asyncio.run(run())


def test_v9_owner_alert_warning_flow_uses_durable_shared_state_and_preserves_paid_access(
    shared_backend,  # noqa: F811 -- imported pytest fixture
    tmp_path,
    monkeypatch,
):
    async def run():
        import json

        import pyotp
        from sqlalchemy import select
        from sunrise_core.errors import DomainError
        from sunrise_infrastructure.db import Account, Device, Job, now
        from sunrise_infrastructure.security import digest
        from test_shared_backend import buy

        app, service, accounts, session, aid, telegram = shared_backend
        accounts.settings.owner_id = str(telegram)
        accounts.settings.admin_email_allowlist = ["owner-alert@example.test"]
        with service.db.transaction() as tx:
            actor = tx.get(Account, aid)
            actor.email = "owner-alert@example.test"
            actor.email_verified = True
        console = bind_console(app, service, accounts, telegram, verify_email=False)
        _, sub = buy(service, aid)
        moment = now()
        monkeypatch.setattr("sunrise_infrastructure.admin_mfa.now", lambda: moment)
        accounts.settings.email_allowlist = ["owner-alert@example.test"]
        accounts.settings.mail_dir = str(tmp_path)
        seed = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
        accounts.settings.admin_totp_secrets = {"owner-alert@example.test": seed}
        challenge = accounts.request_code("owner-alert@example.test", "127.0.0.1")
        code = json.loads((tmp_path / (challenge["challenge_id"] + ".json")).read_text())["code"]
        cookie, _ = accounts.verify_code(
            challenge["challenge_id"],
            code,
            "127.0.0.1",
            legal_accepted=True,
            documents=service.legal.public()["documents"],
        )
        accounts.admin_mfa.verify(aid, digest(cookie), pyotp.TOTP(seed).at(moment), "127.0.0.1")
        with service.db.transaction() as tx:
            device = Device(subscription_id=sub["id"], name="Синтетическое устройство")
            tx.add(device)
            tx.flush()
            did = device.id
        service.revoke_device(aid, sub["id"], did)
        service.revoke_device(aid, sub["id"], did)
        assert len(console.pending_owner_alerts(str(aid))["items"]) == 1
        assert await app.deliver_owner_alerts() == 1
        assert await app.deliver_owner_alerts() == 0
        assert "Устройство отвязано" in session.calls[-1].text
        for number in (1, 2):
            await app.execute(
                str(telegram),
                str(aid),
                "admin_user_moderate",
                {"target_id": str(aid), "moderation_action": "warn"},
                None,
                "",
            )
            await app.dispatcher.feed_update(
                app.bot, message_update(uid=telegram, text=f"Проверка предупреждения номер {number}")
            )
            confirm = find_button(session, "Сохранить")
            await app.dispatcher.feed_update(app.bot, callback_update(confirm, uid=telegram))
            assert (
                "Отвязка устройств: заблокирована" if number == 2 else "Отвязка устройств: разрешена"
            ) in session.calls[-1].text
            await app.dispatcher.feed_update(app.bot, callback_update(confirm, uid=telegram))
        with service.db.transaction() as tx:
            account = tx.get(Account, aid)
            assert (
                account.unlink_blocked and account.unlink_warnings == 0 and account.unlink_warnings_total == 2
            )
            assert (
                tx.scalar(select(Job.state).where(Job.kind == "owner_device_unlink", Job.account_id == aid))
                == "done"
            )
        assert service.dashboard(aid)["subscriptions"][0]["expires_at"] == sub["expires_at"]
        assert service.quote(aid, "standard", 30, "renew", subscription_id=sub["id"])["total"]
        try:
            service.revoke_device(aid, sub["id"], did)
        except DomainError as exc:
            assert exc.code == "device_unlink_blocked"

    asyncio.run(run())
