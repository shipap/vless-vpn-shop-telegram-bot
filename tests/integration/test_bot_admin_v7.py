import json
import os
from datetime import UTC, datetime
from decimal import Decimal as D
from types import SimpleNamespace
from uuid import uuid4

import fakeredis
import pyotp
import pytest
from sqlalchemy import select
from sunrise_core.errors import DomainError
from sunrise_infrastructure.accounts import Accounts
from sunrise_infrastructure.admin import Admin
from sunrise_infrastructure.bot_admin import BotAdmin
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import (
    Account,
    Agreement,
    Alias,
    Audit,
    Database,
    Job,
    Ledger,
    Order,
    Payment,
    Product,
    ProviderOperation,
    Quote,
    Setting,
    Subscription,
)
from sunrise_infrastructure.security import RateGuard

from tests.e2e.local_auth_server import SyntheticSettings
from tests.isolated_database import isolated_database
from tests.legal_fixture import accept_test_legal


@pytest.fixture
def bot_admin_v7(tmp_path, monkeypatch):
    from sunrise_infrastructure.db import now
    from sunrise_infrastructure.security import digest

    with isolated_database("bot_admin_v11_test") as db:
        moment = now()
        monkeypatch.setattr("sunrise_infrastructure.admin_mfa.now", lambda: moment)
        monkeypatch.setattr("sunrise_infrastructure.accounts.now", lambda: moment)
        email = f"{uuid4()}@example.test"
        telegram = 9000000000000000 + int(uuid4().hex[:8], 16)
        aid = 80000000000000000 + int(uuid4().hex[:8], 16)
        did = aid + 1
        config = SyntheticSettings(
            _env_file=None,
            database_url=db.engine.url.render_as_string(hide_password=False),
            secret_key="bot-admin-v11-isolated-secret-" * 3,
            admin_email_allowlist=[email],
            email_allowlist=[email],
            local_test=True,
            owner_id=str(telegram),
            owner_email=email,
            test_telegram_recipients=str(telegram),
            mail_dir=str(tmp_path),
            bot_url="https://t.me/synthetic_test_bot",
        )
        guard = RateGuard(fakeredis.FakeRedis())
        commerce = Commerce(db, True, notification_allowlist=[email])
        accounts = Accounts(db, guard, config)
        commerce.accounts = accounts
        with db.transaction() as s:
            s.add_all(
                [
                    Account(id=aid, email=email, email_verified=True, telegram_id=telegram),
                    Account(id=did, email=f"{uuid4()}@example.test"),
                ]
            )
        documents = [
            {"document_type": kind, "version": "v11", "url": "https://bot.example.test/" + kind}
            for kind in ("offer", "privacy")
        ]
        commerce.legal.configure(None, documents, "Синтетические документы владельца")
        challenge = accounts.request_code(email, "127.0.0.1")
        code = json.loads((tmp_path / (challenge["challenge_id"] + ".json")).read_text())["code"]
        token, _ = accounts.verify_code(
            challenge["challenge_id"], code, "127.0.0.1", legal_accepted=True, documents=documents
        )
        enrollment = accounts.admin_mfa.begin(aid, digest(token), ip="127.0.0.1")
        accounts.admin_mfa.verify(
            aid, digest(token), pyotp.TOTP(enrollment["secret"]).at(moment), "127.0.0.1", enrollment=True
        )
        yield (
            BotAdmin(Admin(commerce, guard, accounts), config, telegram_recipient_allowlist=[str(telegram)]),
            aid,
            did,
        )


def payment_fixture(facade, aid, amount, stamp, fee=None, method=None, environment="live", action="purchase"):
    with facade.db.transaction() as s:
        p = Product(
            included_devices=4,
            currency="RUB",
            id=str(uuid4()),
            code=uuid4().hex,
            name="Тестовый тариф",
            tier_level=1,
            price=D(amount),
        )
        s.add(p)
        s.flush()
        product_id = p.id
    accept_test_legal(facade.admin.commerce, aid)
    quote = facade.admin.commerce.quote(aid, product_id, 30, action)
    order = facade.admin.commerce.create_order(aid, quote["id"], uuid4().hex)
    assert D(order["total"]) == D(amount)
    with facade.db.transaction() as s:
        o = s.get(Order, order["id"])
        o.status, o.environment = "paid", environment
        pay = Payment(
            provider="rollypay",
            provider_id=uuid4().hex,
            environment=environment,
            order_id=o.id,
            account_id=aid,
            amount=D(amount),
            currency="RUB",
            fee=D(fee) if fee is not None else None,
            fee_currency="RUB" if fee is not None else None,
            paid_at=stamp,
            status="paid",
        )
        s.add(pay)
        if method:
            s.add(ProviderOperation(order_id=o.id, terminal="synthetic", request={"payment_method": method}))
        s.flush()
        return pay.id


def test_bot_admin_v7_denies_every_sensitive_read_and_requires_confirmation(bot_admin_v7):
    f, aid, did = bot_admin_v7
    for method, args in [
        (f.bot_products, ()),
        (f.bot_settings, ()),
        (f.bot_statistics, ()),
        (f.bot_users_search, ("x",)),
        (f.bot_utm_list, ()),
        (f.bot_broadcast_list, ()),
    ]:
        with pytest.raises(DomainError) as e:
            method(did, *args)
        assert e.value.status == 404
    with pytest.raises(DomainError):
        f.bot_settings_update(aid, "recurring", {"enabled": False}, "Проверка настройки")


def test_bot_admin_v7_statistics_dates_unknown_actual_and_estimated_fees(bot_admin_v7):
    f, aid, _ = bot_admin_v7
    with f.db.transaction() as s:
        previous = s.get(Setting, "accounting_fee_estimates")
        old = previous.value if previous else None
    try:
        f.bot_settings_update(
            aid,
            "fee_estimates",
            {"sbp": {"percent": "10", "fixed": "0", "currency": "RUB"}},
            "Оценка только учёта",
            confirmed=True,
        )
        baseline = f.bot_statistics(aid, "2031-01-02", "2031-01-02")
        before = next((row for row in baseline["period_money"] if row["currency"] == "RUB"), {})
        # 21:00 UTC starts the next Moscow day; both ends of selected dates included.
        payment_fixture(f, aid, "100", datetime(2031, 1, 1, 21, 0, tzinfo=UTC), "7", "sbp")
        payment_fixture(f, aid, "200", datetime(2031, 1, 2, 20, 59, tzinfo=UTC), None, "sbp")
        payment_fixture(f, aid, "900", datetime(2031, 1, 2, 21, 0, tzinfo=UTC), None, None)
        payment_fixture(f, aid, "999", datetime(2031, 1, 2, 12, 0, tzinfo=UTC), "0", "sbp", "local_test")
        r = f.bot_statistics(aid, "2031-01-02", "2031-01-02")
        rub = r["period_money"][0]
        assert D(rub["gross"]) - D(before.get("gross", "0")) == 300
        assert D(rub["actual_fee"]) - D(before.get("actual_fee", "0")) == 7
        assert D(rub["actual_net"]) - D(before.get("actual_net") or "0") == 93
        assert rub["unknown_fee_count"] - before.get("unknown_fee_count", 0) == 1
        assert D(rub["estimated_fee"]) - D(before.get("estimated_fee", "0")) == 20
        assert D(rub["estimated_net"]) - D(before.get("estimated_net", "0")) == 180
        assert r["purchase_count"] - baseline["purchase_count"] == 3
        before_known = sum(row["count"] for row in baseline["payment_methods"])
        before_sbp = next((row["count"] for row in baseline["payment_methods"] if row["method"] == "sbp"), 0)
        sbp = next(row for row in r["payment_methods"] if row["method"] == "sbp")
        assert sbp["count"] == before_sbp + 2
        assert sbp["denominator"] == before_known + 2
        assert D(sbp["percent"]) == (D(before_sbp + 2) * 100 / D(before_known + 2)).quantize(D(".01"))
    finally:
        with f.db.transaction() as s:
            row = s.get(Setting, "accounting_fee_estimates")
            if old is None:
                s.delete(row)
            else:
                row.value = old


def test_bot_admin_v7_exact_user_search_and_safe_paginated_detail(bot_admin_v7):
    f, aid, _ = bot_admin_v7
    with f.db.transaction() as s:
        email = s.get(Account, aid).email
    assert f.bot_users_search(aid, email)["items"][0]["id"] == str(aid)
    assert f.bot_users_search(aid, email[:8])["total"] == 0
    detail = f.bot_user_detail(aid, aid, limit=1)
    assert detail["subscriptions"]["page_size"] == 1
    assert "password_hash" not in str(detail) and "connection_url" not in str(detail)


def test_bot_admin_v7_catalog_create_edit_shared_intro_and_audit(bot_admin_v7):
    f, aid, _ = bot_admin_v7
    p = f.bot_product_save(
        aid,
        None,
        {
            "code": uuid4().hex,
            "name": "Новый тариф",
            "description": "Описание",
            "price": "127.00",
            "tier_level": 3,
            "included_devices": 4,
            "currency": "RUB",
        },
        "Добавление тарифа",
        confirmed=True,
    )
    f.bot_product_save(
        aid, p["id"], {"name": "Переименован", "price": "128.00"}, "Правка тарифа", confirmed=True
    )
    with f.db.transaction() as s:
        assert s.get(Product, p["id"]).price == 128
        assert s.scalar(select(Audit).where(Audit.actor_id == aid, Audit.action == "catalog_create"))
    f.bot_catalog_intro_save(aid, "Общий каталог", "Описание каталога", confirmed=True)
    assert f.admin.public_content()["catalog_description"] == "Общий каталог"


def test_bot_admin_v7_utm_repeat_start_first_touch_and_invalid_code(bot_admin_v7):
    f, aid, did = bot_admin_v7
    link = f.bot_utm_create(
        aid,
        {"name": "Кампания", "source": "telegram", "medium": "post", "campaign": uuid4().hex},
        "Создание ссылки",
        confirmed=True,
    )
    assert link["start_code"].startswith("utm_") and len(link["start_code"]) <= 64
    assert f.record_utm_start(did, link["start_code"]) is True
    assert f.record_utm_start(did, link["start_code"]) is True
    assert f.record_utm_start(did, "utm_fake") is False
    stats = f.bot_utm_stats(aid, link["id"])
    assert stats["starts"] == 2 and stats["unique_users"] == 1
    assert stats["paid_orders"] == 0


def test_bot_admin_v7_broadcast_draft_is_durable_stopped_and_has_no_caller_allowlist(bot_admin_v7):
    f, aid, _ = bot_admin_v7
    nodes = [
        {"type": "emoji", "id": "123456789012345678", "fallback": "☀️", "label": "Солнце"},
        {"type": "text", "text": "Тест"},
    ]
    preview = f.bot_broadcast_preview(aid, nodes)
    assert preview["recipient_count"] == 1
    job = f.bot_broadcast_create(aid, nodes, "Синтетический черновик", confirmed=True)
    assert job["state"] == "draft"
    with f.db.transaction() as s:
        assert s.get(Job, job["id"]).payload["nodes"] == nodes
    assert (
        f.bot_broadcast_start(aid, job["id"], "Подтверждение запуска", confirmed=True)["state"] == "pending"
    )
    assert f.bot_broadcast_stop(aid, job["id"], "Остановка рассылки", confirmed=True)["state"] == "stopped"
    assert f.bot_broadcast_detail(aid, job["id"])["delivered_count"] == 0


def test_bot_admin_v7_referral_rewards_net_reversal_not_current_balance(bot_admin_v7):
    f, aid, did = bot_admin_v7
    payment_id = uuid4().hex
    with f.db.transaction() as s:
        s.get(Account, did).referrer_id = aid
        s.add_all(
            [
                Ledger(account_id=aid, operation="referral_payment:" + payment_id, amount=D("15")),
                Ledger(account_id=aid, operation="reversal:" + payment_id + ":refund", amount=D("-3")),
                Ledger(account_id=aid, operation="admin:" + uuid4().hex, amount=D("999")),
            ]
        )
    r = f.bot_referrals(aid, "rewards")
    rows = list(r["items"])
    for page in range(2, (r["total"] + r["page_size"] - 1) // r["page_size"] + 1):
        rows.extend(f.bot_referrals(aid, "rewards", page=page)["items"])
    row = next(x for x in rows if x["account_id"] == str(aid))
    assert D(row["rewards"]) == 12 and row["invited_users"] == 1
    assert D(f.admin.commerce.referral_info(aid)["rewarded_amount"]) == 12


def test_bot_admin_v7_broadcast_worker_never_calls_smtp_and_preserves_ast(bot_admin_v7, tmp_path):
    from sunrise_infrastructure.broadcasts import BroadcastService
    from sunrise_infrastructure.db import Notification, NotificationDelivery

    f, aid, _ = bot_admin_v7
    nodes = [{"type": "emoji", "id": "123456789012345678", "fallback": "☀️", "label": "Солнце"}]
    job = f.bot_broadcast_create(aid, nodes, "Синтетический черновик", confirmed=True)
    f.bot_broadcast_start(aid, job["id"], "Локальная обработка", confirmed=True)

    class ForbiddenSMTP:
        def deliver(self, *args):
            raise AssertionError("Unexpected network transport")

    f.admin.commerce.mail_transport = ForbiddenSMTP()
    f.admin.commerce.notification_dir = str(tmp_path)
    before_files = set(tmp_path.glob("*.json"))
    f.admin.commerce.run_jobs(limit=1)
    with f.db.transaction() as s:
        assert s.get(Job, job["id"]).state == "pending"
    assert set(tmp_path.glob("*.json")) == before_files
    assert BroadcastService(f.admin.commerce, f.settings).expand_pending(limit=1) == 1
    with f.db.transaction() as s:
        assert s.get(Job, job["id"]).state == "done"
        item = s.scalar(select(Notification).where(Notification.event_key == "broadcast:" + job["id"]))
        assert item.nodes == nodes
        deliveries = list(
            s.scalars(select(NotificationDelivery).where(NotificationDelivery.notification_id == item.id))
        )
        assert (
            len(deliveries) == 1 and deliveries[0].channel == "telegram" and deliveries[0].state == "pending"
        )
    assert f.bot_broadcast_detail(aid, job["id"])["delivered_count"] == 0


def test_bot_admin_v7_catalog_intro_preserves_premium_ast(bot_admin_v7):
    f, aid, _ = bot_admin_v7
    nodes = [
        {"type": "emoji", "id": "123456789012345678", "fallback": "☀️", "label": "Солнце"},
        {"type": "text", "text": " Каталог"},
    ]
    result = f.bot_catalog_intro_save(aid, nodes, "Общий форматированный каталог", confirmed=True)
    assert result["catalog_description_content"] == nodes
    assert result["catalog_description"] == "☀️ Каталог"


def test_bot_admin_v7_fee_cross_currency_actual_wins_and_unknown_net_is_null(bot_admin_v7):
    f, aid, _ = bot_admin_v7
    p = SimpleNamespace(
        id="synthetic", currency="RUB", amount=D(100), fee=D("1"), fee_currency="USDT", net_usdt=None
    )
    op = SimpleNamespace(request={"payment_method": "sbp"})
    row = f._money(
        [(p, None, None, op)], {}, {"sbp": {"percent": "10", "fixed": "0", "currency": "RUB"}}, {}
    )[0]
    assert row["actual_fees_by_currency"] == {"USDT": "1"}
    assert row["estimated_count"] == 0 and row["unknown_fee_count"] == 0
    assert row["actual_net"] is None and row["settlement_net_usdt"] is None
    event = SimpleNamespace(fee=None, fee_currency=None, net_usdt=D("2"))
    p.fee, p.fee_currency = D("7"), "RUB"
    row = f._money([(p, None, None, op)], {"synthetic": [event]}, {}, {})[0]
    assert D(row["actual_fee"]) == 7 and D(row["actual_net"]) == 93
    assert D(row["settlement_net_usdt"]) == 2


def test_bot_admin_v7_utm_merge_chooses_single_earliest_attribution(bot_admin_v7):
    f, aid, did = bot_admin_v7
    first = f.bot_utm_create(
        aid,
        {"name": "Первая", "source": "tg", "medium": "post", "campaign": uuid4().hex},
        "Первая кампания",
        confirmed=True,
    )
    second = f.bot_utm_create(
        aid,
        {"name": "Вторая", "source": "tg", "medium": "post", "campaign": uuid4().hex},
        "Вторая кампания",
        confirmed=True,
    )
    f.record_utm_start(did, first["start_code"])
    with f.db.transaction() as s:
        other = Account(email=f"{uuid4()}@example.test")
        s.add(other)
        s.flush()
        oid = other.id
    f.record_utm_start(oid, second["start_code"])
    with f.db.transaction() as s:
        s.add(Alias(old_id=oid, canonical_id=did))
    payment_fixture(f, did, "100", datetime.now(UTC), "7", "sbp")
    assert f.bot_utm_stats(aid, first["id"])["paid_orders"] == 1
    assert f.bot_utm_stats(aid, second["id"])["paid_orders"] == 0


def test_bot_admin_v7_refunded_payment_keeps_confirmed_gross(bot_admin_v7):
    f, aid, _ = bot_admin_v7
    baseline = f.bot_statistics(aid, "2032-01-04", "2032-01-04")
    before = next((r for r in baseline["period_money"] if r["currency"] == "RUB"), {}).get("gross", "0")
    pid = payment_fixture(f, aid, "123", datetime(2032, 1, 4, 12, tzinfo=UTC), "7", "sbp")
    with f.db.transaction() as s:
        s.get(Payment, pid).status = "refunded"
    r = f.bot_statistics(aid, "2032-01-04", "2032-01-04")
    rub = next(r for r in r["period_money"] if r["currency"] == "RUB")
    assert D(rub["gross"]) - D(before) == 123


def test_bot_admin_v7_recurring_enabled_counts_users_not_agreements(bot_admin_v7):
    f, aid, _ = bot_admin_v7
    before = f.bot_statistics(aid)["recurring_users"]
    with f.db.transaction() as s:
        for _ in range(2):
            sub = Subscription(devices_limit=4, account_id=aid, status="active", environment="live")
            s.add(sub)
            s.flush()
            s.add(
                Agreement(
                    account_id=aid,
                    subscription_id=sub.id,
                    state="enabled",
                    consent_amount=D(100),
                    currency="RUB",
                    consent={},
                )
            )
    assert f.bot_statistics(aid)["recurring_users"] - before == 1


def test_bot_admin_v7_every_facade_read_reauthorizes_and_revocation_takes_effect(bot_admin_v7):
    f, aid, did = bot_admin_v7
    calls = [
        (f.authorize_admin, ()),
        (f.bot_referrals, ()),
        (f.bot_user_detail, (aid,)),
        (f.bot_utm_stats, ("missing",)),
        (f.bot_broadcast_detail, ("missing",)),
        (f.bot_broadcast_preview, ([{"type": "text", "text": "test"}],)),
    ]
    for method, args in calls:
        with pytest.raises(DomainError) as exc:
            method(did, *args)
        assert exc.value.status == 404
    f.authorize_admin(aid)
    f.settings.owner_id = None
    f.settings.admin_telegram_allowlist = []
    with pytest.raises(DomainError) as exc:
        f.bot_settings(aid)
    assert exc.value.status == 404


def test_bot_admin_v7_sales_sections_are_all_time_independent_of_money_filter(bot_admin_v7):
    f, aid, _ = bot_admin_v7
    before = f.bot_statistics(aid, "2061-01-02", "2061-01-02")
    first = payment_fixture(f, aid, "100", datetime(2061, 1, 2, 12, tzinfo=UTC), "7", "card")
    second = payment_fixture(f, aid, "200", datetime(2062, 1, 2, 12, tzinfo=UTC), "8", "crypto")
    with f.db.transaction() as s:
        products = {
            s.get(Quote, s.get(Order, s.get(Payment, pid).order_id).quote_id).snapshot["product_id"]
            for pid in (first, second)
        }
    result = f.bot_statistics(aid, "2061-01-02", "2061-01-02")
    assert result["purchase_count"] - before["purchase_count"] == 2
    assert products <= {row["product_id"] for row in result["popular_products"]}
    prior_methods = {row["method"]: row["count"] for row in before["payment_methods"]}
    methods = {row["method"]: row["count"] for row in result["payment_methods"]}
    assert methods["card"] - prior_methods.get("card", 0) == 1
    assert methods["crypto"] - prior_methods.get("crypto", 0) == 1
    assert result["sales_period"] == "all_time"


def test_bot_admin_v7_fixture_commits_are_invisible_to_other_connections(bot_admin_v7):
    f, aid, _ = bot_admin_v7
    external = Database(os.environ["SUNRISE_TEST_DATABASE_URL"])
    try:
        with external.transaction() as s:
            assert s.get(Account, aid) is None
        with f.db.transaction() as s:
            assert s.get(Account, aid) is not None
    finally:
        external.engine.dispose()
