import importlib.util

import pytest
from sqlalchemy import func, select
from sunrise_core.errors import DomainError
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import CampaignVisit, Setting
from test_v11_notifications import notification_context as notification_context


def test_web_campaign_has_real_deduplicated_producer_and_verified_attach(notification_context):
    db, settings, (owner, _, _) = notification_context
    with db.transaction() as session:
        session.add(
            Setting(
                key="utm:" + "a" * 24,
                value={
                    "name": "Синтетическая кампания",
                    "source": "test",
                    "medium": "link",
                    "campaign": "v11",
                },
            )
        )
    assert importlib.util.find_spec("sunrise_infrastructure.campaigns") is not None, (
        "Website campaign producer missing"
    )
    from sunrise_infrastructure.campaigns import Campaigns

    service = Campaigns(Commerce(db), settings)
    first = service.visit("a" * 24, "synthetic-browser-visitor")
    again = service.visit("a" * 24, "synthetic-browser-visitor")
    assert first["visit_token"] == again["visit_token"]
    assert service.attach(owner, [first["visit_token"]])["status"] == "attached"
    with pytest.raises(DomainError):
        service.attach(owner, ["tampered-token"])
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(CampaignVisit)) == 1
        assert session.scalar(select(CampaignVisit.account_id)) == owner
    assert service.stats(owner, "a" * 24)["web_unique_visitors"] == 1
