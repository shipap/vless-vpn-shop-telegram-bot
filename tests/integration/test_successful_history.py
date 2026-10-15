"""History projections omit failed attempts without deleting financial evidence."""

from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import func, select
from sunrise_infrastructure.admin import Admin
from sunrise_infrastructure.bot_admin import BotAdmin
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import Account, Order, Payment, Quote, now

from tests.isolated_database import isolated_database


def test_customer_and_admin_user_histories_keep_paid_and_refunded_facts_only():
    with isolated_database("successful_history") as db:
        commerce = Commerce(db, local_test=True)
        with db.transaction() as session:
            owner = Account(telegram_id=70000000000000001)
            session.add(owner)
            session.flush()
            aid = owner.id
            for status in ("paid", "partially_refunded", "refunded", "failed", "pending", "canceled"):
                quote = Quote(account_id=aid, snapshot={}, expires_at=now())
                session.add(quote)
                session.flush()
                order = Order(
                    id="order-" + status,
                    account_id=aid,
                    quote_id=quote.id,
                    idempotency_key=status,
                    status="reconciliation_required",
                    amount=Decimal("10"),
                    currency="RUB",
                )
                session.add(order)
                session.flush()
                session.add(
                    Payment(
                        id="payment-" + status,
                        provider="synthetic",
                        provider_id=status,
                        environment="local_test",
                        account_id=aid,
                        order_id=order.id,
                        status=status,
                        amount=Decimal("10"),
                        currency="RUB",
                    )
                )
            quote = Quote(account_id=aid, snapshot={}, expires_at=now())
            session.add(quote)
            session.flush()
            session.add(
                Order(
                    id="bonus-paid",
                    account_id=aid,
                    quote_id=quote.id,
                    idempotency_key="bonus",
                    status="paid",
                    amount=Decimal("0"),
                    currency="RUB",
                )
            )
        expected_payments = {"payment-" + status for status in ("paid", "partially_refunded", "refunded")}
        dashboard = commerce.dashboard(aid)
        assert {p["id"] for p in dashboard["payments"]} == expected_payments
        admin = Admin(commerce, None)
        details = admin.user_detail(aid)
        assert {p["id"] for p in details["payments"]} == expected_payments
        assert {o["id"] for o in details["orders"]} == {
            "order-paid",
            "order-partially_refunded",
            "order-refunded",
            "bonus-paid",
        }
        assert details["pagination"]["totals"]["payments"] == 3
        assert details["pagination"]["totals"]["orders"] == 4
        settings = SimpleNamespace(
            owner_id="70000000000000001",
            admin_telegram_allowlist=[],
            test_telegram_recipients="70000000000000001",
        )
        admin.accounts = SimpleNamespace(db=db, settings=settings)
        bot_details = BotAdmin(admin, settings).bot_user_detail(aid, aid)
        assert {p["id"] for p in bot_details["payments"]["items"]} == expected_payments
        assert bot_details["payments"]["total"] == 3
        assert {o["id"] for o in bot_details["orders"]["items"]} == {
            "order-paid",
            "order-partially_refunded",
            "order-refunded",
            "bonus-paid",
        }
        assert bot_details["orders"]["total"] == 4
        assert admin.list_items("payments")["total"] == 6
        assert admin.list_items("orders")["total"] == 7
        with db.transaction() as session:
            assert session.scalar(select(func.count()).select_from(Payment)) == 6
            assert session.scalar(select(func.count()).select_from(Order)) == 7
