import os
from uuid import uuid4

import fakeredis
import pytest
from sqlalchemy import select
from sunrise_core.errors import DomainError
from sunrise_infrastructure.admin import Admin
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import Account, Audit, Database, Device, Setting, now
from sunrise_infrastructure.recurring import Recurring
from sunrise_infrastructure.security import RateGuard

from tests.legal_fixture import accept_test_legal


class Provider:
    terminal = "synthetic-maintenance"
    recurring_authorized = True

    def plans(self):
        return {
            "items": [
                {"id": "standard", "merchant_amount_rub": "99", "payer_amount_rub": "99", "interval": "month"}
            ]
        }

    def create_agreement(self, body, key):
        return {"id": "maintenance-" + key, "plan_id": body["plan_id"], "billing_status": "enabled"}

    def stop_agreement(self, pid):
        return {"state": "stop", "billing_status": "stopped"}


def test_maintenance_blocks_new_operations_preserves_paid_recovery_recurring_and_cancel():
    url = os.getenv("SUNRISE_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Synthetic PostgreSQL required")
    c = Commerce(Database(url), True)
    admin = Admin(c, RateGuard(fakeredis.FakeRedis()))
    r = Recurring(c, Provider())
    with c.db.transaction() as s:
        a = Account(email=f"{uuid4()}@example.test")
        s.add(a)
        s.flush()
        aid = a.id
        old_recurring = s.get(Setting, "recurring").value
        s.get(Setting, "recurring").value = {"enabled": True}
        row = s.get(Setting, "maintenance")
        old_maintenance = row.value if row else {"enabled": False}
        if row:
            row.value = {"enabled": False}
    accept_test_legal(Commerce(c.db), a.id)
    try:
        quote = c.quote(aid, "standard", 30, "purchase")
        order = c.create_order(aid, quote["id"], str(uuid4()))
        c.test_pay(aid, order["id"])
        sid = c.dashboard(aid)["subscriptions"][0]["id"]
        with c.db.transaction() as s:
            device = Device(subscription_id=sid, name="Тестовое устройство")
            s.add(device)
            s.flush()
            did = device.id
        ag = r.enroll(aid, sid, "standard", True, str(uuid4()))
        extra = c.quote(aid, "standard", 30, "purchase")
        awaiting = c.create_order(aid, extra["id"], str(uuid4()))
        unaccepted = c.quote(aid, "standard", 30, "purchase")
        assert admin.maintenance(aid, True, "Проверка режима обслуживания")["enabled"] is True
        for action in (
            lambda: c.quote(aid, "standard", 30, "purchase"),
            lambda: c.create_order(aid, unaccepted["id"], str(uuid4())),
            lambda: c.quote(aid, "universal", 30, "upgrade", sid),
            lambda: c.quote(aid, "standard", 30, "devices", sid, extra_devices=1),
            lambda: c.revoke_device(aid, sid, did),
            lambda: admin.compensate(aid, sid, 1, "Проверка новых прав"),
        ):
            with pytest.raises(DomainError) as exc:
                action()
            assert exc.value.code == "maintenance" and exc.value.status == 503
        # An already accepted order and a paid provider cycle retain delivery during maintenance.
        assert c.test_pay(aid, awaiting["id"])["status"] == "paid"
        payment = {
            "payment_id": "maintenance-" + str(uuid4()),
            "amount": "99",
            "payment_currency": "RUB",
            "status": "paid",
            "subscription_id": ag["provider_id"],
            "paid_at": now().isoformat(),
            "test": True,
            "terminal_id": r.provider.terminal,
        }
        assert (
            r.service_cycle(
                payment,
                {
                    "cycle_number": 1,
                    "payment_id": payment["payment_id"],
                    "status": "payed",
                    "validation_status": "accepted",
                },
            )["status"]
            == "paid"
        )
        assert r.cancel(aid, sid)["state"] == "stopped"
        with pytest.raises(DomainError) as exc:
            r.enroll(aid, sid, "standard", True, str(uuid4()))
        assert exc.value.code == "maintenance"
        assert c.dashboard(aid)["subscriptions"]
        assert admin.maintenance(aid, False, "Завершение обслуживания")["enabled"] is False
        assert c.quote(aid, "standard", 30, "renew", sid)["total"] == "99.00"
        with c.db.transaction() as s:
            audit = list(
                s.scalars(
                    select(Audit)
                    .where(Audit.actor_id == aid, Audit.action == "maintenance")
                    .order_by(Audit.created_at)
                )
            )
            assert [x.details["after"]["enabled"] for x in audit] == [True, False]
            assert (
                audit[0].details["before"]["enabled"] is False
                and audit[1].details["before"]["enabled"] is True
            )
    finally:
        with c.db.transaction() as s:
            s.get(Setting, "recurring").value = old_recurring
            row = s.get(Setting, "maintenance")
            if row:
                row.value = old_maintenance
