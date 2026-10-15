from decimal import Decimal as D
from uuid import uuid4

import fakeredis
import pytest
from sqlalchemy import select
from sunrise_core.errors import DomainError
from sunrise_infrastructure.admin import Admin
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import Account, Ledger
from sunrise_infrastructure.security import RateGuard

from tests.isolated_database import isolated_database
from tests.legal_fixture import accept_test_legal


@pytest.fixture
def admin():
    with isolated_database("admin_fixture") as db:
        c = Commerce(db, True)
        with c.db.transaction() as s:
            a = Account(email=f"{uuid4()}@example.test")
            s.add(a)
            s.flush()
            aid = a.id
        accept_test_legal(Commerce(c.db), a.id)
        yield Admin(c, RateGuard(fakeredis.FakeRedis())), aid


def test_reasoned_bonus_adjustment_is_ledgered_once(admin):
    service, aid = admin
    key = str(uuid4())
    service.adjust_bonus(aid, aid, "10.00", key, "Компенсация теста")
    service.adjust_bonus(aid, aid, "10.00", key, "Компенсация теста")
    with service.db.transaction() as s:
        assert s.get(Account, aid).bonus_balance == D(10)
        assert len(list(s.scalars(select(Ledger).where(Ledger.account_id == aid)))) == 1


def test_content_version_audit_and_discount_edit_validate(admin):
    service, aid = admin
    name = "test_" + str(uuid4())
    service.save_content(aid, name, [{"type": "text", "text": "<не HTML>"}], "Обновление инструкции")
    assert any(x["id"] == "content:" + name for x in service.items("content"))
    with pytest.raises(DomainError):
        service.save_discounts(aid, {"cap": "101"}, "Ошибочный лимит")


def test_paginated_search_payment_lookup_and_safe_user_detail(admin):
    service, aid = admin
    detail = service.user_detail(str(aid))
    result = service.list_items("users", detail["account"]["email"], 1, 1)
    assert result["total"] == 1 and result["items"][0]["id"] == str(aid)
    assert set(detail) >= {"identities", "sessions", "ledger", "payments", "subscriptions", "audit"}
    assert "password_hash" not in str(detail) and "csrf" not in str(detail)
    with pytest.raises(DomainError):
        service.payment_detail("missing-synthetic-payment")


def test_content_versions_broadcast_controls_and_maintenance(admin):
    service, aid = admin
    key = "version_" + str(uuid4())
    nodes = [{"type": "text", "text": "Проверка"}]
    service.save_content(aid, key, nodes, "Первая версия")
    service.save_content(aid, key, nodes, "Вторая версия")
    assert [x["version"] for x in service.content_versions(key)] == [2, 1]
    email = service.user_detail(aid)["account"]["email"]
    preview = service.estimate_broadcast(nodes, [email, "denied@example.test"], [email])
    assert preview == {"recipient_count": 2, "allowed_count": 1, "denied_count": 1, "allowed": False}
    job = service.broadcast(aid, nodes, [email], [email], "Локальная проверка")
    assert service.broadcast_detail(job["id"])["recipient_count"] == 1
    assert service.stop_broadcast(aid, job["id"], "Остановка теста")["state"] == "stopped"
    assert service.maintenance(aid, True, "Тест обслуживания")["enabled"] is True
    health = service.health()
    assert health["database"]["state"] == "ok" and health["backup"]["state"] == "not_verified"
    service.maintenance(aid, False, "Завершение теста")


def test_calculator_shared_sequential_discounts_unknown_fee_and_impact_no_write(admin):
    from sunrise_infrastructure.db import Product

    service, aid = admin
    with service.db.transaction() as s:
        p = Product(
            included_devices=4,
            currency="RUB",
            id=str(uuid4()),
            code=str(uuid4()),
            name="Расчёт",
            tier_level=1,
            price=D(100),
        )
        s.add(p)
        s.flush()
        pid = p.id
    preview = service.discount_preview(
        {
            "product_id": pid,
            "period_days": 30,
            "period_discount_percent": "20",
            "secondary_percent": "20",
            "cap": "40",
            "bonus_cap": "50",
            "bonus_amount": "99",
        }
    )
    assert (
        D(preview["discounted"]) == D(64) and D(preview["bonus"]) == D(32) and D(preview["external"]) == D(32)
    )
    assert preview["fee"] == {"source": "unknown", "amount": None}
    impact = service.catalog_impact(pid, {"price": "120.00"})
    assert impact["before"]["price"] == "100.00" and impact["after"]["price"] == "120.00"
    with service.db.transaction() as s:
        assert s.get(Product, pid).price == D(100)


def test_campaign_eligibility_referral_welcome_once_and_owner_policy_audit(admin):
    from sunrise_infrastructure.db import Audit, Setting

    service, aid = admin
    c = service.commerce
    with c.db.transaction() as s:
        old = s.get(Setting, "discounts").value
        referrer = Account(email=f"{uuid4()}@example.test")
        s.add(referrer)
        s.flush()
        s.get(Account, aid).referrer_id = referrer.id
    accept_test_legal(Commerce(c.db), referrer.id)
    values = {
        "loyalty_period_days": 31,
        "loyalty_milestones": [{"elapsed_periods": 2, "percent": "5"}],
        "referral_welcome": {"enabled": True, "percent": "20"},
        "renewal_campaign": {"enabled": True, "percent": "10"},
    }
    try:
        service.save_discounts(aid, values, "Условия тестовых скидок")
        q = c.quote(aid, "standard", 30, "purchase")
        assert D(q["total"]) == D("79.20") and q["selected_secondary"]["source"] == "referral_welcome"
        order = c.create_order(aid, q["id"], str(uuid4()))
        c.test_pay(aid, order["id"])
        assert D(c.quote(aid, "standard", 30, "purchase")["total"]) == D("99")
        sid = c.dashboard(aid)["subscriptions"][0]["id"]
        renew = c.quote(aid, "standard", 30, "renew", sid)
        assert D(renew["total"]) == D("89.10") and renew["selected_secondary"]["source"] == "renewal_campaign"
        with c.db.transaction() as s:
            assert s.get(Setting, "discounts").value["loyalty_period_days"] == 31
            assert (
                s.scalar(select(Audit).where(Audit.actor_id == aid, Audit.action == "discounts_update"))
                is not None
            )
    finally:
        with c.db.transaction() as s:
            s.get(Setting, "discounts").value = old
