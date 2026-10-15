from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from sunrise_core.errors import DomainError
from sunrise_infrastructure.db import Job, now
from test_bot_admin_v7 import bot_admin_v7 as bot_admin_v7


def alert(context):
    console, owner, customer = context
    with console.db.transaction() as session:
        row = Job(
            key="claim-test:" + uuid4().hex,
            kind="owner_device_unlink",
            account_id=customer,
            payload={"target_id": console.settings.owner_id, "account_id": str(customer)},
        )
        session.add(row)
        session.flush()
        identifier = row.id
    return console, owner, identifier


def test_two_dispatchers_claim_one_owner_alert_once(bot_admin_v7):
    console, owner, identifier = alert(bot_admin_v7)
    start = Barrier(2)

    def claim(_):
        start.wait(timeout=5)
        return console.claim_owner_alert(owner, identifier)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, range(2)))
    assert sum(result is not None for result in results) == 1
    with console.db.transaction() as session:
        row = session.get(Job, identifier)
        assert row.state == "in_flight" and row.attempts == 1
    assert console.pending_owner_alerts(owner)["items"] == []
    console.acknowledge_owner_alert(owner, identifier)
    assert console.claim_owner_alert(owner, identifier) is None


def test_restart_quarantines_expired_claim_without_resend(bot_admin_v7):
    console, owner, identifier = alert(bot_admin_v7)
    console.claim_owner_alert(owner, identifier)
    with console.db.transaction() as session:
        session.get(Job, identifier).available_at = now() - timedelta(seconds=1)
    assert console.pending_owner_alerts(owner)["items"] == []
    assert console.claim_owner_alert(owner, identifier) is None
    with console.db.transaction() as session:
        row = session.get(Job, identifier)
        assert (row.state, row.last_error, row.attempts) == ("review", "delivery_outcome_unknown", 1)


def test_claim_rechecks_current_owner_allowlist_and_ack_requires_claim(bot_admin_v7):
    console, owner, identifier = alert(bot_admin_v7)
    with pytest.raises(DomainError):
        console.acknowledge_owner_alert(owner, identifier)
    console.telegram_recipient_allowlist = frozenset()
    with pytest.raises(DomainError) as error:
        console.claim_owner_alert(owner, identifier)
    assert error.value.code == "recipient_denied"
    with console.db.transaction() as session:
        assert session.get(Job, identifier).state == "pending"


@pytest.mark.parametrize("state", ["review", "blocked"])
def test_failed_claim_is_never_automatically_retried(bot_admin_v7, state):
    console, owner, identifier = alert(bot_admin_v7)
    console.claim_owner_alert(owner, identifier)
    console.fail_owner_alert(owner, identifier, state=state)
    assert console.pending_owner_alerts(owner)["items"] == []
    with console.db.transaction() as session:
        assert session.get(Job, identifier).state == state


def test_definitive_429_has_bounded_retry(bot_admin_v7):
    console, owner, identifier = alert(bot_admin_v7)
    for attempt in range(1, 5):
        console.claim_owner_alert(owner, identifier)
        console.fail_owner_alert(owner, identifier, state="pending", retry_after=5)
        with console.db.transaction() as session:
            row = session.get(Job, identifier)
            assert row.attempts == attempt
            assert row.state == ("pending" if attempt <= 3 else "review")
            assert row.available_at > now()
            row.available_at = now() - timedelta(seconds=1)
    assert console.pending_owner_alerts(owner)["items"] == []
