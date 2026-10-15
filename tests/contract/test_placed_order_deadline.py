"""Only authenticated, aware merchant dates can extend a placed live order window."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sunrise_core.errors import DomainError
from sunrise_infrastructure.billing import Billing


@pytest.mark.parametrize(
    "change",
    ["boolean", "missing_created", "missing_expiry", "naive", "before_order", "future_created", "reversed"],
)
def test_merchant_window_rejects_unproven_or_impossible_dates(change, monkeypatch):
    import sunrise_infrastructure.billing as billing_module

    moment = datetime(2026, 10, 12, 12, tzinfo=UTC)
    monkeypatch.setattr(billing_module, "now", lambda: moment)
    order = SimpleNamespace(created_at=moment - timedelta(seconds=1))
    data = {"created_at": moment.isoformat(), "expires_at": (moment + timedelta(minutes=30)).isoformat()}
    if change == "boolean":
        data["created_at"] = True
    elif change == "missing_created":
        data.pop("created_at")
    elif change == "missing_expiry":
        data.pop("expires_at")
    elif change == "naive":
        data["created_at"] = moment.replace(tzinfo=None).isoformat()
    elif change == "before_order":
        data["created_at"] = (moment - timedelta(days=1)).isoformat()
    elif change == "future_created":
        data["created_at"] = (moment + timedelta(seconds=1)).isoformat()
    else:
        data["expires_at"] = moment.isoformat()
    with pytest.raises(DomainError) as denied:
        Billing._merchant_window(data, order)
    assert denied.value.code == "payment_deadline_review"


def test_unknown_window_is_not_fabricated_and_second_precision_preserves_chronology(monkeypatch):
    import sunrise_infrastructure.billing as billing_module

    moment = datetime(2026, 10, 12, 12, 0, 2, tzinfo=UTC)
    monkeypatch.setattr(billing_module, "now", lambda: moment)
    order = SimpleNamespace(created_at=moment - timedelta(seconds=1, microseconds=500000))
    assert Billing._merchant_window({}, order) == {}
    created = order.created_at.replace(microsecond=0)
    data = {"created_at": created.isoformat(), "expires_at": (created + timedelta(minutes=30)).isoformat()}
    assert Billing._merchant_window(data, order) == data
