from uuid import uuid4

import fakeredis
import pytest
from sqlalchemy import select
from sunrise_core.errors import DomainError
from sunrise_infrastructure.admin import Admin
from sunrise_infrastructure.billing import Billing
from sunrise_infrastructure.db import Job, Product, Quote, Segment, Setting
from sunrise_infrastructure.security import RateGuard
from test_v6_legal import legal_context as legal_context


def test_refund_disable_preserves_snapshot_after_catalog_edit(legal_context):
    from sunrise_infrastructure.db import Payment, RefundReview, Subscription, now

    c, aid, documents = legal_context
    c.accept_legal(aid, documents, "website", "checkout")
    original_mapping = str(uuid4())
    with c.db.transaction() as s:
        pid = str(uuid4())
        s.add(
            Product(
                included_devices=4,
                currency="RUB",
                id=pid,
                code=pid,
                name="Тест отзыва",
                tier_level=1,
                price=99,
                internal_squad_uuid=original_mapping,
            )
        )
    q = c.quote(aid, pid, 30, "purchase")
    order = c.create_order(aid, q["id"], str(uuid4()))
    c.test_pay(aid, order["id"])
    with c.db.transaction() as s:
        payment = s.scalar(select(Payment).where(Payment.order_id == order["id"]))
        segment = s.scalar(select(Segment).where(Segment.order_id == order["id"]))
        version = s.get(Subscription, segment.subscription_id).version
        s.get(Product, pid).internal_squad_uuid = str(uuid4())
    Billing(c, payment.terminal, payment.environment).refund(
        payment.provider_id, str(uuid4()), payment.amount, now()
    )
    with c.db.transaction() as s:
        review_id = s.scalar(select(RefundReview.id).where(RefundReview.payment_id == payment.id))
    Admin(c, RateGuard(fakeredis.FakeRedis())).resolve_refund(
        aid, review_id, "revoke", "Проверено назначение отзыва", expected_subscription_version=version
    )
    with c.db.transaction() as s:
        job = s.scalar(select(Job).where(Job.key == "refund-review:" + review_id))
        assert job.payload["panel_status"] == "DISABLED"
        assert job.payload["internal_squad_uuid"] == original_mapping


def test_mappings_are_saved_in_paid_snapshot_and_admin_edits_do_not_reassign(legal_context, monkeypatch):
    c, aid, documents = legal_context
    c.accept_legal(aid, documents, "website", "checkout")
    admin = Admin(c, RateGuard(fakeredis.FakeRedis()))
    with c.db.transaction() as s:
        previous = s.get(Setting, "panel_mapping")
        old = previous.value if previous else None
        pid = str(uuid4())
        internal, external = str(uuid4()), str(uuid4())
        s.add(
            Product(
                included_devices=4,
                currency="RUB",
                id=pid,
                code=pid,
                name="Проверка групп",
                tier_level=1,
                price=99,
                extra_device_price=50,
                internal_squad_uuid=internal,
            )
        )
    try:
        admin.update_panel_mapping(aid, external, "sunrise_test_", "Настройка тестового проекта")
        q = c.quote(aid, pid, 30, "purchase")
        admin.update_panel_mapping(aid, str(uuid4()), "another_", "Проверка сохранения старых прав")
        order = c.create_order(aid, q["id"], str(uuid4()))
        c.test_pay(aid, order["id"])
        with c.db.transaction() as s:
            job = s.scalar(select(Job).where(Job.key == "provision:" + order["id"]))
            assert job.payload["internal_squad_uuid"] == internal
            assert job.payload["external_squad_uuid"] == external
            assert job.payload["project_prefix"] == "sunrise_test_"
            segment = s.scalar(select(Segment).where(Segment.order_id == order["id"]))
            original = segment.price_basis["panel_configuration"]
            s.get(Product, pid).internal_squad_uuid = str(uuid4())
            sid = segment.subscription_id
        extra = c.quote(aid, pid, 30, "devices", sid, extra_devices=1)
        with c.db.transaction() as s:
            snapshot = s.get(Quote, extra["id"]).snapshot
            assert snapshot["panel_mappings"][pid] == internal
            assert snapshot["panel_configuration"] == original
        admin.compensate(aid, sid, 2, "Сохранение назначения оплаченной услуги")
        with c.db.transaction() as s:
            compensation = s.scalar(
                select(Segment).where(Segment.subscription_id == sid, Segment.kind == "compensation")
            )
            assert set(compensation.price_basis) == {"panel_mappings", "panel_configuration"}
            assert compensation.price_basis["panel_mappings"][pid] == internal
            assert compensation.price_basis["panel_configuration"] == original
            boundary = compensation.starts_at
        monkeypatch.setattr("sunrise_infrastructure.commerce.now", lambda: boundary)
        c.apply_segment_boundaries()
        with c.db.transaction() as s:
            from sunrise_infrastructure.db import Subscription

            payload = c._provision_payload(s, s.get(Subscription, sid))
            assert payload["internal_squad_uuid"] == internal
            assert payload["external_squad_uuid"] == external
        with pytest.raises(DomainError):
            admin.update_panel_mapping(aid, "not-a-uuid", "sunrise_test_", "Проверка неверной группы")
        with pytest.raises(DomainError):
            admin.update_panel_mapping(aid, external, "../escape", "Проверка неверного префикса")
    finally:
        with c.db.transaction() as s:
            row = s.get(Setting, "panel_mapping")
            if old is None:
                s.delete(row)
            else:
                row.value = old
