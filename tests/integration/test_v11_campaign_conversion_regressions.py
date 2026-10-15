from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sunrise_infrastructure.campaigns import Campaigns
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import CampaignVisit, Order, Payment, Quote, Setting, now
from test_v11_notifications import notification_context as notification_context


def campaigns(context):
    db, settings, _ = context
    with db.transaction() as session:
        for identifier in ("a" * 24, "b" * 24):
            session.add(Setting(key="utm:" + identifier, value={"name": "Синтетическая кампания"}))
    return Campaigns(Commerce(db, local_test=True, public_settings=settings), settings)


def payment(db, account_id, key, paid_at):
    with db.transaction() as session:
        quote = Quote(account_id=account_id, snapshot={}, expires_at=now() + timedelta(hours=1))
        session.add(quote)
        session.flush()
        order = Order(
            account_id=account_id,
            quote_id=quote.id,
            idempotency_key=key,
            status="paid",
            amount=Decimal("100.00"),
            currency="RUB",
            fulfilled_at=now(),
        )
        session.add(order)
        session.flush()
        result = Payment(
            provider="local_test",
            provider_id=key,
            environment="local_test",
            order_id=order.id,
            account_id=account_id,
            amount=Decimal("100.00"),
            currency="RUB",
            paid_at=paid_at,
        )
        session.add(result)
        session.flush()
        return order.id, result.id


def test_untracked_historical_payment_never_becomes_campaign_conversion(notification_context):
    db, _, (owner, _, _) = notification_context
    service = campaigns(notification_context)
    payment(db, owner, "old-untracked", now() - timedelta(days=1))
    proof = service.visit("a" * 24, "synthetic-visitor-historical")
    service.attach(owner, [proof["visit_token"]])
    result = service.stats(owner, "a" * 24)
    assert (result["paid_orders"], result["turnover"]) == (0, "0.00")


def test_paid_conversion_keeps_campaign_after_later_touch_and_replay(notification_context):
    db, _, (owner, _, _) = notification_context
    service = campaigns(notification_context)
    service.attach(owner, [service.visit("a" * 24, "synthetic-visitor-first")["visit_token"]])
    order_id, payment_id = payment(db, owner, "tracked-payment", now())
    with db.transaction() as session:
        service.record_paid_in(session, session.get(Order, order_id), payment_id)
    service.attach(owner, [service.visit("b" * 24, "synthetic-visitor-last")["visit_token"]])
    with db.transaction() as session:
        service.record_paid_in(session, session.get(Order, order_id), payment_id)
    assert service.stats(owner, "a" * 24)["paid_orders"] == 1
    assert service.stats(owner, "a" * 24)["turnover"] == "100.00"
    assert service.stats(owner, "b" * 24)["paid_orders"] == 0


@pytest.mark.parametrize("status", ["refunded", "partially_refunded", "chargeback"])
def test_common_purchase_captures_one_conversion_and_refund_preserves_gross(notification_context, status):
    from sunrise_infrastructure.commerce import bootstrap

    from tests.legal_fixture import accept_test_legal

    db, settings, (owner, _, _) = notification_context
    bootstrap(db)
    service = campaigns(notification_context)
    accept_test_legal(service.commerce, owner)
    service.attach(owner, [service.visit("a" * 24, "synthetic-common-checkout")["visit_token"]])
    product = service.commerce.catalog()["products"][0]
    quote = service.commerce.quote(owner, product_id=product["id"], period_days=30, action="purchase")
    order = service.commerce.create_order(owner, quote["id"], "common-campaign-payment")
    service.commerce.test_pay(owner, order["id"])
    service.commerce.test_pay(owner, order["id"])
    assert service.stats(owner, "a" * 24)["paid_orders"] == 1
    with db.transaction() as session:
        snapshot = session.get(Setting, "campaign_conversion:" + order["id"])
        assert snapshot.value["first_campaign_id"] == snapshot.value["last_campaign_id"] == "a" * 24
        amount = snapshot.value["amount"]
        session.scalar(select(Payment).where(Payment.order_id == order["id"])).status = status
    report = service.stats(owner, "a" * 24)
    assert (report["paid_orders"], report["turnover"]) == (1, amount)


def test_payment_window_excludes_expired_touch_and_stores_first_and_last(notification_context):
    db, _, (owner, _, _) = notification_context
    service = campaigns(notification_context)
    stamp = now()
    with db.transaction() as session:
        for key, campaign_id, days in (
            ("expired", "a" * 24, 31),
            ("first", "a" * 24, 2),
            ("last", "b" * 24, 1),
        ):
            session.add(
                Setting(
                    key="campaign_touch:" + key,
                    value={
                        "account_id": str(owner),
                        "campaign_id": campaign_id,
                        "observed_at": (stamp - timedelta(days=days)).isoformat(),
                        "attached_at": (stamp - timedelta(days=days)).isoformat(),
                    },
                )
            )
    order_id, payment_id = payment(db, owner, "window-payment", stamp)
    with db.transaction() as session:
        service.record_paid_in(session, session.get(Order, order_id), payment_id)
    assert service.stats(owner, "a" * 24)["paid_orders"] == 0
    assert service.stats(owner, "b" * 24)["paid_orders"] == 1
    with db.transaction() as session:
        snapshot = session.get(Setting, "campaign_conversion:" + order_id).value
        assert snapshot["first_campaign_id"] == "a" * 24 and snapshot["last_campaign_id"] == "b" * 24


def test_late_callback_ignores_post_payment_touch_and_expired_window(notification_context):
    db, _, (owner, _, _) = notification_context
    service = campaigns(notification_context)
    order_id, payment_id = payment(db, owner, "delayed-callback", now() - timedelta(days=31))
    service.attach(owner, [service.visit("a" * 24, "synthetic-visitor-too-late")["visit_token"]])
    with db.transaction() as session:
        service.record_paid_in(session, session.get(Order, order_id), payment_id)
    assert service.stats(owner, "a" * 24)["paid_orders"] == 0
    with db.transaction() as session:
        snapshot = session.get(Setting, "campaign_conversion:" + order_id)
        assert snapshot is not None and snapshot.value["last_campaign_id"] is None


def test_delayed_callback_uses_immutable_touch_before_later_revisit(notification_context):
    db, _, (owner, _, _) = notification_context
    service = campaigns(notification_context)
    proof = service.visit("a" * 24, "synthetic-visitor-revisit")
    service.attach(owner, [proof["visit_token"]])
    paid_at = now()
    order_id, payment_id = payment(db, owner, "paid-before-revisit", paid_at)
    service.visit("a" * 24, "synthetic-visitor-revisit")
    with db.transaction() as session:
        assert session.scalar(select(CampaignVisit.last_seen_at)) > paid_at
        service.record_paid_in(session, session.get(Order, order_id), payment_id)
    assert service.stats(owner, "a" * 24)["paid_orders"] == 1
