"""Inbox action metadata cannot route to another customer's subscription or quote."""

import pytest
from sunrise_infrastructure.notifications import NotificationService

from tests.integration.test_subscription_engagement import paid_creation as paid_creation
from tests.integration.test_v11_panel_events import owned_panel as owned_panel


def test_notification_exposes_only_exact_owned_saved_renewal_action(paid_creation):
    db, _, commerce, config, _, _, aid, sid, _ = paid_creation
    commerce.provisioning.run_jobs()
    quote = commerce.quote(aid, "standard", 30, "renew", sid)
    actions = [
        {
            "label": "Продлить",
            "target": "/#subscriptions",
            "operation": "renew",
            "metadata": {"subscription_id": sid, "quote_id": quote["id"]},
        }
    ]
    service = NotificationService(db, config)
    with db.transaction() as session:
        service.emit_in(
            session,
            aid,
            "owned-renewal-action",
            "expiry",
            "Продление",
            "Срок заканчивается.",
            actions=actions,
            channel="inbox",
        )
    assert service.list(aid)["items"][0]["actions"] == actions


@pytest.mark.parametrize("wrong", ["subscription", "quote", "operation", "url"])
def test_untrusted_persisted_notification_action_is_not_exposed(paid_creation, wrong):
    db, _, commerce, config, _, _, aid, sid, _ = paid_creation
    commerce.provisioning.run_jobs()
    quote = commerce.quote(aid, "standard", 30, "renew", sid)
    action = {
        "label": "Продлить",
        "target": "/#subscriptions",
        "operation": "renew",
        "metadata": {"subscription_id": sid, "quote_id": quote["id"]},
    }
    if wrong in {"subscription", "quote"}:
        action["metadata"][wrong + "_id"] = "foreign"
    elif wrong == "operation":
        action["operation"] = "delete"
    else:
        action["target"] = "https://foreign.example.test/"
    service = NotificationService(db, config)
    with db.transaction() as session:
        item = service.emit_in(
            session, aid, "untrusted-action", "expiry", "Продление", "Срок заканчивается.", channel="inbox"
        )
        item.actions = [action]
    assert service.list(aid)["items"][0]["actions"] == []
