import asyncio
import re
from decimal import Decimal
from html import unescape
from uuid import uuid4

import fakeredis
import pytest
from aiogram import Bot
from sunrise_bot.adapter import BotApplication
from sunrise_bot.safety import BotSafety
from sunrise_core.errors import DomainError
from sunrise_infrastructure.accounts import Accounts
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import Account, Agreement, Setting, now
from sunrise_infrastructure.security import RateGuard
from test_dispatcher import callback_update, find_button, message_update
from test_presentation import TOKEN, FakeSession

from tests.e2e.local_auth_server import SyntheticSettings
from tests.integration.test_panel_28_workflows import panel28_workflow  # noqa: F401 - fixture
from tests.isolated_database import isolated_database


def visible_message_text(session):
    return unescape(re.sub(r"<[^>]+>", "", session.calls[-1].text))


@pytest.mark.parametrize("open_existing", [False, True])
def test_sandbox_order_has_payment_button_without_waiting_for_worker(shared_backend, open_existing):
    async def run():
        from sqlalchemy import select
        from sunrise_infrastructure.db import ProviderOperation
        from sunrise_infrastructure.instance_config import InstanceConfig

        app, service, _, session, aid, telegram = shared_backend
        app.instance = InstanceConfig(support_url="https://t.me/synthetic_support")
        service.sandbox_payments = True

        class Provider:
            terminal = "synthetic-terminal"

            def create_payment(self, **body):
                return {
                    "order_id": body["order_id"],
                    "payment_id": "synthetic-payment",
                    "test": True,
                    "environment": "sandbox",
                    "pay_url": "https://pay.rollypay.io/synthetic-checkout",
                }

        app.payment_provider = Provider()
        quote = service.quote(aid, "standard", 30, "purchase")
        key = uuid4().hex
        if open_existing:
            order = service.create_order(aid, quote["id"], key, source="bot")
            action, data = "order", {"order_id": order["id"]}
        else:
            action, data = "checkout", {"quote_id": quote["id"], "key": key}
        await app.execute(str(telegram), str(aid), action, data, None, "")
        buttons = [b for row in session.calls[-1].reply_markup.inline_keyboard for b in row]
        pay = [b for b in buttons if b.url == "https://pay.rollypay.io/synthetic-checkout"]
        assert len(pay) == 1
        assert pay[0].text.endswith("Оплатить")
        assert all("Проверить статус" not in button.text for button in buttons)
        assert any("История платежей" in button.text for button in buttons)
        assert any("Поддержка по заказу" in button.text and button.url for button in buttons)
        assert "автоматически" in visible_message_text(session)
        order_id = service.create_order(aid, quote["id"], key, source="bot")["id"]
        await app.execute(str(telegram), str(aid), "order", {"order_id": order_id}, None, "")
        with service.db.transaction() as tx:
            operations = list(
                tx.scalars(select(ProviderOperation).where(ProviderOperation.order_id == order_id))
            )
            assert len(operations) == 1 and operations[0].state == "created"
        assert service.dashboard(aid)["payments"] == []

    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["unknown", "untrusted_url"])
def test_bot_payment_failure_preserves_order_without_retrying_invoice(shared_backend, outcome):
    async def run():
        from sqlalchemy import select
        from sunrise_infrastructure.db import ProviderOperation

        app, service, _, session, aid, telegram = shared_backend
        service.sandbox_payments = True

        class Provider:
            terminal = "synthetic-terminal"

            def create_payment(self, **body):
                if outcome == "unknown":
                    raise DomainError("provider_unavailable", "Unavailable", 503)
                return {
                    "order_id": body["order_id"],
                    "payment_id": "synthetic-payment",
                    "test": True,
                    "environment": "sandbox",
                    "pay_url": "https://evil.example/pay",
                }

        app.payment_provider = Provider()
        quote = service.quote(aid, "standard", 30, "purchase")
        key = uuid4().hex
        await app.execute(
            str(telegram), str(aid), "checkout", {"quote_id": quote["id"], "key": key}, None, ""
        )
        order_id = service.create_order(aid, quote["id"], key, source="bot")["id"]
        for _ in range(2):
            await app.execute(str(telegram), str(aid), "order", {"order_id": order_id}, None, "")
            buttons = [b for row in session.calls[-1].reply_markup.inline_keyboard for b in row]
            assert not any(b.url for b in buttons)
            assert all("Проверить статус" not in button.text for button in buttons)
            assert "обновите статус" not in visible_message_text(session).casefold()
        with service.db.transaction() as tx:
            operations = list(
                tx.scalars(select(ProviderOperation).where(ProviderOperation.order_id == order_id))
            )
            assert len(operations) == 1
            assert operations[0].state == ("outcome_unknown" if outcome == "unknown" else "review")
        assert service.order(aid, order_id)["status"] == "awaiting_payment"
        assert service.dashboard(aid)["payments"] == []

    asyncio.run(run())


def test_configured_test_panel_exposes_real_connection_in_bot(shared_backend):
    async def run():
        from sunrise_infrastructure.db import Subscription

        app, service, _, session, aid, telegram = shared_backend
        with service.db.transaction() as tx:
            sub = Subscription(
                devices_limit=4,
                account_id=aid,
                environment="sandbox",
                status="active",
                connection_url="https://connect.example.test/subscription/synthetic",
            )
            tx.add(sub)
            tx.flush()
            sid = sub.id
        service.panel_provider = object()
        assert service.catalog()["capabilities"]["provisioning"] == "test_panel"
        await app.execute(str(telegram), str(aid), "connect", {"sid": sid}, None, "")
        assert "https://connect.example.test/subscription/synthetic" in session.calls[-1].text
        assert "внешнее VPN-подключение не выполняется" not in session.calls[-1].text

    asyncio.run(run())


def test_static_menu_reads_edited_shared_emoji_without_restart(shared_backend):
    async def run():
        app, service, accounts, session, _, telegram = shared_backend
        from sqlalchemy import select
        from sunrise_infrastructure.db import Setting
        from sunrise_infrastructure.instance_config import InstanceConfig

        app = BotApplication(
            app.bot,
            service,
            app.safety,
            accounts=accounts,
            instance=InstanceConfig(telegram_menu_emoji_key="sunrise"),
        )

        with service.db.transaction() as tx:
            current = tx.scalar(select(Setting).where(Setting.key == "emoji_registry"))
            previous = dict(current.value) if current else {}
            if current:
                current.value = {
                    "sunrise": {
                        "type": "emoji",
                        "id": "9007199254740993",
                        "fallback": "🌅",
                        "label": "Рассвет",
                    }
                }
            else:
                tx.add(
                    Setting(
                        key="emoji_registry",
                        value={
                            "sunrise": {
                                "type": "emoji",
                                "id": "9007199254740993",
                                "fallback": "🌅",
                                "label": "Рассвет",
                            }
                        },
                    )
                )
        try:
            await app.dispatcher.feed_update(app.bot, message_update(uid=telegram, text="/start"))
            assert "🌅" in session.calls[-1].text
            with service.db.transaction() as tx:
                tx.scalar(select(Setting).where(Setting.key == "emoji_registry")).value = {
                    "sunrise": {"type": "emoji", "fallback": "🔆", "label": "Солнце"}
                }
            await app.dispatcher.feed_update(app.bot, message_update(uid=telegram, text="/start"))
            assert "🔆" in session.calls[-1].text
        finally:
            with service.db.transaction() as tx:
                tx.scalar(select(Setting).where(Setting.key == "emoji_registry")).value = previous

    asyncio.run(run())


@pytest.fixture
def shared_backend(tmp_path):
    with isolated_database("bot_shared_v11_test") as db:
        # Fresh synthetic identities for each test; no restored recipients/panel references.
        telegram = 800000000 + int(uuid4().hex[:7], 16)
        service = Commerce(db, local_test=True)
        with db.transaction() as tx:
            tx.add(
                Setting(
                    key="payment_methods",
                    value={
                        "version": 1,
                        "minimum_subscription_amount": "1.00",
                        "methods": [
                            {
                                "id": "sbp",
                                "label": "СБП",
                                "provider": "rollypay",
                                "provider_method": "sbp",
                                "discount_percent": "0",
                                "enabled": True,
                                "currencies": ["RUB"],
                                "mapping_contract": "synthetic-reviewed-contract",
                            }
                        ],
                    },
                )
            )
        account = service.resolve_telegram(telegram)
        service.legal.configure(
            None,
            [
                {"document_type": kind, "version": "bot-test-v1", "url": "https://example.test/bot-" + kind}
                for kind in ("offer", "privacy")
            ],
            "Синтетические документы тестового бота",
        )
        service.accept_legal(account["id"], service.legal.public()["documents"], "telegram", "first_use")
        session = FakeSession()
        settings = SyntheticSettings(
            _env_file=None,
            database_url=db.engine.url.render_as_string(hide_password=False),
            local_test=True,
            secret_key="synthetic-secret-key-" * 3,
            mail_dir=str(tmp_path),
        )
        accounts = Accounts(db, RateGuard(fakeredis.FakeRedis()), settings)
        service.accounts = accounts
        app = BotApplication(
            Bot(TOKEN, session=session),
            service,
            BotSafety(frozenset({"700001"}), frozenset({str(telegram)})),
            accounts=accounts,
        )
        yield app, service, accounts, session, int(account["id"]), telegram


def test_format_degradation_is_reported_to_shared_admin_status(shared_backend):
    async def run():
        app, service, _, session, _, telegram = shared_backend
        session.fail_html = True
        app.presenter.custom_emoji = True
        await app.dispatcher.feed_update(app.bot, message_update(uid=telegram))
        from sqlalchemy import select
        from sunrise_infrastructure.db import Setting

        with service.db.transaction() as tx:
            report = tx.scalar(select(Setting).where(Setting.key == "bot_capabilities"))
            assert report is not None
            assert report.value["custom_emoji"] is False
            assert report.value["rich_messages"] is False
            assert "Unicode" in report.value["reason"]
            assert report.value["environment"] == "local_test"

    asyncio.run(run())


def test_standalone_promo_is_persisted_then_used_by_shared_quote(shared_backend):
    async def run():
        app, service, _, session, aid, telegram = shared_backend
        from sunrise_infrastructure.db import Setting

        code = "BOT" + uuid4().hex[:10].upper()
        with service.db.transaction() as tx:
            row = tx.get(Setting, "discounts")
            previous = dict(row.value)
            row.value = previous | {
                "promos": previous.get("promos", [])
                + [
                    {
                        "code": code,
                        "percent": "20",
                        "enabled": True,
                        "audience": "first_purchase",
                        "per_account_limit": 1,
                    }
                ]
            }
        try:
            await app.dispatcher.feed_update(app.bot, message_update(uid=telegram))
            await app.dispatcher.feed_update(
                app.bot, callback_update(find_button(session, "Промокод"), uid=telegram)
            )
            await app.dispatcher.feed_update(app.bot, message_update(uid=telegram, text=code))
            assert service.active_promo(aid)["code"] == code
            assert "20%" in session.calls[-1].text
            assert service.dashboard(aid)["payments"] == []
            for label in ("Открыть каталог", "Стандартный", "30 дней", "СБП"):
                button = find_button(session, label)
                await app.dispatcher.feed_update(app.bot, callback_update(button, uid=telegram))
            assert "79,20" in session.calls[-1].text
            assert find_button(session, "Создать заказ")
            assert service.active_promo(aid)["code"] == code
            assert service.dashboard(aid)["payments"] == []
        finally:
            with service.db.transaction() as tx:
                tx.get(Setting, "discounts").value = previous

    asyncio.run(run())


def test_stale_promo_input_does_not_activate_or_create_quote(shared_backend):
    async def run():
        app, service, _, session, aid, telegram = shared_backend
        from sqlalchemy import func, select
        from sunrise_infrastructure.db import Quote

        await app.execute(
            str(telegram),
            str(aid),
            "promo",
            {},
            None,
            "",
        )
        app.promo_inputs[str(telegram)].expires = -1
        with service.db.transaction() as tx:
            before = tx.scalar(select(func.count()).select_from(Quote).where(Quote.account_id == aid))
        await app.dispatcher.feed_update(app.bot, message_update(uid=telegram, text="UNTRUSTED"))
        assert service.active_promo(aid) is None
        assert find_button(session, "Каталог")
        with service.db.transaction() as tx:
            assert tx.scalar(select(func.count()).select_from(Quote).where(Quote.account_id == aid)) == before

    asyncio.run(run())


def test_owner_edited_main_menu_content_is_visible_without_restart(shared_backend):
    async def run():
        app, service, _, session, _, telegram = shared_backend
        from sunrise_infrastructure.db import Setting

        with service.db.transaction() as tx:
            row = tx.get(Setting, "content:bot_menu")
            previous = dict(row.value) if row else {"nodes": []}
            value = {"nodes": [{"type": "heading", "children": [{"type": "text", "text": "Первое меню"}]}]}
            if row:
                row.value = value
            else:
                tx.add(Setting(key="content:bot_menu", value=value))
        try:
            await app.dispatcher.feed_update(app.bot, message_update(uid=telegram))
            assert "Первое меню" in session.calls[-1].text
            assert find_button(session, "Подписки")
            with service.db.transaction() as tx:
                tx.get(Setting, "content:bot_menu").value = {
                    "nodes": [
                        {"type": "paragraph", "children": [{"type": "text", "text": "Обновлённое меню"}]}
                    ]
                }
            await app.dispatcher.feed_update(app.bot, message_update(uid=telegram))
            assert "Обновлённое меню" in session.calls[-1].text
        finally:
            with service.db.transaction() as tx:
                tx.get(Setting, "content:bot_menu").value = previous

    asyncio.run(run())


def buy(service, aid):
    service.accept_legal(aid, service.legal.public()["documents"], "telegram", "first_use")
    quote = service.quote(aid, "standard", 30, "purchase")
    order = service.create_order(aid, quote["id"], uuid4().hex, source="bot")
    service.test_pay(aid, order["id"])
    return order, service.dashboard(aid)["subscriptions"][0]


def test_v6_real_first_use_records_versions_source_and_requires_url_reacceptance(shared_backend):
    async def run():
        _, service, _, _, _, _ = shared_backend
        telegram = 800000000 + int(uuid4().hex[:7], 16)
        aid = service.resolve_telegram(telegram)["id"]
        session = FakeSession()
        app = BotApplication(
            Bot(TOKEN, session=session), service, BotSafety(frozenset({"700001"}), frozenset({str(telegram)}))
        )
        await app.dispatcher.feed_update(app.bot, message_update(uid=telegram))
        buttons = [b for row in session.calls[-1].reply_markup.inline_keyboard for b in row]
        labels = [
            "Политика конфиденциальности",
            "Публичная оферта",
            "Принять",
        ]
        assert len(buttons) == 3
        assert all(
            button.text == label or button.text.endswith(" " + label)
            for button, label in zip(buttons, labels, strict=True)
        )
        documents = {
            document["document_type"]: document["url"] for document in service.legal_status(aid)["documents"]
        }
        assert buttons[0].url == documents["privacy"]
        assert buttons[1].url == documents["offer"]
        assert buttons[2].style == "success" and buttons[2].callback_data
        with pytest.raises(DomainError):
            service.quote(aid, "standard", 30, "purchase")
        await app.dispatcher.feed_update(
            app.bot, callback_update(find_button(session, "Принять"), uid=telegram)
        )
        accepted = service.legal_status(aid)
        assert accepted["accepted"]
        assert len(accepted["history"]) == 2
        assert all(
            event["source"] == "telegram" and event["context"] == "first_use" for event in accepted["history"]
        )
        _, sub = buy(service, aid)
        previous = service.legal.public()["documents"]
        updated = [dict(doc) | {"url": doc["url"] + "?revision=2"} for doc in previous]
        try:
            service.legal.configure(None, updated, "Синтетическая смена адреса документов")
            await app.dispatcher.feed_update(app.bot, message_update(uid=telegram))
            assert not service.legal_status(aid)["accepted"]
            await app.execute(str(telegram), aid, "connect", {"sid": sub["id"]}, None, "")
            assert "Локальный тестовый стенд" in session.calls[-1].text
            assert service.dashboard(aid)["subscriptions"][0]["id"] == sub["id"]
        finally:
            service.legal.configure(None, previous, "Восстановление синтетических документов")

    asyncio.run(run())


def test_bot_quote_matches_shared_pricing_and_payment_pending(shared_backend):
    async def run():
        app, service, _, session, aid, telegram = shared_backend
        await app.dispatcher.feed_update(app.bot, message_update(uid=telegram))
        for label in ("Подписки", "Добавить подписку", "Стандартный", "30 дней", "СБП"):
            await app.dispatcher.feed_update(
                app.bot, callback_update(find_button(session, label), uid=telegram)
            )
        text = session.calls[-1].text
        quote = service.quote(aid, "standard", 30, "purchase")
        assert quote["total"] == "99.00"
        assert "99,00 ₽" in text
        token = find_button(session, "Создать заказ")
        quote_id = app.actions[token].data["quote_id"]
        order_key = app.actions[token].data["key"]
        await app.dispatcher.feed_update(app.bot, callback_update(token, uid=telegram))
        await app.dispatcher.feed_update(app.bot, callback_update(token, uid=telegram))
        order_id = service.create_order(aid, quote_id, order_key, source="bot")["id"]
        service.test_pay(aid, order_id)
        await app.execute(str(telegram), str(aid), "order", {"order_id": order_id}, None, "")
        assert "Оплата получена" in session.calls[-1].text
        assert len(service.dashboard(aid)["payments"]) == 1
        assert len(service.dashboard(aid)["subscriptions"]) == 1
        for _ in range(20):
            service.run_jobs()
            if service.dashboard(aid)["subscriptions"][0]["status"] == "active":
                break
        await app.execute(str(telegram), str(aid), "order", {"order_id": order_id}, None, "")
        assert "Тестовая выдача завершена" in session.calls[-1].text

    asyncio.run(run())


def test_backend_stale_quote_rejected_and_bot_shows_error(shared_backend):
    async def run():
        app, service, _, session, aid, telegram = shared_backend
        _, sub = buy(service, aid)
        quote = service.quote(aid, "standard", 30, "renew", subscription_id=sub["id"])
        with service.db.transaction() as tx:
            from sunrise_infrastructure.db import Subscription

            tx.get(Subscription, sub["id"]).version += 1
        token = app.button(
            str(telegram), "Создать заказ", "checkout", quote_id=quote["id"], key=uuid4().hex
        ).callback_data
        await app.dispatcher.feed_update(app.bot, callback_update(token, uid=telegram))
        assert "Действие не выполнено" in session.calls[-1].text
        assert len(service.dashboard(aid)["payments"]) == 1

    asyncio.run(run())


def test_real_device_revoke_and_off_agreement_cancellation(shared_backend, panel28_workflow):  # noqa: F811 - imported fixture
    async def run():
        from sqlalchemy import select
        from sunrise_infrastructure.db import DeviceUnlink

        panel_db, _, panel_service, events, operations, _, sid, _ = panel28_workflow
        events.reconcile_owned(sid, initial=True)
        panel_session = FakeSession()
        panel_telegram = 70000000000000001
        panel_app = BotApplication(
            Bot(TOKEN, session=panel_session),
            panel_service,
            BotSafety(frozenset({"700001"}), frozenset({str(panel_telegram)})),
            customer_operations=operations,
        )
        await panel_app.execute(str(panel_telegram), "301", "devices", {"sid": sid}, None, "")
        for label in ("Отключить: Тестовый телефон", "Подтвердить отключение"):
            await panel_app.dispatcher.feed_update(
                panel_app.bot, callback_update(find_button(panel_session, label), uid=panel_telegram)
            )
        # Queued is not a completed unlink; only verified worker outcome updates devices/counters.
        assert len([d for d in operations.inventory("301", sid)["devices"] if not d["revoked"]]) == 2
        assert panel_service.provisioning.run_jobs() == 1
        assert len([d for d in operations.inventory("301", sid)["devices"] if not d["revoked"]]) == 1
        with panel_db.transaction() as tx:
            assert len(list(tx.scalars(select(DeviceUnlink)))) == 1
        app, service, _, session, aid, telegram = shared_backend
        _, sub = buy(service, aid)
        with service.db.transaction() as tx:
            tx.add(
                Agreement(
                    account_id=aid,
                    subscription_id=sub["id"],
                    provider_id="synthetic-" + uuid4().hex,
                    state="enabled",
                    consent_amount=Decimal("99.00"),
                    currency="RUB",
                )
            )
        await app.dispatcher.feed_update(app.bot, message_update(uid=telegram))
        await app.dispatcher.feed_update(
            app.bot, callback_update(find_button(session, "Подписки"), uid=telegram)
        )
        for label in ("Отключить автопродление", "Подтвердить отмену"):
            await app.dispatcher.feed_update(
                app.bot, callback_update(find_button(session, label), uid=telegram)
            )
        assert service.dashboard(aid)["subscriptions"][0]["recurring"]["state"] == "stop_pending"
        assert "Запрос отмены" in session.calls[-1].text

    asyncio.run(run())


def test_real_link_exact_pair_preserves_both_subscriptions_and_rejects_replay(shared_backend):
    async def run():
        app, service, accounts, session, aid, telegram = shared_backend
        buy(service, aid)
        with service.db.transaction() as tx:
            email = Account(email=uuid4().hex + "@example.test", email_verified=True)
            tx.add(email)
            tx.flush()
            email_id = email.id
        buy(service, email_id)
        challenge = accounts.request_link(email_id, str(telegram), now())
        await app.dispatcher.feed_update(
            app.bot, message_update(uid=telegram, text="/link " + challenge["proof"])
        )
        await app.dispatcher.feed_update(
            app.bot, callback_update(find_button(session, "Объединить аккаунты"), uid=telegram)
        )
        assert "Аккаунты связаны" in session.calls[-1].text
        canonical = service.resolve_telegram(telegram)
        assert canonical["id"] == str(email_id)
        assert len(service.dashboard(email_id)["subscriptions"]) == 2
        with pytest.raises(DomainError):
            accounts.confirm_telegram_link(email_id, challenge["proof"])

    asyncio.run(run())


def test_bot_bonus_redemption_is_calculated_by_shared_service(shared_backend):
    async def run():
        app, service, _, session, aid, telegram = shared_backend
        with service.db.transaction() as tx:
            tx.get(Account, aid).bonus_balance = Decimal("100.00")
        await app.dispatcher.feed_update(app.bot, message_update(uid=telegram))
        for label in (
            "Подписки",
            "Добавить подписку",
            "Стандартный",
            "30 дней",
            "Использовать допустимые бонусы",
            "СБП",
        ):
            await app.dispatcher.feed_update(
                app.bot, callback_update(find_button(session, label), uid=telegram)
            )
        assert "Бонусы со счёта: 49,50 ₽" in visible_message_text(session)
        assert "Итого: 49,50 ₽" in visible_message_text(session)
        direct = service.quote_with_max_bonus(aid, product_id="standard", period_days=30, action="purchase")
        assert direct["total"] == "49.50"

    asyncio.run(run())


def test_bot_cash_only_checkout_uses_shared_ledger_without_external_payment(
    shared_backend, tmp_path, monkeypatch
):
    async def run():
        import json

        import pyotp
        from sqlalchemy import func, select
        from sunrise_infrastructure.admin import Admin
        from sunrise_infrastructure.db import CreditLedger, Order, Payment
        from sunrise_infrastructure.security import digest

        app, service, accounts, session, aid, telegram = shared_backend
        email = "cash-owner@example.test"
        accounts.settings.email_allowlist = [email]
        accounts.settings.admin_email_allowlist = [email]
        accounts.settings.owner_email = email
        accounts.settings.owner_id = str(telegram)
        with service.db.transaction() as tx:
            owner = tx.get(Account, aid)
            owner.email, owner.email_verified = email, True
        moment = now()
        monkeypatch.setattr("sunrise_infrastructure.admin_mfa.now", lambda: moment)
        monkeypatch.setattr("sunrise_infrastructure.accounts.now", lambda: moment)
        challenge = accounts.request_code(email, "127.0.0.1")
        code = json.loads((tmp_path / (challenge["challenge_id"] + ".json")).read_text())["code"]
        token, _ = accounts.verify_code(
            challenge["challenge_id"],
            code,
            "127.0.0.1",
            legal_accepted=True,
            documents=service.legal.public()["documents"],
        )
        enrollment = accounts.admin_mfa.begin(aid, digest(token))
        accounts.admin_mfa.verify(
            aid, digest(token), pyotp.TOTP(enrollment["secret"]).at(moment), "127.0.0.1", enrollment=True
        )
        Admin(service, RateGuard(fakeredis.FakeRedis())).adjust_credit(
            aid,
            aid,
            "99.00",
            "RUB",
            "owner_adjustment",
            "bot-cash-fixture",
            "Синтетический остаток для проверки общего расчёта",
        )
        await app.dispatcher.feed_update(app.bot, message_update(uid=telegram))
        for label in (
            "Подписки",
            "Добавить подписку",
            "Стандартный",
            "30 дней",
            "Использовать денежный остаток",
            "СБП",
        ):
            await app.dispatcher.feed_update(
                app.bot, callback_update(find_button(session, label), uid=telegram)
            )
        assert "Денежный остаток: 99,00 ₽" in visible_message_text(session)
        assert "Итого: 0,00 ₽" in visible_message_text(session)
        confirm = find_button(session, "Создать заказ")
        await app.dispatcher.feed_update(app.bot, callback_update(confirm, uid=telegram))
        await app.dispatcher.feed_update(app.bot, callback_update(confirm, uid=telegram))
        with service.db.transaction() as tx:
            assert tx.get(Account, aid).credit_balance == 0
            assert tx.get(Account, aid).credit_reserved == 0
            assert tx.scalar(select(func.count()).select_from(Payment).where(Payment.account_id == aid)) == 0
            assert tx.scalar(select(func.count()).select_from(Order).where(Order.account_id == aid)) == 1
            assert (
                tx.scalar(
                    select(func.count())
                    .select_from(CreditLedger)
                    .where(CreditLedger.account_id == aid, CreditLedger.amount < 0)
                )
                == 1
            )
        assert len(service.dashboard(aid)["subscriptions"]) == 1

    asyncio.run(run())
