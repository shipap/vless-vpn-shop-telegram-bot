"""Renewal confirmation follows accepted payment and exact verified provisioning."""

from sqlalchemy import select
from sunrise_infrastructure.db import Notification, Order, Subscription
from sunrise_infrastructure.scheduler import Scheduler

from tests.integration.test_subscription_engagement import paid_creation as paid_creation
from tests.integration.test_v11_panel_events import owned_panel as owned_panel


def test_initial_paid_purchase_does_not_create_access_ready_notice(paid_creation):
    db, _, commerce, config, _, _, _, _, order_id = paid_creation
    assert commerce.provisioning.run_jobs() == 1
    scheduler = Scheduler(commerce, config)
    scheduler.notify_ready()
    scheduler.notify_ready()
    with db.transaction() as session:
        assert session.scalar(select(Notification.id).where(Notification.kind == "access_ready")) is None
        assert session.scalar(select(Notification.id).where(Notification.event_key == "payment:" + order_id))
        assert session.get(Order, order_id).status == "paid"


def test_paid_renewal_notice_uses_applied_new_expiry_once(paid_creation):
    db, _, commerce, config, _, _, aid, sid, _ = paid_creation
    commerce.provisioning.run_jobs()
    quote = commerce.quote(aid, "standard", 30, "renew", sid)
    order = commerce.create_order(aid, quote["id"], "actual-renewal-confirmation")
    commerce.test_pay(aid, order["id"])
    scheduler = Scheduler(commerce, config)
    scheduler.notify_ready()
    with db.transaction() as session:
        assert (
            session.scalar(
                select(Notification.id).where(Notification.event_key == "access-ready:" + order["id"])
            )
            is None
        )
    assert commerce.provisioning.run_jobs() == 1
    with db.transaction() as session:
        from sunrise_infrastructure.db import Job, Setting

        job = session.scalar(select(Job).where(Job.key == "provision:" + order["id"]))
        sub = session.get(Subscription, sid)
        record = session.get(Setting, "panel_test:" + sid)
        assert job.payload.get("applied_binding") == record.value.get("binding")
        assert job.payload.get("version") == sub.version
    scheduler.notify_ready()
    scheduler.notify_ready()
    with db.transaction() as session:
        items = list(
            session.scalars(
                select(Notification).where(Notification.event_key == "access-ready:" + order["id"])
            )
        )
        assert len(items) == 1 and items[0].kind == "subscription_renewed"
        assert session.get(Subscription, sid).expires_at.isoformat() in items[0].body
        assert session.get(Order, order["id"]).status == "paid"


def test_archived_paid_order_never_advertises_ready(paid_creation):
    from sunrise_infrastructure.db import Setting

    db, _, commerce, config, _, _, aid, sid, order_id = paid_creation
    commerce.provisioning.run_jobs()
    # An unverified archival marker is a fail-closed fence, never readiness proof.
    with db.transaction() as session:
        session.add(Setting(key="archive:" + sid, value={"unverified": True}))
    Scheduler(commerce, config).notify_ready()
    with db.transaction() as session:
        assert (
            session.scalar(
                select(Notification.id).where(Notification.event_key == "access-ready:" + order_id)
            )
            is None
        )
