"""Fresh local evidence prices renewal reminders without polling the panel fleet."""

from datetime import timedelta

import httpx
from sqlalchemy import func, select
from sunrise_infrastructure.db import Account, Notification, Order, Quote, now
from sunrise_infrastructure.scheduler import Scheduler

from tests.integration.test_subscription_engagement import paid_creation as paid_creation
from tests.integration.test_v11_panel_events import owned_panel as owned_panel


def test_fresh_reminder_has_exact_common_quote_and_dedup_without_panel_io(paid_creation):
    db, panel, commerce, config, state, _, aid, sid, _ = paid_creation
    commerce.provisioning.run_jobs()
    expiry = now() + timedelta(hours=12)
    state["user"]["expireAt"] = expiry.isoformat()
    commerce.dashboard(aid, refresh_panel=True)
    with db.transaction() as session:
        before_orders = session.scalar(select(func.count()).select_from(Order))
        before_reserved = session.get(Account, aid).bonus_reserved

    def no_http(request):
        raise AssertionError("A local reminder must not poll a user")

    panel.client._transport = httpx.MockTransport(no_http)
    scheduler = Scheduler(commerce, config)
    assert scheduler.remind() >= 1
    assert scheduler.remind() == 0
    with db.transaction() as session:
        notice = session.scalar(
            select(Notification).where(
                Notification.account_id == aid, Notification.kind == "subscription_expiry"
            )
        )
        assert notice is not None
        action = notice.actions[0]
        assert action["operation"] == "renew" and action["metadata"]["subscription_id"] == sid
        quote = session.get(Quote, action["metadata"]["quote_id"])
        assert (
            quote.account_id == aid and quote.subscription_id == sid and quote.snapshot["action"] == "renew"
        )
        assert quote.snapshot["total"] in notice.body
        currency = quote.snapshot["currency"]
        assert ("₽" if currency == "RUB" else currency) in notice.body
        assert session.scalar(select(func.count()).select_from(Order)) == before_orders
        assert session.get(Account, aid).bonus_reserved == before_reserved


def test_reminder_price_includes_purchased_extra_devices_from_the_panel(paid_creation):
    from decimal import Decimal

    from sunrise_infrastructure.db import Product, Subscription

    db, panel, commerce, config, state, _, aid, sid, _ = paid_creation
    commerce.provisioning.run_jobs()
    with db.transaction() as session:
        product = session.get(Product, session.get(Subscription, sid).product_id)
        product.extra_device_price = 50
        included, monthly = product.included_devices, Decimal(product.price)
    state["user"]["hwidDeviceLimit"] = included + 2
    state["user"]["expireAt"] = (now() + timedelta(hours=12)).isoformat()
    commerce.dashboard(aid, refresh_panel=True)
    assert Scheduler(commerce, config).remind() == 1
    with db.transaction() as session:
        notice = session.scalar(
            select(Notification).where(Notification.account_id == aid, Notification.kind == "subscription_expiry")
        )
        quote = session.get(Quote, notice.actions[0]["metadata"]["quote_id"])
        devices = quote.snapshot["device_pricing"]
        assert devices["extra_devices"] == 2 and Decimal(devices["amount"]) == Decimal("100.00")
        assert Decimal(quote.snapshot["base_amount"]) >= monthly + Decimal("100.00")
        assert quote.snapshot["total"] in notice.body


def expiry_notices(db, aid):
    with db.transaction() as session:
        return session.scalars(
            select(Notification.event_key).where(
                Notification.account_id == aid, Notification.kind == "subscription_expiry"
            )
        ).all()


def test_next_term_gets_its_own_reminders_after_a_renewal(paid_creation):
    from sunrise_infrastructure.db import Notification as Row

    db, panel, commerce, config, state, _, aid, sid, _ = paid_creation
    commerce.provisioning.run_jobs()
    state["user"]["expireAt"] = (now() + timedelta(hours=12)).isoformat()
    commerce.dashboard(aid, refresh_panel=True)
    scheduler = Scheduler(commerce, config)
    assert scheduler.remind() == 1
    # The term moves (a renewal); the same bucket for the new expiry is a new reminder.
    state["user"]["expireAt"] = (now() + timedelta(hours=11)).isoformat()
    commerce.dashboard(aid, refresh_panel=True)
    assert scheduler.remind() == 1
    assert len(set(expiry_notices(db, aid))) == 2
    # An old reminder of the same bucket from a previous term does not silence a stale candidate.
    with db.transaction() as session:
        for row in session.scalars(select(Row).where(Row.account_id == aid, Row.kind == "subscription_expiry")):
            row.created_at = now() - timedelta(days=20)
    state["user"]["expireAt"] = (now() + timedelta(hours=10)).isoformat()
    commerce.dashboard(aid, refresh_panel=True)
    make_stale(db, sid)
    assert scheduler.remind() == 1
    assert len(set(expiry_notices(db, aid))) == 3


def test_active_automatic_renewal_gets_no_manual_reminder(paid_creation):
    from decimal import Decimal

    from sunrise_infrastructure.db import Agreement

    db, panel, commerce, config, state, _, aid, sid, _ = paid_creation
    commerce.provisioning.run_jobs()
    state["user"]["expireAt"] = (now() + timedelta(hours=12)).isoformat()
    commerce.dashboard(aid, refresh_panel=True)
    with db.transaction() as session:
        session.add(
            Agreement(account_id=aid, subscription_id=sid, state="active", consent_amount=Decimal("99"), currency="RUB")
        )
    assert Scheduler(commerce, config).remind() == 0
    assert expiry_notices(db, aid) == []


def test_one_failing_subscription_does_not_stop_the_worker_tick(paid_creation, monkeypatch):
    db, panel, commerce, config, state, _, aid, sid, _ = paid_creation
    commerce.provisioning.run_jobs()
    state["user"]["expireAt"] = (now() + timedelta(hours=12)).isoformat()
    commerce.dashboard(aid, refresh_panel=True)

    def broken(*args, **kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr("sunrise_infrastructure.renewal_reminders.reminder_content", broken)
    assert Scheduler(commerce, config).remind() == 0
    assert expiry_notices(db, aid) == []


def make_stale(db, sid):
    from sunrise_infrastructure.db import PanelObservation

    with db.transaction() as session:
        for observation in session.scalars(
            select(PanelObservation).where(PanelObservation.subscription_id == sid)
        ):
            observation.observed_at = now() - timedelta(hours=1)


def counting(panel):
    original, calls = panel.client._transport, []

    def handler(request):
        calls.append(request.method)
        return original.handle_request(request)

    panel.client._transport = httpx.MockTransport(handler)
    return calls


def test_stale_due_reminder_refreshes_only_its_own_user_once_and_is_sent(paid_creation):
    db, panel, commerce, config, state, _, aid, sid, _ = paid_creation
    commerce.provisioning.run_jobs()
    state["user"]["expireAt"] = (now() + timedelta(hours=12)).isoformat()
    commerce.dashboard(aid, refresh_panel=True)
    make_stale(db, sid)
    calls = counting(panel)
    scheduler = Scheduler(commerce, config)
    assert scheduler.remind() == 1
    assert calls and set(calls) == {"GET"}
    sent = len(calls)
    assert scheduler.remind() == 0
    make_stale(db, sid)
    assert scheduler.remind() == 0
    assert len(calls) == sent  # already notified for this bucket: no further reads
    with db.transaction() as session:
        notice = session.scalar(
            select(Notification).where(Notification.account_id == aid, Notification.kind == "subscription_expiry")
        )
        assert "Продлите подписку до" in notice.body and "чтобы не потерять доступ" in notice.body


def test_failed_refresh_sends_nothing_and_retries_at_most_every_30_minutes(paid_creation):
    db, panel, commerce, config, state, _, aid, sid, _ = paid_creation
    commerce.provisioning.run_jobs()
    state["user"]["expireAt"] = (now() + timedelta(hours=12)).isoformat()
    commerce.dashboard(aid, refresh_panel=True)
    make_stale(db, sid)
    with db.transaction() as session:
        count = session.scalar(select(func.count()).select_from(Quote))
    calls = []

    def failing(request):
        calls.append(request.method)
        return httpx.Response(503, json={"message": "unavailable"})

    panel.client._transport = httpx.MockTransport(failing)
    scheduler = Scheduler(commerce, config)
    assert scheduler.remind() == 0
    attempts = len(calls)
    assert attempts >= 1 and set(calls) == {"GET"}
    assert scheduler.remind() == 0
    assert len(calls) == attempts
    assert scheduler.remind(now() + timedelta(minutes=31)) == 0
    assert len(calls) > attempts
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Quote)) == count
        assert (
            session.scalar(
                select(func.count())
                .select_from(Notification)
                .where(Notification.account_id == aid, Notification.kind == "subscription_expiry")
            )
            == 0
        )


def test_reminder_is_sent_even_when_renewal_cannot_be_priced(paid_creation, monkeypatch):
    from sunrise_core.errors import DomainError

    db, panel, commerce, config, state, _, aid, sid, _ = paid_creation
    commerce.provisioning.run_jobs()
    state["user"]["expireAt"] = (now() + timedelta(hours=12)).isoformat()
    commerce.dashboard(aid, refresh_panel=True)

    def unpriced(*args, **kwargs):
        raise DomainError("valuation_unknown", "Нужна проверка владельцем.", 409)

    monkeypatch.setattr(commerce, "_renewal_offer", unpriced)
    assert Scheduler(commerce, config).remind() == 1
    with db.transaction() as session:
        notice = session.scalar(
            select(Notification).where(Notification.account_id == aid, Notification.kind == "subscription_expiry")
        )
        assert notice.actions[0]["metadata"] == {"subscription_id": sid}
        assert "Продление на" not in notice.body
