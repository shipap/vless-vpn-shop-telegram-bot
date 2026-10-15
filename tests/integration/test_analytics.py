import os
from datetime import UTC, datetime
from decimal import Decimal as D
from uuid import uuid4

import pytest
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import Account, Database, Order, Payment, Product, Refund

from tests.legal_fixture import accept_test_legal


def test_cash_cohort_currency_fees_and_details_reconcile():
    url = os.getenv("SUNRISE_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Synthetic PostgreSQL required")
    c = Commerce(Database(url), True)
    pid = str(uuid4())
    with c.db.transaction() as s:
        a = Account(email=f"{uuid4()}@example.test")
        p = Product(
            included_devices=4,
            currency="RUB",
            id=pid,
            code=pid,
            name="Финансовая проверка",
            tier_level=1,
            price=D(100),
        )
        s.add_all([a, p])
        s.flush()
        aid = a.id
    accept_test_legal(Commerce(c.db), a.id)
    q = c.quote(aid, pid, 30, "purchase")
    o = c.create_order(aid, q["id"], str(uuid4()))
    p1 = str(uuid4())
    with c.db.transaction() as s:
        order = s.get(Order, o["id"])
        order.environment = "live"
        s.add(
            Payment(
                id=p1,
                provider="synthetic_statistics",
                provider_id=str(uuid4()),
                terminal="test",
                environment="live",
                order_id=o["id"],
                account_id=aid,
                amount=D(100),
                currency="RUB",
                paid_at=datetime(2025, 1, 31, 23, 30, tzinfo=UTC),
                fee=None,
            )
        )
        s.flush()
        s.add(
            Refund(
                payment_id=p1,
                operation_id=str(uuid4()),
                amount=D(25),
                occurred_at=datetime(2025, 2, 15, tzinfo=UTC),
            )
        )
    jan = c.analytics(
        datetime(2025, 1, 1, tzinfo=UTC), datetime(2025, 2, 1, tzinfo=UTC), "UTC", product_id=pid
    )
    assert jan["currencies"] == {"RUB": "100.00"} and jan["refunds"] == {}
    assert jan["unknown_fee_count"] == 1 and jan["net_usdt"] is None
    with c.db.transaction() as s:
        s.get(Order, o["id"]).status = "refund_review"
    unfulfilled = c.analytics(
        datetime(2025, 1, 1, tzinfo=UTC), datetime(2025, 2, 1, tzinfo=UTC), "UTC", product_id=pid
    )
    assert unfulfilled["service_count"] == 0 and unfulfilled["service_value"] == {}
    feb = c.analytics(
        datetime(2025, 2, 1, tzinfo=UTC), datetime(2025, 3, 1, tzinfo=UTC), "UTC", product_id=pid
    )
    assert feb["currencies"] == {} and feb["refunds"] == {"RUB": "25.00"}
    assert sum(D(x["amount"]) for x in feb["rows"]) == D(feb["net_after_refunds"]["RUB"]) == D(-25)
    cohort = c.analytics(
        datetime(2025, 1, 1, tzinfo=UTC),
        datetime(2025, 2, 1, tzinfo=UTC),
        "UTC",
        product_id=pid,
        mode="cohort",
    )
    assert cohort["net_after_refunds"] == {"RUB": "75.00"}
    with c.db.transaction() as s:
        p = s.get(Payment, p1)
        p.fee = D("0.10")
        p.fee_currency = "USDT"
        p.net_usdt = D("1.00")
    updated = c.analytics(
        datetime(2025, 1, 1, tzinfo=UTC), datetime(2025, 2, 1, tzinfo=UTC), "UTC", product_id=pid
    )
    assert D(updated["net_usdt"]) == D("1.00")  # Already net; no second subtraction.
    assert updated["unknown_fee_count"] == 0 and D(updated["confirmed_fees"]["USDT"]) == D("0.10")
    empty = c.analytics(
        datetime(2025, 1, 1, tzinfo=UTC),
        datetime(2025, 2, 1, tzinfo=UTC),
        "UTC",
        product_id=pid,
        currency="EUR",
    )
    assert empty["rows"] == [] and empty["currencies"] == {}
    from zoneinfo import ZoneInfo

    moscow_jan = c.analytics(
        datetime(2025, 1, 1, tzinfo=ZoneInfo("Europe/Moscow")),
        datetime(2025, 2, 1, tzinfo=ZoneInfo("Europe/Moscow")),
        "Europe/Moscow",
        product_id=pid,
    )
    assert moscow_jan["currencies"] == {}  # 31 января 23:30 UTC — уже февраль в Москве.
    assert moscow_jan["historical_accounting_complete"] is False
    assert empty["paid_count"] == 0 and empty["unknown_fee_count"] == 0


def test_delayed_settlement_cash_date_duplicate_and_csv_totals():
    from sunrise_infrastructure.billing import Billing, VerifiedPayment

    url = os.getenv("SUNRISE_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Synthetic PostgreSQL required")
    c = Commerce(Database(url), True)
    pid = str(uuid4())
    with c.db.transaction() as s:
        a = Account(email=f"{uuid4()}@example.test")
        p = Product(
            included_devices=4,
            currency="RUB",
            id=pid,
            code=pid,
            name="Первое имя",
            tier_level=1,
            price=D(100),
        )
        s.add_all([a, p])
        s.flush()
        aid = a.id
    accept_test_legal(Commerce(c.db), a.id)
    q = c.quote(aid, pid, 30, "purchase")
    o = c.create_order(aid, q["id"], str(uuid4()))
    with c.db.transaction() as s:
        s.get(Order, o["id"]).environment = "live"
    # Synthetic statistics facts exercise SQL gates; this is not production admission.
    from sunrise_infrastructure.db import Setting

    from tests.integration.test_production_business_races import SyntheticBusinessPolicy

    with c.db.transaction() as s:
        s.merge(
            Setting(
                key="production_admission",
                value={"scope_id": "synthetic-pg-business", "synthetic_business_only": True},
            )
        )
        s.merge(Setting(key="production_runtime_gates", value={"financial_apply": True}))
    c.runtime_policy = SyntheticBusinessPolicy(c.db)
    billing = Billing(c, "statistics-only", "live")
    provider_id = str(uuid4())
    paid = datetime(2025, 1, 15, tzinfo=UTC)
    settled = datetime(2025, 2, 15, tzinfo=UTC)
    billing.accept(
        VerifiedPayment(
            "synthetic_statistics", provider_id, "statistics-only", "live", o["id"], D(100), "RUB", paid
        )
    )
    billing.record_settlement(provider_id, "conversion-1", D("0.10"), "USDT", D("1.00"), settled)
    billing.record_settlement(provider_id, "conversion-1", D("0.10"), "USDT", D("1.00"), settled)
    jan = c.analytics(
        datetime(2025, 1, 1, tzinfo=UTC), datetime(2025, 2, 1, tzinfo=UTC), "UTC", product_id=pid
    )
    feb = c.analytics(
        datetime(2025, 2, 1, tzinfo=UTC), datetime(2025, 3, 1, tzinfo=UTC), "UTC", product_id=pid
    )
    assert jan["net_usdt"] is None and jan["confirmed_fees"] == {}
    assert D(feb["net_usdt"]) == D("1.00") and D(feb["confirmed_fees"]["USDT"]) == D("0.10")
    assert len(feb["settlement_series"]) == 1
    cohort = c.analytics(
        datetime(2025, 1, 1, tzinfo=UTC),
        datetime(2025, 2, 1, tzinfo=UTC),
        "UTC",
        product_id=pid,
        mode="cohort",
    )
    assert D(cohort["net_usdt"]) == D("1.00")
    assert sum(D(x["net_usdt"] or "0") for x in cohort["rows"]) == D(cohort["net_usdt"])


def test_sale_composition_uses_stable_product_not_renamed_label():
    url = os.getenv("SUNRISE_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Synthetic PostgreSQL required")
    c = Commerce(Database(url), True)
    pid = str(uuid4())
    with c.db.transaction() as s:
        a = Account(email=f"{uuid4()}@example.test")
        s.add_all(
            [
                a,
                Product(
                    included_devices=4,
                    currency="RUB",
                    id=pid,
                    code=pid,
                    name="До",
                    tier_level=1,
                    price=D(100),
                ),
            ]
        )
        s.flush()
        aid = a.id
    accept_test_legal(Commerce(c.db), a.id)
    for name in ("До", "После"):
        with c.db.transaction() as s:
            s.get(Product, pid).name = name
        q = c.quote(aid, pid, 30, "purchase")
        o = c.create_order(aid, q["id"], str(uuid4()))
        c.test_pay(aid, o["id"])
        with c.db.transaction() as s:
            order = s.get(Order, o["id"])
            order.environment = "live"
            # Историческая синтетическая выдача имеет явно заданную дату, отдельно от банковского факта.
            order.fulfilled_at = datetime(2025, 1, 20, tzinfo=UTC)
            s.add(
                Payment(
                    provider="fixture",
                    provider_id=str(uuid4()),
                    terminal="fixture",
                    environment="live",
                    order_id=o["id"],
                    account_id=aid,
                    amount=D(100),
                    currency="RUB",
                    paid_at=datetime(2025, 1, 20, tzinfo=UTC),
                )
            )
    data = c.analytics(
        datetime(2025, 1, 1, tzinfo=UTC), datetime(2025, 2, 1, tzinfo=UTC), "UTC", product_id=pid
    )
    assert len(data["sales_composition"]) == 1 and data["sales_composition"][0]["count"] == 2
    assert data["new_payers"] == 1 and data["currencies"] == {"RUB": "200.00"}


def test_multicurrency_cash_csv_and_recurring_attempt_cycle_denominator():
    import csv
    import io
    from datetime import timedelta

    import fakeredis
    import pyotp
    from fastapi.testclient import TestClient
    from sunrise_api.main import create_app
    from sunrise_infrastructure.db import Agreement, RecurringAttempt, Session, Subscription, now
    from sunrise_infrastructure.security import digest
    from sunrise_infrastructure.settings import Settings

    url = os.getenv("SUNRISE_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Synthetic PostgreSQL required")
    c = Commerce(Database(url), True)
    pid = str(uuid4())
    email = f"{uuid4()}@example.test"
    token = str(uuid4())
    moment = now()
    telegram_id = 9000000000000000 + int(uuid4().hex[:8], 16)
    with c.db.transaction() as s:
        a = Account(email=email, email_verified=True, telegram_id=telegram_id)
        s.add_all(
            [
                a,
                Product(
                    included_devices=4,
                    currency="RUB",
                    id=pid,
                    code=pid,
                    name="Валюты",
                    tier_level=1,
                    price=D("100"),
                ),
            ]
        )
        s.flush()
        aid = a.id
        s.add(
            Session(
                id=digest(token),
                account_id=aid,
                csrf="fixture",
                expires_at=moment + timedelta(hours=1),
                confirmed_at=moment,
            )
        )
    accept_test_legal(Commerce(c.db), a.id)
    for currency, amount, environment in [
        ("RUB", "100", "live"),
        ("EUR", "7", "live"),
        ("RUB", "500", "local_test"),
    ]:
        q = c.quote(aid, pid, 30, "purchase")
        o = c.create_order(aid, q["id"], str(uuid4()))
        with c.db.transaction() as s:
            s.get(Order, o["id"]).environment = environment
            s.add(
                Payment(
                    provider="statistics_fixture",
                    provider_id=str(uuid4()),
                    terminal="fixture",
                    environment=environment,
                    order_id=o["id"],
                    account_id=aid,
                    amount=D(amount),
                    currency=currency,
                    paid_at=datetime(2025, 1, 20, tzinfo=UTC),
                )
            )
    with c.db.transaction() as s:
        sub = Subscription(
            devices_limit=4,
            account_id=aid,
            product_id=pid,
            environment="live",
            expires_at=moment + timedelta(days=30),
        )
        s.add(sub)
        s.flush()
        ag = Agreement(
            account_id=aid, subscription_id=sub.id, consent_amount=D("100"), currency="RUB", state="enabled"
        )
        s.add(ag)
        s.flush()
        for number, state, hour in [
            (1, "pending", 1),
            (1, "failed", 2),
            (1, "declined", 3),
            (2, "pending", 1),
            (3, "review", 1),
        ]:
            s.add(
                RecurringAttempt(
                    id=str(uuid4()),
                    agreement_id=ag.id,
                    cycle_number=number,
                    state=state,
                    occurred_at=datetime(2025, 1, 20, hour, tzinfo=UTC),
                )
            )
    start = datetime(2025, 1, 1, tzinfo=UTC)
    end = datetime(2025, 2, 1, tzinfo=UTC)
    data = c.analytics(start, end, "UTC", product_id=pid)
    assert data["currencies"] == {"RUB": "100.00", "EUR": "7.00"}
    assert data["recurring_due_count"] == 3 and data["recurring_attempt_count"] == 5
    assert {x["state"]: x["count"] for x in data["recurring_cycles"]} == {
        "declined": 1,
        "pending": 1,
        "review": 1,
    }
    secret = pyotp.random_base32()
    settings = Settings(
        database_url=url,
        secret_key="s" * 48,
        local_test=True,
        admin_email_allowlist=[email],
        admin_telegram_allowlist=[str(telegram_id)],
        admin_totp_secrets={email: secret},
        allowed_hosts=["testserver"],
    )
    with TestClient(
        create_app(settings, redis_client=fakeredis.FakeRedis()), client=("127.0.0.1", 50000)
    ) as client:
        client.cookies.set("sunrise_session", token)
        verified = client.post(
            "/api/v1/admin/mfa/verify",
            json={"code": pyotp.TOTP(secret).now()},
            headers={"X-CSRF-Token": "fixture"},
        )
        assert verified.status_code == 200, verified.text
        result = client.get(
            "/api/v1/admin/analytics/export.csv",
            params={
                "date_from": start.isoformat(),
                "date_to": end.isoformat(),
                "timezone": "UTC",
                "product_id": pid,
            },
        )
        assert result.status_code == 200, result.text
        rows = list(csv.DictReader(io.StringIO(result.text)))
        for currency in ("RUB", "EUR"):
            assert sum(D(row["amount"]) for row in rows if row["currency"] == currency) == D(
                data["net_after_refunds"][currency]
            )
        filtered = client.get(
            "/api/v1/admin/overview",
            params={
                "date_from": start.isoformat(),
                "date_to": end.isoformat(),
                "timezone": "UTC",
                "product_id": pid,
                "currency": "EUR",
            },
        )
        assert filtered.status_code == 200 and filtered.json()["currencies"] == {"EUR": "7.00"}
