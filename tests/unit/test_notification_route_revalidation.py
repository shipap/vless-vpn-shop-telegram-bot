"""The final send boundary retains the same single-channel broadcast policy as claim."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sunrise_core.errors import DomainError
from sunrise_infrastructure.db import (
    Account,
    Job,
    Notification,
    NotificationDelivery,
    NotificationPreference,
    PushSubscription,
)
from sunrise_infrastructure.notifications import NotificationService


@pytest.mark.parametrize("base", [None, 42])
def test_non_string_dashboard_url_does_not_break_notification_delivery(base):
    service = NotificationService(None, SimpleNamespace(_runtime_policy=None, dashboard_url=base))
    assert service._action_links({}) == []


@pytest.mark.parametrize(
    "channel,has_telegram,media_self,denied",
    [
        ("email", True, False, True),
        ("email", False, False, False),
        ("email", True, True, False),
        ("push", False, False, True),
    ],
)
def test_final_broadcast_route_rechecks_current_contact(channel, has_telegram, media_self, denied):
    account = Account(
        id=1, telegram_id=123 if has_telegram else None, email="owner@synthetic.test", email_verified=True
    )
    item = Notification(
        id="notice", account_id=account.id, kind="broadcast", audience="account", event_key="broadcast:job"
    )
    recipient = account.email if channel == "email" else "push-id"
    delivery = NotificationDelivery(
        id="delivery", notification_id=item.id, channel=channel, recipient=recipient, state="in_flight"
    )
    rows = {
        (Account, account.id): account,
        (Notification, item.id): item,
        (NotificationDelivery, delivery.id): delivery,
        (NotificationPreference, account.id): SimpleNamespace(browser_enabled=True),
        (PushSubscription, "push-id"): SimpleNamespace(account_id=account.id, disabled_at=None),
        (Job, "job"): SimpleNamespace(account_id=account.id, payload={"source": "media_self"})
        if media_self
        else None,
    }

    @contextmanager
    def transaction():
        yield SimpleNamespace(get=lambda model, key, **_: rows.get((model, key)))

    service = NotificationService(
        SimpleNamespace(transaction=transaction),
        SimpleNamespace(
            email_allowlist=[account.email],
            push_test_authorized=True,
            admin_telegram_allowlist=[],
            owner_id=None,
        ),
    )
    assert service._allowed(
        SimpleNamespace(get=lambda model, key, **_: rows.get((model, key))), account, delivery
    )
    if denied:
        with pytest.raises(DomainError):
            service._validate_delivery(
                channel, recipient, {"notification_id": item.id, "audience": "account"}, delivery.id
            )
    else:
        assert service._validate_delivery(
            channel, recipient, {"notification_id": item.id, "audience": "account"}, delivery.id
        ) == (None, None)
