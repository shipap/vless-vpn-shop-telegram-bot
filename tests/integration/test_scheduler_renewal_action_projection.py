"""A priced scheduler reminder retains its owned renewal action through consumers."""

from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select
from sunrise_infrastructure.db import Account, Agreement, NotificationDelivery, Order, now
from sunrise_infrastructure.notifications import NotificationService
from sunrise_infrastructure.scheduler import Scheduler

from tests.integration.test_subscription_engagement import paid_creation as paid_creation
from tests.integration.test_v11_panel_events import owned_panel as owned_panel


@pytest.mark.parametrize("projection", ["inbox", "delivery"])
def test_scheduler_renewal_action_survives_real_owned_projection(paid_creation, projection):
    db, panel, commerce, config, state, _, aid, sid, _ = paid_creation
    commerce.provisioning.run_jobs()
    state["user"]["expireAt"] = (now() + timedelta(hours=12)).isoformat()
    commerce.dashboard(aid, refresh_panel=True)
    with db.transaction() as session:
        before = (
            session.scalar(select(func.count()).select_from(Order)),
            session.scalar(select(func.count()).select_from(Agreement)),
            session.get(Account, aid).bonus_reserved,
        )
        for delivery in session.scalars(select(NotificationDelivery)):
            delivery.state = "cancelled"
    panel.client._transport = httpx.MockTransport(
        lambda request: (_ for _ in ()).throw(AssertionError("Reminder must not poll panel"))
    )
    assert Scheduler(commerce, config).remind() == 1
    captured = []
    service = NotificationService(
        db, config, telegram_transport=lambda recipient, payload, identifier: captured.append(payload)
    )
    if projection == "inbox":
        actions = next(item for item in service.list(aid)["items"] if item["kind"] == "subscription_expiry")[
            "actions"
        ]
    else:
        assert service.deliver_pending() == 1
        actions = captured[0]["actions"]
    assert len(actions) == 1
    assert actions[0]["operation"] == "renew"
    assert actions[0]["metadata"]["subscription_id"] == sid
    assert actions[0]["metadata"]["quote_id"]
    with db.transaction() as session:
        assert (
            session.scalar(select(func.count()).select_from(Order)),
            session.scalar(select(func.count()).select_from(Agreement)),
            session.get(Account, aid).bonus_reserved,
        ) == before
