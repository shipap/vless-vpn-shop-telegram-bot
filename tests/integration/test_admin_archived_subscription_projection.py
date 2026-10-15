"""Confirmed archive hides operational rows while retaining money and audit history."""

import pytest
from sqlalchemy import select
from sunrise_infrastructure.admin import Admin
from sunrise_infrastructure.bot_admin import BotAdmin
from sunrise_infrastructure.db import (
    Device,
    Notification,
    Order,
    Payment,
    Segment,
    Setting,
    Subscription,
    now,
)
from sunrise_infrastructure.panel_events import PanelEvents

from tests.integration.test_node_news_and_crm import routed as routed
from tests.integration.test_subscription_engagement import paid_creation as paid_creation
from tests.integration.test_v11_panel_events import owned_panel as owned_panel
from tests.integration.test_v11_panel_events import signed_event


@pytest.mark.parametrize("projection", ["list", "website_detail", "bot_detail"])
@pytest.mark.parametrize("corrupt_marker", [False, True])
def test_archived_admin_rows_are_not_operational_and_history_survives(routed, projection, corrupt_marker):
    context, _ = routed
    db, panel, commerce, config, state, _, aid, sid, _ = context
    with db.transaction() as session:
        session.add(Device(subscription_id=sid, name="Synthetic archived device"))
        periods = [
            (row.id, row.starts_at, row.ends_at, row.paid_value) for row in session.scalars(select(Segment))
        ]
        payments = [row.id for row in session.scalars(select(Payment))]
        orders = [row.id for row in session.scalars(select(Order))]
        original_identity = session.get(Subscription, sid).panel_user_id
        other_subscriptions = list(
            session.scalars(
                select(Subscription).where(Subscription.account_id == aid, Subscription.id != sid)
            )
        )
        other_ids = {row.id for row in other_subscriptions}
        other_active = sum(row.status == "active" and row.expires_at > now() for row in other_subscriptions)
    events = PanelEvents(db, panel, config)
    assert events.accept(*signed_event("user.deleted"))["status"] == "accepted"
    state["user"] = None
    assert events.reconcile_pending() == 1
    if corrupt_marker:
        with db.transaction() as session:
            session.get(Setting, "archive:" + sid).value = {"invalid": "synthetic archive corruption"}
    admin = Admin(commerce, None)
    if projection == "list":
        result = admin.list_items("subscriptions", search=sid)
        assert result["items"] == [] and result["total"] == 0
    elif projection == "website_detail":
        result = admin.user_detail(aid, aid, config)
        assert {row["id"] for row in result["subscriptions"]} == other_ids and result["devices"] == []
        assert result["active_subscriptions_total"] == other_active
        assert result["orders"] and result["payments"]
        assert any(row["action"] == "subscription_archive" for row in result["audit"])
    else:
        result = BotAdmin(admin, config).bot_user_detail(aid, aid)
        assert {row["id"] for row in result["subscriptions"]["items"]} == other_ids
        assert result["subscriptions"]["total"] == len(other_ids)
        assert result["devices"]["items"] == [] and result["devices"]["total"] == 0
        assert result["active_subscriptions_total"] == other_active
        assert result["orders"]["items"] and result["payments"]["items"]
        assert any(row["action"] == "subscription_archive" for row in result["audit"]["items"])
    with db.transaction() as session:
        assert session.get(Subscription, sid).panel_user_id == original_identity
        assert [
            (row.id, row.starts_at, row.ends_at, row.paid_value) for row in session.scalars(select(Segment))
        ] == periods
        assert [row.id for row in session.scalars(select(Payment))] == payments
        assert [row.id for row in session.scalars(select(Order))] == orders
        assert (
            session.scalar(select(Notification).where(Notification.kind == "subscription_deleted_owner"))
            is not None
        )
