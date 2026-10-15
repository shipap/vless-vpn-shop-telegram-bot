"""Only verified finite traffic limits can produce progress and threshold messages."""

import pytest
from sqlalchemy import select
from sunrise_infrastructure.db import Notification, now
from sunrise_infrastructure.panel_events import PanelEvents

from tests.integration.test_subscription_engagement import paid_creation as paid_creation
from tests.integration.test_v11_panel_events import owned_panel as owned_panel
from tests.integration.test_v11_panel_events import signed_event


@pytest.mark.parametrize("limit", [0, None, 10000])
def test_threshold_warning_is_finite_verified_and_deduplicated(paid_creation, limit):
    db, panel, commerce, config, state, _, aid, sid, _ = paid_creation
    commerce.provisioning.run_jobs()
    state["user"].update(trafficLimitBytes=limit, lastTriggeredThreshold=80, lastTrafficResetAt=None)
    state["user"]["userTraffic"]["usedTrafficBytes"] = 9000
    events = PanelEvents(db, panel, config)
    raw, headers = signed_event("user.bandwidth_usage_threshold_reached")
    events.accept(raw, headers)
    assert events.reconcile_pending() == 1
    events.accept(*signed_event("user.bandwidth_usage_threshold_reached", timestamp=now().isoformat()))
    events.reconcile_pending()
    projection = next(item for item in commerce.dashboard(aid)["subscriptions"] if item["id"] == sid)
    assert projection["traffic_progress_percent"] == ("90.00" if limit else None)
    assert projection["traffic_warning"] is bool(limit)
    with db.transaction() as session:
        notices = list(session.scalars(select(Notification).where(Notification.kind == "traffic_threshold")))
        assert len(notices) == (1 if limit else 0)
