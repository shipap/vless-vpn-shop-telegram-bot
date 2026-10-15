"""Synthetic PostgreSQL and MockTransport exercise durable automatic test issuance."""

import json
import os
from datetime import datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sunrise_infrastructure.db import Account, Database, Job, LegacyIssue, Setting, Subscription, now
from sunrise_infrastructure.panel import RemnawaveTest
from sunrise_infrastructure.provisioning import Provisioning

SQUAD = "20000000-0000-4000-8000-000000000002"
SYNTHETIC = "synthetic-credential-placeholder"


@pytest.fixture
def db():
    value = Database(os.environ["SUNRISE_TEST_DATABASE_URL"])
    assert value.engine.url.database == "sunrise_test_backend"
    assert value.engine.url.host in {"localhost", "127.0.0.1"}
    yield value
    value.engine.dispose()


def create_account(db):
    telegram_id = 70000000000000000 + uuid4().int % 100000000000000
    with db.transaction() as session:
        account = Account(telegram_id=telegram_id, email=f"{uuid4()}@example.test")
        session.add(account)
        session.flush()
        return account.id, str(telegram_id)


def provider(telegram_id, handler):
    return RemnawaveTest(
        base_url="https://panel.example.test",
        token=SYNTHETIC,
        contract="2.8_uuid",
        authorized=True,
        allowed_hosts={"panel.example.test"},
        allowed_squads={SQUAD},
        auto_test_telegram_ids={telegram_id},
        transport=httpx.MockTransport(handler),
    )


def new_job(db, account_id, *, subscription_id=None, **overrides):
    with db.transaction() as session:
        if subscription_id is None:
            subscription = Subscription(
                devices_limit=4, account_id=account_id, expires_at=now() + timedelta(days=30)
            )
            session.add(subscription)
            session.flush()
        else:
            subscription = session.get(Subscription, subscription_id)
        payload = (
            dict(
                subscription_id=subscription.id,
                version=subscription.version,
                expires_at=subscription.expires_at.isoformat(),
                devices_limit=subscription.devices_limit,
                environment=subscription.environment,
                panel_object_type="internal_squad",
                internal_squad_uuid=SQUAD,
            )
            | overrides
        )
        job = Job(key=str(uuid4()), kind="provision", account_id=account_id, payload=payload)
        session.add(job)
        session.flush()
        return subscription.id, job.id


def panel_response(body):
    return dict(
        uuid=body["uuid"],
        shortUuid=body["shortUuid"],
        username=body["username"],
        expireAt=body["expireAt"],
        hwidDeviceLimit=body["hwidDeviceLimit"],
        activeInternalSquads=[{"uuid": SQUAD}],
        status="ACTIVE",
        subscriptionUrl="https://derived.example.test/subscription/" + body["shortUuid"],
    )


def test_auto_binding_is_committed_before_http_and_link_is_persisted_atomically(db):
    account_id, telegram_id = create_account(db)
    state, calls, fixture = {}, [], {}

    def handler(request):
        calls.append(request.method)
        # Separate transaction must observe identity before even the first lookup.
        with db.transaction() as session:
            record = session.get(Setting, "panel_test:" + fixture["sid"])
            job = session.get(Job, fixture["jid"])
            assert record.value["account_id"] == str(account_id)
            assert record.value["binding"] == job.payload["panel_binding"]
            assert job.payload["create_started"] is True
        if request.method == "POST":
            state.update(panel_response(json.loads(request.content)))
        return httpx.Response(200, json={"response": state}) if state else httpx.Response(404)

    service = Provisioning(db, provider(telegram_id, handler))
    fixture["sid"], fixture["jid"] = new_job(db, account_id)
    source_expiry = "2027-01-01T00:00:00.123793+00:00"
    with db.transaction() as session:
        session.get(Subscription, fixture["sid"]).expires_at = datetime.fromisoformat(source_expiry)
        job = session.get(Job, fixture["jid"])
        job.payload = dict(job.payload) | {"expires_at": source_expiry}
    assert service.run_jobs() == 1
    assert calls == ["GET", "POST", "GET"]
    with db.transaction() as session:
        subscription = session.get(Subscription, fixture["sid"])
        record = session.get(Setting, "panel_test:" + fixture["sid"])
        assert subscription.panel_uuid == record.value["binding"]["uuid"]
        assert subscription.connection_url == record.value["connection_url"] == state["subscriptionUrl"]
        assert subscription.expires_at == datetime.fromisoformat(source_expiry)
        assert session.get(Job, fixture["jid"]).payload["expires_at"] == source_expiry
        assert state["expireAt"] == "2027-01-01T00:00:00.124+00:00"
        assert subscription.status == "active"


def test_restart_after_accepted_create_timeout_reuses_identity_without_second_post(db):
    account_id, telegram_id = create_account(db)
    state, methods, created = {}, [], []

    def handler(request):
        methods.append(request.method)
        if request.method == "POST":
            body = json.loads(request.content)
            created.append(body)
            state.update(panel_response(body))
            raise httpx.ReadTimeout("synthetic accepted creation")
        if request.method == "PATCH":
            state["expireAt"] = json.loads(request.content)["expireAt"]
        return httpx.Response(200, json={"response": state}) if state else httpx.Response(404)

    service = Provisioning(db, provider(telegram_id, handler))
    sid, jid = new_job(db, account_id)
    assert service.run_jobs() == 0
    with db.transaction() as session:
        job = session.get(Job, jid)
        original_binding = job.payload["panel_binding"]
        assert job.state == "pending" and job.payload["create_started"] is True
        job.available_at = now() - timedelta(seconds=1)
    restarted = Provisioning(db, provider(telegram_id, handler))
    assert restarted.run_jobs() == 1
    assert methods == ["GET", "POST", "GET", "PATCH", "GET"]
    assert len(created) == 1 and created[0]["uuid"] == original_binding["uuid"]
    with db.transaction() as session:
        assert session.get(Subscription, sid).panel_uuid == original_binding["uuid"]


def test_failed_first_lookup_retries_same_binding_and_creates_once(db):
    account_id, telegram_id = create_account(db)
    state, methods = {}, []

    def handler(request):
        methods.append(request.method)
        if len(methods) == 1:
            raise httpx.ConnectError("synthetic initial lookup connection failure")
        if request.method == "POST":
            state.update(panel_response(json.loads(request.content)))
        return httpx.Response(200, json={"response": state}) if state else httpx.Response(404)

    service = Provisioning(db, provider(telegram_id, handler))
    sid, jid = new_job(db, account_id)
    assert service.run_jobs() == 0
    with db.transaction() as session:
        job = session.get(Job, jid)
        original_binding = dict(job.payload["panel_binding"])
        assert job.state == "pending" and job.payload["create_started"] is False
        job.available_at = now() - timedelta(seconds=1)
    restarted = Provisioning(db, provider(telegram_id, handler))
    assert restarted.run_jobs() == 1
    assert methods == ["GET", "GET", "POST", "GET"]
    with db.transaction() as session:
        assert session.get(Job, jid).payload["panel_binding"] == original_binding
        assert session.get(Subscription, sid).panel_uuid == original_binding["uuid"]


def test_unknown_initial_lookup_failure_preserves_existing_creation_marker(db):
    account_id, telegram_id = create_account(db)
    state, methods = {}, []

    def handler(request):
        methods.append(request.method)
        if request.method == "POST":
            state.update(panel_response(json.loads(request.content)))
            raise httpx.ReadTimeout("synthetic accepted creation")
        if len(methods) > 2:
            raise httpx.ConnectError("synthetic lookup failure during uncertain recovery")
        return httpx.Response(404)

    service = Provisioning(db, provider(telegram_id, handler))
    _, jid = new_job(db, account_id)
    assert service.run_jobs() == 0
    with db.transaction() as session:
        job = session.get(Job, jid)
        assert job.payload["create_started"] is True
        job.available_at = now() - timedelta(seconds=1)
    restarted = Provisioning(db, provider(telegram_id, handler))
    assert restarted.run_jobs() == 0
    with db.transaction() as session:
        assert session.get(Job, jid).payload["create_started"] is True
    assert methods == ["GET", "POST", "GET"]


def test_historical_pending_job_is_not_automatically_registered(db):
    account_id, telegram_id = create_account(db)
    sid, jid = new_job(db, account_id)
    service = Provisioning(
        db, provider(telegram_id, lambda _: pytest.fail("Historical jobs cannot reach panel"))
    )
    assert service.run_jobs() == 0
    with db.transaction() as session:
        assert session.get(Job, jid).state == "pending"
        assert session.get(Setting, "panel_test:" + sid) is None


@pytest.mark.parametrize("denial", ["owner", "production", "existing_uuid", "import", "job_owner"])
def test_auto_registration_denies_foreign_existing_and_imported_targets(db, denial):
    account_id, telegram_id = create_account(db)
    service = Provisioning(db, provider(telegram_id, lambda _: pytest.fail("Denied target reached panel")))
    sid, jid = new_job(db, account_id)
    with db.transaction() as session:
        subscription = session.get(Subscription, sid)
        job = session.get(Job, jid)
        if denial == "owner":
            session.get(Account, account_id).telegram_id = None
        elif denial == "production":
            subscription.environment = "production"
        elif denial == "existing_uuid":
            subscription.panel_uuid = str(uuid4())
        elif denial == "import":
            session.add(LegacyIssue(source_table="subscriptions", source_id=sid, reason="synthetic_import"))
        elif denial == "job_owner":
            job.account_id = None
    assert service.run_jobs() == 0
    with db.transaction() as session:
        assert session.get(Setting, "panel_test:" + sid) is None
        assert session.get(Subscription, sid).connection_url is None


def test_canonical_owner_change_after_timeout_denies_resume(db):
    account_id, telegram_id = create_account(db)
    service = Provisioning(
        db, provider(telegram_id, lambda _: (_ for _ in ()).throw(httpx.ReadTimeout("synthetic")))
    )
    sid, jid = new_job(db, account_id)
    assert service.run_jobs() == 0
    with db.transaction() as session:
        session.get(Account, account_id).telegram_id = None
        new_owner = Account(telegram_id=int(telegram_id), email=f"{uuid4()}@example.test")
        session.add(new_owner)
        session.flush()
        session.get(Subscription, sid).account_id = new_owner.id
        job = session.get(Job, jid)
        job.account_id = new_owner.id
        job.available_at = now() - timedelta(seconds=1)
    restarted = Provisioning(
        db, provider(telegram_id, lambda _: pytest.fail("Changed canonical owner reached HTTP"))
    )
    assert restarted.run_jobs() == 0
    with db.transaction() as session:
        assert session.get(Job, jid).state == "blocked"


def test_renewal_reuses_durable_binding_and_rejects_changed_connection_host(db):
    account_id, telegram_id = create_account(db)
    state, methods = {}, []

    def handler(request):
        methods.append(request.method)
        if request.method == "POST":
            state.update(panel_response(json.loads(request.content)))
        if request.method == "PATCH":
            state["expireAt"] = json.loads(request.content)["expireAt"]
            state["subscriptionUrl"] = "https://changed.example.test/subscription/" + state["shortUuid"]
        return httpx.Response(200, json={"response": state}) if state else httpx.Response(404)

    service = Provisioning(db, provider(telegram_id, handler))
    sid, _ = new_job(db, account_id)
    assert service.run_jobs() == 1
    with db.transaction() as session:
        subscription = session.get(Subscription, sid)
        original_link = subscription.connection_url
        original_uuid = subscription.panel_uuid
        subscription.version += 1
        subscription.expires_at += timedelta(days=30)
    _, jid = new_job(db, account_id, subscription_id=sid)
    restarted = Provisioning(db, provider(telegram_id, handler))
    assert restarted.run_jobs() == 0
    assert methods.count("POST") == 1
    with db.transaction() as session:
        assert session.get(Job, jid).state == "review"
        subscription = session.get(Subscription, sid)
        assert subscription.panel_uuid == original_uuid and subscription.connection_url == original_link


def test_owner_is_revalidated_before_local_completion_after_http(db):
    account_id, telegram_id = create_account(db)
    state = {}

    def handler(request):
        if request.method == "POST":
            state.update(panel_response(json.loads(request.content)))
            with db.transaction() as session:
                session.get(Account, account_id).telegram_id = None
        return httpx.Response(200, json={"response": state}) if state else httpx.Response(404)

    service = Provisioning(db, provider(telegram_id, handler))
    sid, jid = new_job(db, account_id)
    assert service.run_jobs() == 0
    with db.transaction() as session:
        assert session.get(Job, jid).state == "review"
        assert session.get(Subscription, sid).panel_uuid is None
