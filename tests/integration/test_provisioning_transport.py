import importlib
import json
import os
from datetime import datetime, timedelta
from uuid import uuid4

import httpx
from sqlalchemy import select
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import Account, Database, Job, Product, Subscription, now

from tests.legal_fixture import accept_test_legal

UUID = "30000000-0000-4000-8000-000000000003"
SQUAD = "40000000-0000-4000-8000-000000000004"
BINDING = {"uuid": UUID, "short_uuid": "local_short", "username": "sunrise_test_retry"}


def test_persisted_target_retry_does_not_reprice_or_extend():
    uuid = str(uuid4())
    short = "test_" + uuid4().hex[:20]
    binding = BINDING | {"uuid": uuid, "short_uuid": short}
    module = importlib.import_module("sunrise_infrastructure.panel")
    url = os.environ["SUNRISE_TEST_DATABASE_URL"]
    db = Database(url)
    assert db.engine.url.database == "sunrise_test_backend"
    calls = []
    state = {"expiry": "2026-01-01T00:00:00+00:00", "fail": True}

    def handler(request):
        if request.method == "PATCH":
            body = json.loads(request.content)
            calls.append(body)
            if state["fail"]:
                state["fail"] = False
                raise httpx.ReadTimeout("synthetic failure")
            state["expiry"] = body["expireAt"]
        return httpx.Response(
            200,
            json={
                "response": {
                    "uuid": uuid,
                    "shortUuid": short,
                    "username": binding["username"],
                    "expireAt": state["expiry"],
                    "hwidDeviceLimit": 4,
                    "activeInternalSquads": [{"uuid": SQUAD}],
                    "status": "ACTIVE",
                    "subscriptionUrl": "https://connect.example.test/subscription/" + short,
                }
            },
        )

    with db.transaction() as s:
        a = Account(email=f"{uuid4()}@example.test")
        p = Product(
            currency="RUB",
            code=str(uuid4()),
            name="Тест",
            tier_level=70,
            price=100,
            included_devices=4,
            internal_squad_uuid=SQUAD,
        )
        s.add_all([a, p])
        s.flush()
        aid, pid = a.id, p.id
    accept_test_legal(Commerce(db), a.id)
    c = Commerce(db, local_test=True)
    quote = c.quote(aid, pid, 30, "purchase")
    assert "panel_mappings" not in quote
    order = c.create_order(aid, quote["id"], str(uuid4()))
    c.test_pay(aid, order["id"])
    sid = c.dashboard(aid)["subscriptions"][0]["id"]
    with db.transaction() as s:
        sub = s.get(Subscription, sid)
        sub.panel_uuid, sub.short_uuid = uuid, short
    credential = "synthetic-credential-placeholder"
    provider = module.RemnawaveTest(
        base_url="https://panel.example.test",
        token=credential,
        contract="2.8_uuid",
        authorized=True,
        allowed_hosts={"panel.example.test"},
        allowed_uuids={uuid},
        allowed_short_uuids={short},
        allowed_usernames={binding["username"]},
        allowed_squads={SQUAD},
        bindings={sid: binding},
        transport=httpx.MockTransport(handler),
        allowed_subscription_hosts={"connect.example.test"},
    )
    c.panel_provider = provider
    assert hasattr(c, "provisioning"), "Commerce must route jobs through shared persistent provisioning"
    c.run_jobs()
    with db.transaction() as s:
        j = s.scalar(select(Job).where(Job.key == "provision:" + order["id"]))
        assert j.state == "pending" and j.last_error == "panel_unavailable"
        assert j.payload["internal_squad_uuid"] == SQUAD
        original_expiry = j.payload["expires_at"]
        s.get(Product, pid).internal_squad_uuid = "50000000-0000-4000-8000-000000000005"
        j.available_at = now() - timedelta(seconds=1)
    c.run_jobs()
    with db.transaction() as s:
        j = s.scalar(select(Job).where(Job.key == "provision:" + order["id"]))
        assert j.state == "done"
        sub = s.get(Subscription, sid)
        assert sub.status == "active" and sub.panel_uuid == uuid and sub.short_uuid == short
        assert sub.expires_at == datetime.fromisoformat(original_expiry)
    assert len(calls) == 2 and calls[0] == calls[1]
    db.engine.dispose()


def test_create_timeout_reconciles_persisted_binding_before_any_retry_post():
    from sunrise_infrastructure.panel import RemnawaveTest
    from sunrise_infrastructure.provisioning import Provisioning

    db = Database(os.environ["SUNRISE_TEST_DATABASE_URL"])
    uuid, short = str(uuid4()), "test_" + uuid4().hex[:20]
    binding = {"uuid": uuid, "short_uuid": short, "username": "sunrise_test_" + uuid4().hex[:20]}
    expiry = (now() + timedelta(days=30)).isoformat()
    with db.transaction() as s:
        account = Account(email=f"{uuid4()}@example.test")
        s.add(account)
        s.flush()
        sub = Subscription(account_id=account.id, expires_at=datetime.fromisoformat(expiry), devices_limit=4)
        s.add(sub)
        s.flush()
        job = Job(
            key=str(uuid4()),
            kind="provision",
            account_id=account.id,
            payload={
                "subscription_id": sub.id,
                "expires_at": expiry,
                "devices_limit": 4,
                "version": sub.version,
                "internal_squad_uuid": SQUAD,
                "panel_object_type": "internal_squad",
                "environment": "local_test",
            },
        )
        s.add(job)
        s.flush()
        sid, jid = sub.id, job.id
    accept_test_legal(Commerce(db), account.id)
    state, methods = {}, []

    def handler(request):
        methods.append(request.method)
        if request.method == "POST":
            payload = json.loads(request.content)
            assert payload["uuid"] == uuid and payload["shortUuid"] == short
            state.update(
                uuid=uuid,
                shortUuid=short,
                username=binding["username"],
                expireAt=payload["expireAt"],
                hwidDeviceLimit=4,
                activeInternalSquads=[{"uuid": SQUAD}],
                status="ACTIVE",
                subscriptionUrl="https://connect.example.test/subscription/" + short,
            )
            raise httpx.ReadTimeout("synthetic accepted-before-timeout")
        return httpx.Response(200, json={"response": state}) if state else httpx.Response(404)

    credential = "synthetic-credential-placeholder"
    provider = RemnawaveTest(
        base_url="https://panel.example.test",
        token=credential,
        contract="2.8_uuid",
        authorized=True,
        allowed_hosts={"panel.example.test"},
        allowed_uuids={uuid},
        allowed_short_uuids={short},
        allowed_usernames={binding["username"]},
        allowed_squads={SQUAD},
        bindings={sid: binding},
        allowed_subscription_hosts={"connect.example.test"},
        transport=httpx.MockTransport(handler),
    )
    service = Provisioning(db, provider)
    assert service.run_jobs() == 0
    with db.transaction() as s:
        job = s.get(Job, jid)
        assert job.payload["create_started"] is True and job.state == "pending"
        assert job.payload["panel_binding"] == binding
        job.available_at = now() - timedelta(seconds=1)
    assert service.run_jobs() == 1
    assert methods.count("POST") == 1 and methods == ["GET", "POST", "GET", "PATCH", "GET"]
    with db.transaction() as s:
        sub = s.get(Subscription, sid)
        assert sub.panel_uuid == uuid and sub.short_uuid == short
        assert sub.expires_at == datetime.fromisoformat(expiry)
        assert sub.connection_url == state["subscriptionUrl"]
    db.engine.dispose()


def test_device_revoke_is_allowlisted_persistent_and_observed_before_local_success():
    from sunrise_infrastructure.db import Device
    from sunrise_infrastructure.panel import RemnawaveTest

    db = Database(os.environ["SUNRISE_TEST_DATABASE_URL"])
    uuid, short = str(uuid4()), "test_" + uuid4().hex[:20]
    binding = {"uuid": uuid, "short_uuid": short, "username": "sunrise_test_" + uuid4().hex[:20]}
    with db.transaction() as s:
        account = Account(email=f"{uuid4()}@example.test")
        s.add(account)
        s.flush()
        sub = Subscription(devices_limit=4, account_id=account.id, panel_uuid=uuid, short_uuid=short)
        s.add(sub)
        s.flush()
        device = Device(subscription_id=sub.id, name="Синтетическое устройство")
        s.add(device)
        s.flush()
        aid, sid, did = account.id, sub.id, device.id
    accept_test_legal(Commerce(db), account.id)
    calls = []
    device_present = True

    def handler(request):
        nonlocal device_present
        calls.append(request.url.path)
        if request.method == "POST":
            assert json.loads(request.content) == {"userUuid": uuid, "hwid": "synthetic_hwid"}
            device_present = False
        response = (
            {"devices": [{"hwid": "synthetic_hwid"}] if device_present else []}
            if "/hwid/" in request.url.path
            else {"uuid": uuid, "shortUuid": short, "username": binding["username"]}
        )
        return httpx.Response(200, json={"response": response})

    credential = "synthetic-credential-placeholder"
    provider = RemnawaveTest(
        base_url="https://panel.example.test",
        token=credential,
        contract="2.8_uuid",
        authorized=True,
        allowed_hosts={"panel.example.test"},
        allowed_uuids={uuid},
        allowed_short_uuids={short},
        allowed_usernames={binding["username"]},
        allowed_squads={SQUAD},
        allowed_hwids={"synthetic_hwid"},
        bindings={sid: binding},
        allowed_subscription_hosts={"connect.example.test"},
        transport=httpx.MockTransport(handler),
    )
    commerce = Commerce(db, local_test=True, panel_provider=provider, device_bindings={did: "synthetic_hwid"})
    assert commerce.revoke_device(aid, sid, did)["status"] == "revoke_pending"
    with db.transaction() as s:
        assert not s.get(Device, did).revoked
    commerce.run_jobs()
    with db.transaction() as s:
        assert s.get(Device, did).revoked
    assert calls == [
        "/api/users/" + uuid,
        "/api/hwid/devices/" + uuid,
        "/api/hwid/devices/delete",
        "/api/hwid/devices/" + uuid,
    ]
    db.engine.dispose()
