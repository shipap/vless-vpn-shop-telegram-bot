"""Retiring an unbound local fixture preserves its paid historical graph."""

from uuid import uuid4

import fakeredis
import pytest
from sqlalchemy import select
from sunrise_core.errors import DomainError
from sunrise_infrastructure.admin import Admin
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import Account, Job, Order, Payment, Quote, Segment, Setting, Subscription
from sunrise_infrastructure.security import RateGuard
from sunrise_infrastructure.subscription_archive import unarchived_subscription_clause

from tests.isolated_database import isolated_database
from tests.legal_fixture import accept_test_legal


@pytest.fixture
def fixture_subscription():
    with isolated_database("fixture_retire_test") as db:
        commerce = Commerce(db, local_test=True)
        with db.transaction() as session:
            account = Account(email=f"{uuid4()}@example.test", email_verified=True)
            session.add(account)
            session.flush()
            aid = account.id
        accept_test_legal(commerce, aid)
        quote = commerce.quote(aid, "standard", 30, "purchase")
        order = commerce.create_order(aid, quote["id"], str(uuid4()))
        commerce.test_pay(aid, order["id"])
        sid = commerce.dashboard(aid)["subscriptions"][0]["id"]
        with db.transaction() as session:
            sub = session.get(Subscription, sid)
            assert sub.environment == "local_test"
            assert all(
                getattr(sub, key) is None
                for key in ("panel_uuid", "panel_user_id", "short_uuid", "connection_url")
            )
            sub.status = "fixture_retired"
        yield commerce, aid, sid, order["id"]


def paid_graph(commerce):
    with commerce.db.transaction() as session:
        return {
            model.__tablename__: sorted(
                (
                    tuple(getattr(row, col.name) for col in model.__table__.columns)
                    for row in session.scalars(select(model))
                ),
                key=lambda row: str(row[0]),
            )
            for model in (Subscription, Segment, Quote, Order, Payment)
        }


def test_retired_fixture_absent_from_profile_and_operational_queries(fixture_subscription):
    commerce, aid, sid, _ = fixture_subscription
    before = paid_graph(commerce)
    dashboard = commerce.dashboard(aid)
    assert dashboard["subscriptions"] == []
    assert dashboard["subscription_deletion_notices"] == []
    assert len(dashboard["payments"]) == 1
    with commerce.db.transaction() as session:
        assert list(session.scalars(select(Subscription.id).where(unarchived_subscription_clause()))) == []
        assert session.get(Setting, "archive:" + sid) is None
    assert paid_graph(commerce) == before


def test_retired_fixture_cannot_be_priced_for_renewal(fixture_subscription):
    commerce, aid, sid, _ = fixture_subscription
    before = paid_graph(commerce)
    with pytest.raises(DomainError) as error:
        commerce.quote(aid, "standard", 30, "renew", subscription_id=sid)
    assert (error.value.code, error.value.status) == ("not_found", 404)
    assert paid_graph(commerce) == before


def test_retired_fixture_not_promo_target_and_cannot_receive_free_days(fixture_subscription):
    commerce, aid, sid, _ = fixture_subscription
    code = "FIXTURE" + uuid4().hex[:8].upper()
    with commerce.db.transaction() as session:
        policy = session.get(Setting, "discounts")
        policy.value = policy.value | {
            "promos": [
                {
                    "code": code,
                    "enabled": True,
                    "reward_type": "free_days",
                    "reward_days": 5,
                    "product_id": "standard",
                    "per_account_limit": 1,
                    "audience": "all",
                }
            ]
        }
    commerce.activate_promo(aid, code)
    before = paid_graph(commerce)
    assert commerce.promos.targets(aid) == []
    with pytest.raises(DomainError) as error:
        commerce.claim_promo(aid, code, subscription_id=sid)
    assert (error.value.code, error.value.status) == ("not_found", 404)
    assert paid_graph(commerce) == before


def test_pending_fake_provision_cannot_reactivate_retired_fixture(fixture_subscription):
    commerce, _, sid, _ = fixture_subscription
    before = paid_graph(commerce)
    commerce.run_jobs(limit=100)
    with commerce.db.transaction() as session:
        job = session.scalar(select(Job).where(Job.kind == "provision"))
        assert job.payload["subscription_id"] == sid
        assert job.state == "review"
        assert job.last_error == "subscription_cleanup_pending"
    assert paid_graph(commerce) == before


def test_segment_boundary_does_not_queue_retired_fixture(fixture_subscription):
    commerce, _, sid, _ = fixture_subscription
    # A changed version leaves no matching current provision job. Paid segment remains current.
    with commerce.db.transaction() as session:
        session.get(Subscription, sid).version += 1
        jobs = list(session.scalars(select(Job.id)))
    before = paid_graph(commerce)
    commerce.apply_segment_boundaries()
    assert paid_graph(commerce) == before
    with commerce.db.transaction() as session:
        assert list(session.scalars(select(Job.id))) == jobs


@pytest.mark.parametrize("operation", ["suspend", "resume", "synchronize", "compensate"])
def test_direct_admin_mutations_cannot_reactivate_retired_fixture(fixture_subscription, operation):
    commerce, aid, sid, _ = fixture_subscription
    before = paid_graph(commerce)
    admin = Admin(commerce, RateGuard(fakeredis.FakeRedis()))
    with pytest.raises(DomainError) as error:
        if operation == "compensate":
            admin.compensate(aid, sid, 2, "Проверка тестовой фикстуры")
        else:
            admin.subscription_operation(aid, sid, operation, "Проверка тестовой фикстуры")
    assert (error.value.code, error.value.status) == ("subscription_cleanup_pending", 409)
    assert paid_graph(commerce) == before


def test_repeat_paid_fulfillment_keeps_retired_fixture_history(fixture_subscription):
    commerce, aid, _, oid = fixture_subscription
    before = paid_graph(commerce)
    commerce.test_pay(aid, oid)
    assert paid_graph(commerce) == before


def test_active_local_control_remains_operable(fixture_subscription):
    commerce, aid, sid, _ = fixture_subscription
    with commerce.db.transaction() as session:
        session.get(Subscription, sid).status = "active"
    assert [sub["id"] for sub in commerce.dashboard(aid)["subscriptions"]] == [sid]
    assert commerce.promos.targets(aid) == [sid]
    quote = commerce.quote(aid, "standard", 30, "renew", subscription_id=sid)
    assert quote["subscription_id"] == sid
    admin = Admin(commerce, RateGuard(fakeredis.FakeRedis()))
    assert admin.subscription_operation(aid, sid, "suspend", "Проверка обычной подписки")["job_id"]
