import os
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from uuid import uuid4

import fakeredis
import pytest
from sqlalchemy import select
from sunrise_core.errors import DomainError
from sunrise_infrastructure.admin import Admin
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import Account, Database, Payment, Product
from sunrise_infrastructure.security import RateGuard

from tests.legal_fixture import accept_test_legal


def services():
    db = Database(os.environ["SUNRISE_TEST_DATABASE_URL"])
    assert db.engine.url.database == "sunrise_test_backend"
    commerce = Commerce(db, local_test=True)
    admin = Admin(commerce, RateGuard(fakeredis.FakeRedis()))
    assert hasattr(admin, "adjust_credit"), "Missing separate currency-aware cash-credit ledger"
    with db.transaction() as s:
        a = Account(email=f"{uuid4()}@example.test", bonus_balance=Decimal(200))
        s.add(a)
        s.flush()
        aid = a.id
    accept_test_legal(Commerce(db), a.id)
    return commerce, admin, aid


def test_cash_can_pay_full_value_without_bonus_cap_external_revenue_or_duplicates():
    c, admin, aid = services()
    operation = str(uuid4())
    admin.adjust_credit(aid, aid, "200.00", "RUB", "owner_adjustment", operation, "Синтетический кошелёк")
    admin.adjust_credit(aid, aid, "200.00", "RUB", "owner_adjustment", operation, "Повторная доставка")
    quote = c.quote(aid, "standard", 30, "purchase", bonus_amount="200", credit_amount="200")
    assert quote["bonus_amount"] == "49.50" and quote["credit_amount"] == "49.50"
    assert quote["total"] == "0.00" and quote["credit_currency"] == "RUB"
    order = c.create_order(aid, quote["id"], "cash-only")
    assert order["status"] == "paid"
    assert c.create_order(aid, quote["id"], "cash-only")["id"] == order["id"]
    dashboard = c.dashboard(aid)
    assert dashboard["credit_balance"] == "150.500000" and dashboard["credit_reserved"] == "0.000000"
    with c.db.transaction() as s:
        assert not s.scalar(select(Payment.id).where(Payment.account_id == aid))


def test_cash_reservations_are_canonical_and_cannot_overspend_concurrently():
    c, admin, aid = services()
    admin.adjust_credit(aid, aid, "50.00", "RUB", "owner_adjustment", str(uuid4()), "Тест резерва")
    quotes = [c.quote(aid, "standard", 30, "purchase", credit_amount="50") for _ in range(2)]

    def create(quote):
        try:
            return c.create_order(aid, quote["id"], str(uuid4()))
        except DomainError as exc:
            assert exc.code == "credit_insufficient"
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        orders = list(pool.map(create, quotes))
    assert sum(order is not None for order in orders) == 1
    paid = next(order for order in orders if order)
    c.test_pay(aid, paid["id"])
    assert Decimal(c.dashboard(aid)["credit_balance"]) == 0


def test_unknown_cash_currency_and_rub_bonus_cannot_be_spent_as_eur():
    c, admin, aid = services()
    with c.db.transaction() as s:
        s.get(Account, aid).credit_balance = Decimal(100)
        p = Product(
            code=str(uuid4()),
            name="EUR тест",
            price=Decimal(10),
            currency="EUR",
            tier_level=90,
            included_devices=4,
        )
        s.add(p)
        s.flush()
        pid = p.id
    assert c.dashboard(aid)["credit_currency"] is None
    with pytest.raises(DomainError) as exc:
        c.quote(aid, "standard", 30, "purchase", credit_amount="20")
    assert exc.value.code == "credit_currency_unverified"
    with pytest.raises(DomainError) as exc:
        c.quote(aid, pid, 30, "purchase", bonus_amount="1")
    assert exc.value.code == "bonus_currency_mismatch"
    admin.adjust_credit(
        aid,
        aid,
        "0.00",
        "RUB",
        "verified_legacy",
        str(uuid4()),
        "Подтверждённая валюта синтетического остатка",
        confirm_existing_currency=True,
    )
    with pytest.raises(DomainError) as exc:
        c.quote(aid, pid, 30, "purchase", credit_amount="10")
    assert exc.value.code == "credit_currency_mismatch"


def test_known_eur_credit_and_reasoned_negative_adjustments_keep_separate_ledger():
    c, admin, aid = services()
    admin.adjust_credit(aid, aid, "30.00", "EUR", "owner_adjustment", str(uuid4()), "Известная валюта EUR")
    with c.db.transaction() as s:
        p = Product(
            code=str(uuid4()),
            name="EUR тест",
            price=Decimal(10),
            currency="EUR",
            tier_level=91,
            included_devices=4,
        )
        s.add(p)
        s.flush()
        pid = p.id
    quote = c.quote(aid, pid, 30, "purchase", credit_amount="30")
    assert quote["credit_amount"] == "10.00" and quote["total"] == "0.00"
    c.create_order(aid, quote["id"], str(uuid4()))
    admin.adjust_credit(
        aid, aid, "-25.00", "EUR", "refund_compensation", str(uuid4()), "Отдельная корректировка"
    )
    assert Decimal(c.dashboard(aid)["credit_balance"]) == Decimal(-5)
    assert Decimal(c.dashboard(aid)["bonus_balance"]) == Decimal(200)


def test_fractional_cash_and_bonus_are_floored_preserving_legacy_dust():
    c, admin, aid = services()
    admin.adjust_credit(aid, aid, "10.00", "RUB", "owner_adjustment", str(uuid4()), "Тест точности")
    with c.db.transaction() as s:
        account = s.get(Account, aid)
        account.credit_balance = Decimal("12.345678")
        account.bonus_balance = Decimal("1.235678")
    assert c.dashboard(aid)["credit_available"] == "12.34"
    quote = c.quote_with_max_bonus(
        aid, product_id="standard", period_days=30, action="purchase", credit_amount="12.34"
    )
    assert quote["bonus_amount"] == "1.23"
    order = c.create_order(aid, quote["id"], str(uuid4()))
    c.test_pay(aid, order["id"])
    dashboard = c.dashboard(aid)
    assert Decimal(dashboard["credit_balance"]) == Decimal("0.005678")
    assert Decimal(dashboard["bonus_balance"]) == Decimal("0.005678")


def test_external_refund_does_not_restore_internal_credit_or_bonus():
    from sunrise_infrastructure.billing import Billing

    c, admin, aid = services()
    admin.adjust_credit(aid, aid, "20.00", "RUB", "owner_adjustment", str(uuid4()), "Раздельный возврат")
    quote = c.quote(aid, "standard", 30, "purchase", bonus_amount="10", credit_amount="20")
    order = c.create_order(aid, quote["id"], str(uuid4()))
    c.test_pay(aid, order["id"])
    before = c.dashboard(aid)
    with c.db.transaction() as s:
        provider_id = s.scalar(select(Payment.provider_id).where(Payment.order_id == order["id"]))
    from sunrise_infrastructure.db import now

    billing = Billing(c, "synthetic-terminal", "local_test")
    with c.db.transaction() as s:
        payment = s.scalar(select(Payment).where(Payment.order_id == order["id"]))
        terminal = payment.terminal
    billing.terminal = terminal
    billing.refund(provider_id, str(uuid4()), Decimal("10.00"), now())
    after = c.dashboard(aid)
    assert after["credit_balance"] == before["credit_balance"]
    assert after["bonus_balance"] == before["bonus_balance"]


def test_same_currency_merge_preserves_reservations_dust_and_credit_operation_identity():
    from sunrise_infrastructure.accounts import Accounts
    from sunrise_infrastructure.db import CreditLedger, now
    from sunrise_infrastructure.settings import Settings

    c, admin, aid = services()
    telegram = int(uuid4().int % 10**12) + 100000
    target = int(c.resolve_telegram(telegram)["id"])
    accept_test_legal(c, target)
    admin.adjust_credit(aid, aid, "30.00", "RUB", "owner_adjustment", str(uuid4()), "Первый кошелёк")
    operation = str(uuid4())
    admin.adjust_credit(aid, target, "50.00", "RUB", "owner_adjustment", operation, "Второй кошелёк")
    bonus_operation = str(uuid4())
    admin.adjust_bonus(aid, target, "10.00", bonus_operation, "Бонус до объединения")
    with c.db.transaction() as s:
        s.get(Account, aid).email_verified = True
        s.get(Account, aid).credit_balance += Decimal("0.005678")
    quote = c.quote(target, "standard", 30, "purchase", credit_amount="50")
    order = c.create_order(target, quote["id"], str(uuid4()))
    accounts = Accounts(
        c.db, RateGuard(fakeredis.FakeRedis()), Settings(database_url=os.environ["SUNRISE_TEST_DATABASE_URL"])
    )
    proof = accounts.request_link(aid, str(telegram), now())["proof"]
    accounts.confirm_telegram_link(target, proof)
    admin.adjust_credit(
        aid, target, "50.00", "RUB", "owner_adjustment", operation, "Повтор после объединения"
    )
    admin.adjust_credit(aid, aid, "50.00", "RUB", "owner_adjustment", operation, "Повтор через основной ID")
    admin.adjust_bonus(aid, aid, "10.00", bonus_operation, "Повтор бонуса через основной ID")
    assert Decimal(c.dashboard(aid)["bonus_balance"]) == 210
    assert Decimal(c.dashboard(aid)["credit_balance"]) == Decimal("80.005678")
    assert Decimal(c.dashboard(aid)["credit_reserved"]) == Decimal(50)
    c.test_pay(target, order["id"])
    assert Decimal(c.dashboard(aid)["credit_balance"]) == Decimal("30.005678")
    with c.db.transaction() as s:
        rows = list(s.scalars(select(CreditLedger).where(CreditLedger.account_id == aid)))
        assert len(rows) == 3 and sum(row.amount for row in rows) == Decimal(30)


def test_confirmed_provider_cancellation_releases_credit_reservation_once():
    from sunrise_infrastructure.billing import Billing
    from sunrise_infrastructure.db import Inbox

    c, admin, aid = services()
    admin.adjust_credit(aid, aid, "50.00", "RUB", "owner_adjustment", str(uuid4()), "Резерв до отмены")
    c.local_test = False
    quote = c.quote(aid, "standard", 30, "purchase", credit_amount="50")
    order = c.create_order(aid, quote["id"], str(uuid4()))

    class Provider:
        terminal = "synthetic-terminal"

        def create_payment(self, order_id, amount, currency, account_id, *, description=None):
            self.data = {
                "payment_id": "test-" + order_id,
                "order_id": order_id,
                "amount": amount,
                "payment_currency": currency,
                "test": True,
                "terminal_id": self.terminal,
                "customer_id": account_id,
                "status": "canceled",
            }
            return {
                "payment_id": self.data["payment_id"],
                "order_id": order_id,
                "pay_url": "https://pay.rollypay.io/invoice/synthetic",
                "test": True,
                "environment": "sandbox",
            }

        def payment(self, pid):
            return self.data

    provider = Provider()
    billing = Billing(c, provider.terminal, "sandbox")
    billing.prepare_payment(order["id"], provider)
    with c.db.transaction() as s:
        s.add(
            Inbox(
                event_key=str(uuid4()),
                payload={"payment_id": provider.data["payment_id"], "order_id": order["id"]},
            )
        )
    billing.reconcile_inbox(provider)
    billing.reconcile_inbox(provider)
    assert Decimal(c.dashboard(aid)["credit_reserved"]) == 0
    assert Decimal(c.dashboard(aid)["credit_balance"]) == 50


def test_cash_only_fulfillment_counts_service_without_external_cash_or_referral():
    from datetime import timedelta

    from sunrise_infrastructure.db import Order, Subscription, now

    c, admin, aid = services()
    admin.adjust_credit(
        aid, aid, "200.00", "RUB", "owner_adjustment", str(uuid4()), "Учёт услуги без внешней оплаты"
    )
    with c.db.transaction() as s:
        product = Product(
            code=str(uuid4()),
            name="Тест состава услуг",
            price=Decimal(100),
            currency="RUB",
            tier_level=91,
            included_devices=4,
        )
        s.add(product)
        s.flush()
        pid = product.id
    quote = c.quote(aid, pid, 30, "purchase", credit_amount="200")
    order = c.create_order(aid, quote["id"], str(uuid4()))
    with c.db.transaction() as s:
        record = s.get(Order, order["id"])
        record.environment = "live"
        s.get(Subscription, record.subscription_id).environment = "live"
    report = c.analytics(now() - timedelta(days=1), now() + timedelta(days=1), product_id=pid)
    assert report["currencies"] == {} and report["paid_count"] == 0 and report["new_payers"] == 0
    assert sum(item["count"] for item in report["sales_composition"]) == 1
    assert report["credit_spent"] == {"RUB": "100.00"}
    assert report["service_value"] == {"RUB": "100.00"}
    assert len(report["service_rows"]) == 1 and report["service_rows"][0]["time_source"] == "fulfilled_at"


def test_new_credit_operation_after_merge_is_idempotent_across_alias_and_canonical_ids():
    from sunrise_infrastructure.accounts import Accounts
    from sunrise_infrastructure.db import CreditLedger, now
    from sunrise_infrastructure.settings import Settings

    c, admin, aid = services()
    telegram = int(uuid4().int % 10**12) + 100000
    alias = int(c.resolve_telegram(telegram)["id"])
    accept_test_legal(c, alias)
    with c.db.transaction() as s:
        s.get(Account, aid).email_verified = True
    accounts = Accounts(
        c.db, RateGuard(fakeredis.FakeRedis()), Settings(database_url=os.environ["SUNRISE_TEST_DATABASE_URL"])
    )
    accounts.confirm_telegram_link(alias, accounts.request_link(aid, str(telegram), now())["proof"])
    operation = str(uuid4())
    for identity in (alias, aid, alias):
        admin.adjust_credit(
            aid, identity, "10.00", "RUB", "owner_adjustment", operation, "Одна операция после объединения"
        )
    assert Decimal(c.dashboard(aid)["credit_balance"]) == 10
    with c.db.transaction() as s:
        assert len(list(s.scalars(select(CreditLedger).where(CreditLedger.account_id == aid)))) == 1


def test_ambiguous_premerge_credit_namespaces_require_review_without_new_grant():
    from sunrise_infrastructure.accounts import Accounts
    from sunrise_infrastructure.db import CreditLedger, now
    from sunrise_infrastructure.settings import Settings

    c, admin, aid = services()
    telegram = int(uuid4().int % 10**12) + 100000
    alias = int(c.resolve_telegram(telegram)["id"])
    accept_test_legal(c, alias)
    operation = str(uuid4())
    for identity in (aid, alias):
        admin.adjust_credit(
            aid,
            identity,
            "10.00",
            "RUB",
            "owner_adjustment",
            operation,
            "Независимая операция до объединения",
        )
    with c.db.transaction() as s:
        s.get(Account, aid).email_verified = True
    accounts = Accounts(
        c.db, RateGuard(fakeredis.FakeRedis()), Settings(database_url=os.environ["SUNRISE_TEST_DATABASE_URL"])
    )
    accounts.confirm_telegram_link(alias, accounts.request_link(aid, str(telegram), now())["proof"])
    with pytest.raises(DomainError) as error:
        admin.adjust_credit(aid, aid, "10.00", "RUB", "owner_adjustment", operation, "Неоднозначный повтор")
    assert error.value.code == "key_conflict"
    assert Decimal(c.dashboard(aid)["credit_balance"]) == 20
    with c.db.transaction() as s:
        assert len(list(s.scalars(select(CreditLedger).where(CreditLedger.account_id == aid)))) == 2
