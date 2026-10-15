import json
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select
from sunrise_infrastructure.db import Account, Job, Order, Setting, Subscription, now

from tests.integration import test_panel_contact_sync
from tests.legal_fixture import accept_test_legal

live_contacts = test_panel_contact_sync.live_contacts


def test_paid_new_binding_uses_telegram_priority_and_verified_email_snapshot(live_contacts):
    db, commerce, aid, _, _, _, _ = live_contacts
    commerce.runtime_policy = None
    commerce.panel_provider = None
    config = commerce.public_settings
    commerce.public_settings = None
    accept_test_legal(commerce, aid)
    quote = commerce.quote(aid, "standard", 30, "purchase")
    order = commerce.create_order(aid, quote["id"], "new-name")
    commerce.test_pay(aid, order["id"])
    commerce.public_settings = config
    sid = commerce.order(aid, order["id"])["subscription_id"]
    from sunrise_infrastructure.provisioning import Provisioning

    from tests.integration.test_production_business_races import SyntheticBusinessPolicy

    policy = SyntheticBusinessPolicy(db)
    panel = SimpleNamespace(
        runtime_policy=policy,
        api_host="panel.synthetic.test",
        contract="3.4_id",
        register_project_binding=lambda *args, **kwargs: None,
    )
    with db.transaction() as tx:
        tx.get(Account, aid).telegram_username = "RealName"
        sub = tx.get(Subscription, sid)
        sub.environment = "live"
        saved = tx.get(Order, order["id"])
        saved.environment = "live"
        job = Job(
            key="new-name",
            kind="provision",
            account_id=aid,
            payload={"environment": "live", "scope_id": policy.scope.scope_id, "order_id": saved.id},
        )
        tx.add(job)
        tx.flush()
        binding = Provisioning(db, panel, commerce)._project_binding(tx, job, sub, job.payload)
        assert binding["username"] == "sr_RealName_12345_1"
        from sunrise_infrastructure.panel_v3 import production_binding_key

        record = tx.get(Setting, production_binding_key(sid))
        assert record.value["creation_contacts"] == {"email": "new@synthetic.test", "telegramId": 12345}
        assert record.value["naming"]["sequence"] == 1


def new_paid_live(item):
    db, commerce, aid, _, _, _, _ = item
    panel, policy, config = commerce.panel_provider, commerce.runtime_policy, commerce.public_settings
    commerce.panel_provider, commerce.runtime_policy, commerce.public_settings = None, None, None
    accept_test_legal(commerce, aid)
    quote = commerce.quote(aid, "standard", 30, "purchase")
    order = commerce.create_order(aid, quote["id"], "new-paid-live")
    commerce.test_pay(aid, order["id"])
    sid = commerce.order(aid, order["id"])["subscription_id"]
    commerce.panel_provider, commerce.runtime_policy, commerce.public_settings = panel, policy, config
    squad = "11111111-1111-4111-8111-111111111111"
    panel.squads.add(squad)
    panel.runtime_policy.scope.panel.internal_squads.append(squad)
    with db.transaction() as tx:
        tx.get(Account, aid).telegram_username = "RealName"
        sub = tx.get(Subscription, sid)
        sub.environment = "live"
        tx.get(Order, order["id"]).environment = "live"
        job = tx.scalar(select(Job).where(Job.key == "provision:" + order["id"]))
        job.state, job.available_at = "pending", now()
        job.payload = job.payload | {
            "environment": "live",
            "scope_id": policy.scope.scope_id,
            "internal_squad_uuid": squad,
        }
        jid, target = job.id, dict(job.payload)
    return sid, jid, target


def test_definite_foreign_preflight_collision_uses_sequence_two_and_preserves_short_uuid(live_contacts):
    db, commerce, _, _, _, calls, handlers = live_contacts
    sid, jid, target = new_paid_live(live_contacts)
    remote, posted = {}, []

    def handle(request):
        if request.method == "GET" and request.url.path.endswith("sr_RealName_12345_1"):
            return httpx.Response(
                200,
                json={
                    "response": {
                        "id": 99,
                        "username": "sr_RealName_12345_1",
                        "shortUuid": "foreignshortuuid123",
                    }
                },
            )
        if request.method == "GET" and "by-username" in request.url.path:
            return httpx.Response(404)
        if request.method == "POST":
            body = json.loads(request.content)
            posted.append(body)
            remote.update(
                body
                | {
                    "id": 42,
                    "activeInternalSquads": [{"uuid": target["internal_squad_uuid"]}],
                    "subscriptionUrl": "https://sub.synthetic.test/" + body["shortUuid"],
                }
            )
        return httpx.Response(200, json={"response": remote})

    handlers["handle"] = handle
    assert commerce.provisioning.run_jobs() == 1
    assert len(posted) == 1 and posted[0]["username"] == "sr_RealName_12345_2"
    assert posted[0]["email"] == "new@synthetic.test" and posted[0]["telegramId"] == 12345
    from sunrise_infrastructure.panel_v3 import production_binding_key

    with db.transaction() as tx:
        record = tx.get(Setting, production_binding_key(sid)).value
        assert record["naming"]["sequence"] == 2
        assert record["binding"]["short_uuid"] == posted[0]["shortUuid"]
        assert tx.get(Job, jid).state == "done" and tx.get(Subscription, sid).panel_user_id == 42
    assert calls.count("POST") == 1


def test_unknown_create_recovers_same_short_uuid_without_another_post(live_contacts):
    db, commerce, _, _, _, calls, handlers = live_contacts
    sid, jid, target = new_paid_live(live_contacts)
    remote, paths = {}, []

    def handle(request):
        paths.append(request.url.path)
        if request.method == "GET" and "by-username" in request.url.path:
            return httpx.Response(404)
        if request.method == "POST":
            body = json.loads(request.content)
            remote.update(
                body
                | {
                    "id": 42,
                    "activeInternalSquads": [{"uuid": target["internal_squad_uuid"]}],
                    "subscriptionUrl": "https://sub.synthetic.test/" + body["shortUuid"],
                }
            )
            raise httpx.ReadTimeout("synthetic uncertain CREATE")
        return httpx.Response(200, json={"response": remote})

    handlers["handle"] = handle
    assert commerce.provisioning.run_jobs() == 0
    with db.transaction() as tx:
        job = tx.get(Job, jid)
        assert job.payload["create_started"] is True
        job.available_at = now()
    completed = commerce.provisioning.run_jobs()
    with db.transaction() as tx:
        diagnostic = (tx.get(Job, jid).state, tx.get(Job, jid).last_error, calls, paths)
    assert completed == 1, diagnostic
    assert calls.count("POST") == 1 and any("by-short-uuid" in p for p in paths)
    with db.transaction() as tx:
        assert tx.get(Subscription, sid).panel_user_id == 42 and tx.get(Job, jid).state == "done"


def test_sixteen_known_occupied_probes_save_sequence_seventeen_without_post(live_contacts):
    db, commerce, _, _, _, calls, handlers = live_contacts
    sid, jid, _ = new_paid_live(live_contacts)

    def occupied(request):
        return httpx.Response(
            200,
            json={
                "response": {
                    "id": 99,
                    "username": request.url.path.rsplit("/", 1)[1],
                    "shortUuid": "foreignshortuuid123",
                }
            },
        )

    handlers["handle"] = occupied
    assert commerce.provisioning.run_jobs() == 0
    assert calls == ["GET"] * 16
    from sunrise_infrastructure.panel_v3 import production_binding_key

    with db.transaction() as tx:
        assert tx.get(Setting, production_binding_key(sid)).value["naming"]["sequence"] == 17
        assert tx.get(Job, jid).payload["create_started"] is False


def test_unknown_create_bare_404_does_not_advance_name_or_post_again(live_contacts):
    db, commerce, _, _, _, calls, handlers = live_contacts
    sid, jid, _ = new_paid_live(live_contacts)

    def lost(request):
        if request.method == "POST":
            raise httpx.ReadTimeout("synthetic unknown create")
        return httpx.Response(404)

    handlers["handle"] = lost
    assert commerce.provisioning.run_jobs() == 0
    from sunrise_infrastructure.panel_v3 import production_binding_key

    with db.transaction() as tx:
        first = dict(tx.get(Setting, production_binding_key(sid)).value["binding"])
        tx.get(Job, jid).available_at = now()
    assert commerce.provisioning.run_jobs() == 0
    with db.transaction() as tx:
        assert tx.get(Setting, production_binding_key(sid)).value["binding"] == first
        assert tx.get(Job, jid).state == "review"
    assert calls.count("POST") == 1


def test_generic_post_rejection_does_not_skip_candidate(live_contacts):
    db, commerce, _, _, _, calls, handlers = live_contacts
    sid, jid, _ = new_paid_live(live_contacts)

    def rejected(request):
        return httpx.Response(400) if request.method == "POST" else httpx.Response(404)

    handlers["handle"] = rejected
    assert commerce.provisioning.run_jobs() == 0
    from sunrise_infrastructure.panel_v3 import production_binding_key

    with db.transaction() as tx:
        record = tx.get(Setting, production_binding_key(sid)).value
        assert record["naming"]["sequence"] == 1 and tx.get(Job, jid).payload["create_started"] is True
    assert calls.count("POST") == 1


def test_profile_changed_before_post_reclaims_name_without_stale_creation_intent(live_contacts):
    db, commerce, aid, _, _, calls, handlers = live_contacts
    sid, jid, target = new_paid_live(live_contacts)
    changed, remote = [], {}

    def handle(request):
        if request.method == "GET" and "by-username" in request.url.path:
            if not changed:
                changed.append(True)
                with db.transaction() as tx:
                    actor = tx.get(Account, aid)
                    actor.telegram_id = actor.telegram_username = None
            return httpx.Response(404)
        if request.method == "POST":
            body = json.loads(request.content)
            remote.update(
                body
                | {
                    "id": 42,
                    "telegramId": None,
                    "activeInternalSquads": [{"uuid": target["internal_squad_uuid"]}],
                    "subscriptionUrl": "https://sub.synthetic.test/" + body["shortUuid"],
                }
            )
        return httpx.Response(200, json={"response": remote})

    handlers["handle"] = handle
    assert commerce.provisioning.run_jobs() == 0 and calls.count("POST") == 0
    with db.transaction() as tx:
        tx.get(Job, jid).available_at = now()
    assert commerce.provisioning.run_jobs() == 1 and calls.count("POST") == 1
    from sunrise_infrastructure.panel_v3 import production_binding_key

    with db.transaction() as tx:
        proof = tx.get(Setting, production_binding_key(sid)).value
        assert proof["binding"]["username"] == "sr_new_synthetic_test_1"
        assert proof["creation_intent"]["username"] == proof["binding"]["username"]


def test_missing_verified_identity_reviews_creation_without_worker_crash(live_contacts):
    db, commerce, aid, _, _, calls, _ = live_contacts
    _, jid, _ = new_paid_live(live_contacts)
    with db.transaction() as tx:
        actor = tx.get(Account, aid)
        actor.telegram_id, actor.email_verified = None, False
    assert commerce.provisioning.run_jobs() == 0
    with db.transaction() as tx:
        assert tx.get(Job, jid).state == "review"
    assert calls == []


def test_started_live_creation_without_matching_attempt_is_denied_before_http(live_contacts):
    from sunrise_infrastructure.provisioning import Provisioning

    db, commerce, _, _, _, calls, _ = live_contacts
    sid, jid, original_payload = new_paid_live(live_contacts)
    with pytest.raises(ValueError, match="panel_creation_origin_denied"):
        with db.transaction() as tx:
            job = tx.get(Job, jid)
            job.payload = job.payload | {"create_started": True}
            Provisioning(db, commerce.panel_provider, commerce)._project_binding(
                tx, job, tx.get(Subscription, sid), job.payload
            )
    with db.transaction() as tx:
        assert tx.get(Job, jid).payload == original_payload
    assert calls == []
