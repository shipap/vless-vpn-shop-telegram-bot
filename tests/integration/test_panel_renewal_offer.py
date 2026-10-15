"""Explicit renewal actions use the common quote without allocating orders or reserves."""

import pytest
from sqlalchemy import func, select
from sunrise_core.errors import DomainError
from sunrise_infrastructure.db import Account, Order, Quote

from tests.integration.test_subscription_engagement import paid_creation as paid_creation
from tests.integration.test_v11_panel_events import owned_panel as owned_panel


def test_renewal_offer_reuses_saved_owned_quote_and_never_creates_order(paid_creation):
    db, _, commerce, _, _, _, aid, sid, _ = paid_creation
    assert commerce.provisioning.run_jobs() == 1
    quote = commerce.quote(aid, "standard", 30, "renew", sid)
    with db.transaction() as session:
        orders = session.scalar(select(func.count()).select_from(Order))
        balance = session.get(Account, aid).bonus_reserved
    offer = commerce.renewal_offer(aid, sid, quote_id=quote["id"])
    assert offer["id"] == quote["id"] and offer["period_days"] == 30
    assert offer["total"] == quote["total"] and offer["devices_limit"] == quote["devices_limit"]
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Order)) == orders
        assert session.get(Account, aid).bonus_reserved == balance


@pytest.mark.parametrize("wrong", ["purchase", "unknown", "foreign"])
def test_renewal_offer_does_not_accept_unrelated_quote(paid_creation, wrong):
    db, _, commerce, _, _, _, aid, sid, _ = paid_creation
    assert commerce.provisioning.run_jobs() == 1
    quote = commerce.quote(
        aid,
        "standard",
        30,
        "purchase" if wrong == "purchase" else "renew",
        None if wrong == "purchase" else sid,
    )
    if wrong == "foreign":
        with db.transaction() as session:
            owner = Account(email="foreign-offer@example.test")
            session.add(owner)
            session.flush()
            session.get(Quote, quote["id"]).account_id = owner.id
    with pytest.raises(DomainError) as denied:
        commerce.renewal_offer(aid, sid, quote_id="missing" if wrong == "unknown" else quote["id"])
    assert denied.value.status == 404


def test_explicit_renewal_offer_chooses_published_monthly_period(paid_creation):
    _, _, commerce, _, _, _, aid, sid, _ = paid_creation
    assert commerce.provisioning.run_jobs() == 1
    result = commerce.renewal_offer(aid, sid)
    assert result["action"] == "renew" and result["subscription_id"] == sid
    assert result["period_days"] == 30 and result["devices_limit"] >= 1
