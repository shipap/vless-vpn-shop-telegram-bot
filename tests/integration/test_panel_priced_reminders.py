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
        assert quote.snapshot["currency"] in notice.body
        assert session.scalar(select(func.count()).select_from(Order)) == before_orders
        assert session.get(Account, aid).bonus_reserved == before_reserved


def test_stale_reminder_neither_polls_nor_allocates_quote(paid_creation):
    db, panel, commerce, config, state, _, aid, sid, _ = paid_creation
    commerce.provisioning.run_jobs()
    state["user"]["expireAt"] = (now() + timedelta(hours=12)).isoformat()
    commerce.dashboard(aid, refresh_panel=True)
    from sunrise_infrastructure.db import PanelObservation

    with db.transaction() as session:
        for observation in session.scalars(
            select(PanelObservation).where(PanelObservation.subscription_id == sid)
        ):
            observation.observed_at = now() - timedelta(hours=1)
        count = session.scalar(select(func.count()).select_from(Quote))
    panel.client._transport = httpx.MockTransport(
        lambda request: (_ for _ in ()).throw(AssertionError("stale fleet GET"))
    )
    assert Scheduler(commerce, config).remind() == 0
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Quote)) == count
