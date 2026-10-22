"""Новые конечные условия после истечения NULL не восстанавливают старые права."""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sunrise_core.errors import DomainError
from sunrise_infrastructure.db import Job, Order, Payment, Quote, Segment, Setting, Subscription, now
from sunrise_infrastructure.panel_entitlements import override_key
from sunrise_infrastructure.panel_v3 import production_binding_key

from tests.integration.test_legacy_catalog_valuation import (
    fulfill,
)
from tests.integration.test_legacy_catalog_valuation import (
    legacy_catalog as legacy_catalog,
)
from tests.integration.test_production_import_authority import imported_cleanup as imported_cleanup


def prepare_null(context, *, expired):
    db, _, state, sid, _, _ = context
    expiry = now() + timedelta(days=-1 if expired else 1)
    with db.transaction() as tx:
        sub = tx.get(Subscription, sid)
        sub.devices_limit, sub.expires_at = None, expiry
        sub.status = "expired" if expired else "active"
        root = tx.get(Setting, production_binding_key(sid))
        root.value = root.value | {
            "capacity_evidence": {
                "kind": "explicit_null_preserved",
                "present": True,
                "source": "authenticated_panel_snapshot",
                "until": expiry.isoformat(),
            }
        }
        marker = tx.get(Setting, override_key(sid))
        marker.value = {key: value for key, value in marker.value.items() if key != "capacity_evidence"} | {
            "devices_limit": None,
            "expires_at": expiry.isoformat(),
            "status": "ACTIVE",
        }
    state["user"].update(
        hwidDeviceLimit=None, expireAt=expiry.isoformat(), status="EXPIRED" if expired else "ACTIVE"
    )


def test_expired_damaged_null_quotes_catalog_finite_terms_and_only_creates_new_paid_segment(legacy_catalog):
    db, commerce, state, sid, aid, _ = legacy_catalog
    prepare_null(legacy_catalog, expired=True)
    before = commerce.dashboard(aid, refresh_panel=True)["subscriptions"][0]
    assert before["renewal_pricing_error"] == "panel_capacity_unpriced"
    assert before["renewal_device_limit_required"] is True
    offer = commerce.renewal_offer(aid, sid, payment_method="sbp")
    assert offer["devices_limit"] == 4 and offer["total"] == "100.00"
    with db.transaction() as tx:
        assert list(tx.scalars(select(Segment))) == []
        assert list(tx.scalars(select(Order))) == []
        assert list(tx.scalars(select(Payment))) == []
        assert tx.get(Subscription, sid).devices_limit is None
        assert "capacity_evidence" not in tx.get(Setting, override_key(sid)).value
        original_uuid = tx.get(Subscription, sid).panel_uuid
        root_evidence = tx.get(Setting, production_binding_key(sid)).value["capacity_evidence"]
    order = fulfill(legacy_catalog, offer["id"], "explicit-finite-renewal")
    with db.transaction() as tx:
        sub = tx.get(Subscription, sid)
        segment = tx.scalar(select(Segment))
        saved = tx.get(Quote, offer["id"]).snapshot
        assert segment.kind == "paid" and segment.order_id == order["id"]
        assert segment.devices == sub.devices_limit == saved["devices_limit"] == 4
        assert segment.paid_value == Decimal("100")
        assert sub.panel_uuid == original_uuid and sub.panel_user_id == 8
        assert tx.get(Setting, production_binding_key(sid)).value["capacity_evidence"] == root_evidence
        payload = tx.scalar(select(Job).where(Job.key == "provision:" + order["id"])).payload
        assert payload["devices_limit"] == 4 and "preserve_null_capacity" not in payload


@pytest.mark.parametrize("action", ["renew", "devices", "upgrade", "downgrade"])
def test_active_damaged_null_never_receives_finite_replacement_or_order(legacy_catalog, action):
    db, commerce, _, sid, aid, _ = legacy_catalog
    prepare_null(legacy_catalog, expired=False)
    before = commerce.dashboard(aid, refresh_panel=True)["subscriptions"][0]
    assert before["renewal_device_limit_required"] is False
    with pytest.raises(DomainError) as blocked:
        commerce.quote(aid, "standard", 30, action, sid, payment_method="sbp", extra_devices=1)
    assert blocked.value.code == "panel_capacity_unpriced"
    with db.transaction() as tx:
        assert tx.get(Subscription, sid).devices_limit is None
        assert list(tx.scalars(select(Order))) == []
        assert list(tx.scalars(select(Segment))) == []
