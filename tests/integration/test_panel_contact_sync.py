"""Real PostgreSQL; policy facade/HTTP are synthetic, not production admission."""

from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import Account, Job, Setting, Subscription, now
from sunrise_infrastructure.panel_v3 import RemnawaveProduction, production_binding_key

from tests.integration.test_production_business_races import SyntheticBusinessPolicy
from tests.isolated_database import isolated_database


@pytest.fixture
def live_contacts():
    with isolated_database("panel_contacts") as db:
        commerce = Commerce(db, local_test=True)
        policy = SyntheticBusinessPolicy(db)
        policy.scope.panel = SimpleNamespace(
            origin="https://panel.synthetic.test",
            internal_squads=[],
            external_squads=[],
            subscription_hosts=["sub.synthetic.test"],
            node_uuids=[],
        )
        current = dict(
            id=7,
            username="legacy.stable",
            shortUuid="syntheticshortuuid123",
            subscriptionUrl="https://sub.synthetic.test/syntheticshortuuid123",
            expireAt="2030-01-01T00:00:00Z",
            status="ACTIVE",
            activeInternalSquads=[],
            externalSquadUuid=None,
            hwidDeviceLimit=None,
            email="old@synthetic.test",
            telegramId=None,
        )
        calls = []
        handlers = {}

        def handle(request):
            import json

            calls.append(request.method)
            if "handle" in handlers:
                return handlers["handle"](request)
            if "on_get" in handlers and request.method == "GET":
                handlers["on_get"]()
            if request.method == "PATCH":
                current.update(json.loads(request.content))
                if "on_patch" in handlers:
                    handlers["on_patch"]()
            return httpx.Response(200, json={"response": current})

        with db.transaction() as tx:
            actor = Account(email="new@synthetic.test", email_verified=True, telegram_id=12345)
            tx.add(actor)
            tx.flush()
            aid = actor.id
            actor.legacy_consent = {
                "production_access": {
                    "version": 1,
                    "account_id": str(aid),
                    "state": "active",
                    "source": "legacy_user_is_active",
                    "source_account_id": str(aid),
                }
            }
            sub = Subscription(
                account_id=aid,
                product_id="standard",
                environment="live",
                status="active",
                panel_user_id=7,
                short_uuid=current["shortUuid"],
                connection_url=current["subscriptionUrl"],
                devices_limit=None,
                expires_at=now() + timedelta(days=3),
            )
            tx.add(sub)
            tx.flush()
            sid = sub.id
            binding = dict(
                scope_id=policy.scope.scope_id,
                generation=1,
                origin_nonce="a" * 32,
                user_id="7",
                uuid=None,
                username=current["username"],
                short_uuid=sub.short_uuid,
            )
            tx.add(
                Setting(
                    key=production_binding_key(sid),
                    value=dict(
                        scope_id=policy.scope.scope_id,
                        generation=1,
                        origin="imported_project_identity",
                        contract="3.4_id",
                        panel_host="panel.synthetic.test",
                        subscription_id=sid,
                        account_id=str(aid),
                        environment="live",
                        binding=binding,
                        connection_url=sub.connection_url,
                    ),
                )
            )
            tx.add(
                Setting(
                    key="production_admission",
                    value={"scope_id": policy.scope.scope_id, "synthetic_business_only": True},
                )
            )
            tx.add(Setting(key="production_runtime_gates", value={"panel_mutation": True, "auth_mail": True}))
        panel = RemnawaveProduction(
            runtime_policy=policy,
            token="synthetic-panel-token",  # noqa: S106 -- mock only
            transport=httpx.MockTransport(handle),
        )
        commerce.panel_provider, commerce.runtime_policy = panel, policy
        commerce.public_settings = SimpleNamespace(panel_username_prefix="sr")
        yield db, commerce, aid, sid, current, calls, handlers
        panel.client.close()


def enqueue(db, commerce, aid, source="synthetic-verified-email"):
    from sunrise_infrastructure.panel_contacts import PanelContactSync

    service = PanelContactSync(commerce)
    with db.transaction() as tx:
        service.enqueue_in(tx, tx.get(Account, aid), source)
    return service


def test_owned_contacts_sync_once_preserves_nullable_capacity_and_paid_identity(live_contacts):
    db, commerce, aid, sid, current, calls, _ = live_contacts
    before = {k: v for k, v in current.items() if k not in {"email", "telegramId"}}
    service = enqueue(db, commerce, aid)
    assert service.run_pending() == 1
    assert current["email"] == "new@synthetic.test" and current["telegramId"] == 12345
    assert {k: v for k, v in current.items() if k not in {"email", "telegramId"}} == before
    assert service.run_pending() == 0
    assert calls == ["GET", "PATCH", "GET"]
    with db.transaction() as tx:
        assert tx.get(Subscription, sid).version == 1
        assert len(list(tx.scalars(select(Job).where(Job.kind == "panel_contact")))) == 1


def test_transaction_rollback_leaves_no_contact_intent(live_contacts):
    from sunrise_infrastructure.panel_contacts import PanelContactSync

    db, commerce, aid, _, _, calls, _ = live_contacts
    with pytest.raises(RuntimeError), db.transaction() as tx:
        PanelContactSync(commerce).enqueue_in(tx, tx.get(Account, aid), "verified")
        raise RuntimeError("synthetic rollback")
    with db.transaction() as tx:
        assert not list(tx.scalars(select(Job).where(Job.kind == "panel_contact")))
    assert calls == []


def test_gate_off_preserves_local_verified_change_and_later_delivers(live_contacts):
    db, commerce, aid, _, _, calls, _ = live_contacts
    with db.transaction() as tx:
        tx.get(Setting, "production_runtime_gates").value = {"panel_mutation": False}
    service = enqueue(db, commerce, aid)
    assert service.run_pending() == 0 and calls == []
    with db.transaction() as tx:
        tx.get(Setting, "production_runtime_gates").value = {"panel_mutation": True}
    assert service.run_pending() == 1


def test_profile_change_during_get_blocks_old_patch_then_sends_current_goal(live_contacts):
    db, commerce, aid, _, current, calls, handlers = live_contacts
    service = enqueue(db, commerce, aid)

    def change():
        handlers.pop("on_get")
        with db.transaction() as tx:
            actor = tx.get(Account, aid)
            actor.email = "latest@synthetic.test"
            service.enqueue_in(tx, actor, "latest-verified")

    handlers["on_get"] = change
    assert service.run_pending() == 0 and calls == ["GET"]
    with db.transaction() as tx:
        for job in tx.scalars(select(Job).where(Job.kind == "panel_contact")):
            job.available_at = now()
    assert service.run_pending() == 1
    assert current["email"] == "latest@synthetic.test" and calls.count("PATCH") == 1


def due(db):
    with db.transaction() as tx:
        for job in tx.scalars(select(Job).where(Job.kind == "panel_contact")):
            job.available_at = now()


def test_gate_withdrawn_after_get_denies_patch_and_restored_gate_recovers(live_contacts):
    db, commerce, aid, _, _, calls, handlers = live_contacts
    service = enqueue(db, commerce, aid)

    def revoke():
        handlers.pop("on_get")
        with db.transaction() as tx:
            tx.get(Setting, "production_runtime_gates").value = {"panel_mutation": False}

    handlers["on_get"] = revoke
    assert service.run_pending() == 0 and calls == ["GET"]
    with db.transaction() as tx:
        tx.get(Setting, "production_runtime_gates").value = {"panel_mutation": True}
    due(db)
    assert service.run_pending() == 1 and calls.count("PATCH") == 1


def test_uncertain_landed_old_patch_reconciles_before_new_goal_without_duplicate(live_contacts):
    db, commerce, aid, _, current, calls, handlers = live_contacts
    service = enqueue(db, commerce, aid)

    def changed_then_timeout():
        handlers.pop("on_patch")
        with db.transaction() as tx:
            actor = tx.get(Account, aid)
            actor.email = "latest@synthetic.test"
            service.enqueue_in(tx, actor, "latest-verified")
        raise httpx.ReadTimeout("synthetic lost PATCH response")

    handlers["on_patch"] = changed_then_timeout
    assert service.run_pending() == 0 and calls.count("PATCH") == 1
    assert current["email"] == "new@synthetic.test"
    assert service.run_pending() == 0 and calls.count("PATCH") == 1
    assert service.run_pending() == 1 and calls.count("PATCH") == 2
    assert current["email"] == "latest@synthetic.test"
    assert service.run_pending() == 0


def test_unknown_unobserved_patch_never_reposts_or_sends_newer_goal(live_contacts):
    db, commerce, aid, _, current, calls, handlers = live_contacts
    service = enqueue(db, commerce, aid)

    def uncertain(request):
        if request.method == "PATCH":
            raise httpx.ReadTimeout("synthetic uncertain PATCH before observed apply")
        return httpx.Response(200, json={"response": current})

    handlers["handle"] = uncertain
    assert service.run_pending() == 0 and calls.count("PATCH") == 1
    with db.transaction() as tx:
        actor = tx.get(Account, aid)
        actor.email = "latest@synthetic.test"
        service.enqueue_in(tx, actor, "latest")
    assert service.run_pending() == 0 and calls.count("PATCH") == 1
    due(db)
    assert service.run_pending() == 0 and calls.count("PATCH") == 1


def test_concurrent_workers_share_provisioning_lock_and_stale_get_cannot_send(live_contacts):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    db, commerce, aid, _, current, calls, handlers = live_contacts
    service = enqueue(db, commerce, aid)
    entered, release = Event(), Event()

    def blocked_get():
        handlers.pop("on_get")
        entered.set()
        assert release.wait(5)

    handlers["on_get"] = blocked_get
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(service.run_pending)
        assert entered.wait(5)
        # Reclaim is intentionally due; the shared advisory lock still denies it.
        due(db)
        assert service.run_pending() == 0
        with db.transaction() as tx:
            actor = tx.get(Account, aid)
            actor.email = "latest@synthetic.test"
            service.enqueue_in(tx, actor, "latest-verified")
        release.set()
        assert pending.result(timeout=5) == 0
    assert calls == ["GET"]
    assert service.run_pending() == 1 and current["email"] == "latest@synthetic.test"


@pytest.mark.parametrize("invalid", ["archive", "foreign", "disabled"])
def test_current_scope_and_disabled_access_deny_contact_transport(live_contacts, invalid):
    db, commerce, aid, sid, _, calls, _ = live_contacts
    service = enqueue(db, commerce, aid)
    with db.transaction() as tx:
        if invalid == "archive":
            tx.add(Setting(key="archive:" + sid, value={"invalid": "fail closed"}))
        elif invalid == "foreign":
            marker = tx.get(Setting, production_binding_key(sid))
            marker.value = marker.value | {"scope_id": "foreign-scope"}
        else:
            actor = tx.get(Account, aid)
            actor.legacy_consent = {
                "production_access": actor.legacy_consent["production_access"] | {"state": "disabled"}
            }
    assert service.run_pending() == 0 and calls == []
    with db.transaction() as tx:
        job = tx.scalar(select(Job).where(Job.kind == "panel_contact"))
        assert job.state == "review"


def identity_accounts(item):
    import fakeredis
    from sunrise_infrastructure.accounts import Accounts
    from sunrise_infrastructure.security import RateGuard
    from sunrise_infrastructure.settings import Settings

    db, commerce, *_ = item
    config = Settings(
        _env_file=None,
        local_test=True,
        secret_key="synthetic-identity-contacts-key-1234567890",  # noqa: S106 -- synthetic OTP only
        owner_id=None,
        admin_email_allowlist=[],
    )  # noqa: S106 -- synthetic OTP only
    config._runtime_policy = commerce.runtime_policy
    return Accounts(db, RateGuard(fakeredis.FakeRedis()), config)


def test_real_email_otp_confirmation_enqueues_only_after_correct_code(live_contacts):
    from sunrise_core.errors import DomainError
    from sunrise_infrastructure.db import Challenge
    from sunrise_infrastructure.security import digest

    from tests.legal_fixture import current_test_legal

    db, commerce, aid, _, _, calls, _ = live_contacts
    accounts = identity_accounts(live_contacts)
    docs = current_test_legal(commerce)
    cid, code, ip = "synthetic-otp-contact", "123456", "127.0.0.1"
    with db.transaction() as tx:
        actor = tx.get(Account, aid)
        actor.email_verified = False
        tx.add(
            Challenge(
                id=cid,
                email=actor.email,
                digest=accounts._code_digest(cid, code),
                context=digest(ip + ":" + actor.email),
                expires_at=now() + timedelta(minutes=10),
            )
        )
    with pytest.raises(DomainError):
        accounts.verify_code(cid, "000000", ip, legal_accepted=True, documents=docs)
    with db.transaction() as tx:
        assert not list(tx.scalars(select(Job).where(Job.kind == "panel_contact")))
    accounts.verify_code(cid, code, ip, legal_accepted=True, documents=docs)
    from sunrise_infrastructure.panel_contacts import PanelContactSync

    assert PanelContactSync(commerce).run_pending() == 1
    assert calls.count("PATCH") == 1


def test_real_verified_email_change_and_telegram_unlink_sync_only_contacts(live_contacts):
    from sunrise_infrastructure.account_features import AccountsFeatures
    from sunrise_infrastructure.db import Session
    from sunrise_infrastructure.panel_contacts import PanelContactSync

    db, commerce, aid, _, current, calls, _ = live_contacts
    accounts = identity_accounts(live_contacts)
    features, deliveries = AccountsFeatures(accounts), []
    accounts._deliver_code = lambda *args: deliveries.append(args)
    ip, session_id = "127.0.0.1", "synthetic-identity-session"
    with db.transaction() as tx:
        tx.add(
            Session(
                id=session_id,
                account_id=aid,
                csrf="synthetic",
                expires_at=now() + timedelta(days=1),
                email_confirmed_at=now(),
                ip_address=ip,
            )
        )
    result = features.request_email_change(aid, session_id, "changed@synthetic.test", ip)
    with db.transaction() as tx:
        assert not list(tx.scalars(select(Job).where(Job.kind == "panel_contact")))
    cid, code, *_ = deliveries[0]
    features.verify_email_change(aid, session_id, result["operation_id"], cid, code, ip)
    service = PanelContactSync(commerce)
    assert service.run_pending() == 1 and current["email"] == "changed@synthetic.test"
    with db.transaction() as tx:
        tx.get(Session, session_id).email_confirmed_at = now()
    operation = features.request_unlink(aid, session_id, ip)
    features.confirm_unlink(12345, operation["proof"])
    assert service.run_pending() == 1 and current["telegramId"] is None
    assert calls.count("PATCH") == 2


def test_actual_canonical_merge_enqueues_owned_contact_without_rename_or_identity_loss(live_contacts):
    from sunrise_infrastructure.db import Alias, MergeProof
    from sunrise_infrastructure.panel_contacts import PanelContactSync
    from sunrise_infrastructure.security import digest

    db, commerce, telegram_aid, sid, current, calls, _ = live_contacts
    accounts = identity_accounts(live_contacts)
    with db.transaction() as tx:
        actor = tx.get(Account, telegram_aid)
        actor.email, actor.email_verified = None, False
        email_owner = Account(email="canonical@synthetic.test", email_verified=True)
        tx.add(email_owner)
        tx.flush()
        cid = email_owner.id
        email_owner.legacy_consent = {
            "production_access": {
                "version": 1,
                "account_id": str(cid),
                "state": "active",
                "source": "legacy_user_is_active",
                "source_account_id": str(cid),
            }
        }
        tx.add(
            MergeProof(
                id="synthetic-contact-link",
                source_id=cid,
                target_id=telegram_aid,
                secret_hash=digest("synthetic-secret"),
                expires_at=now() + timedelta(minutes=5),
            )
        )
    accounts.confirm_telegram_link(telegram_aid, "synthetic-contact-link.synthetic-secret")
    with db.transaction() as tx:
        assert tx.get(Alias, telegram_aid).canonical_id == cid
        assert tx.get(Subscription, sid).account_id == cid
    assert PanelContactSync(commerce).run_pending() == 1
    assert current["email"] == "canonical@synthetic.test" and current["telegramId"] == 12345
    assert current["username"] == "legacy.stable" and current["id"] == 7
    assert calls.count("PATCH") == 1


def test_gate_withdrawn_during_account_lock_wait_denies_all_transport(live_contacts):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, current_thread

    from sqlalchemy import event

    db, commerce, aid, _, _, calls, _ = live_contacts
    service = enqueue(db, commerce, aid)
    waiting = Event()

    def before_lock(connection, cursor, statement, parameters, context, executemany):
        if (
            current_thread().name.startswith("contact-lock")
            and "FROM v5_accounts" in statement
            and "FOR UPDATE" in statement
        ):
            connection.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
            waiting.set()

    event.listen(db.engine, "before_cursor_execute", before_lock)
    try:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="contact-lock") as pool:
            with db.transaction() as tx:
                tx.scalar(select(Account).where(Account.id == aid).with_for_update())
                pending = pool.submit(service.run_pending)
                assert waiting.wait(5)
                with db.transaction() as gates:
                    gates.get(Setting, "production_runtime_gates").value = {"panel_mutation": False}
            assert pending.result(timeout=5) == 0
    finally:
        event.remove(db.engine, "before_cursor_execute", before_lock)
    assert calls == []


def test_real_merge_during_contact_get_fences_old_goal_and_routes_new_canonical(live_contacts):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from sunrise_infrastructure.db import MergeProof
    from sunrise_infrastructure.security import digest

    db, commerce, aid, sid, current, calls, handlers = live_contacts
    accounts = identity_accounts(live_contacts)
    with db.transaction() as tx:
        source = tx.get(Account, aid)
        source.email, source.email_verified = None, False
        target = Account(email="merged@synthetic.test", email_verified=True)
        tx.add(target)
        tx.flush()
        canonical = target.id
        target.legacy_consent = {
            "production_access": {
                "version": 1,
                "account_id": str(canonical),
                "state": "active",
                "source": "legacy_user_is_active",
                "source_account_id": str(canonical),
            }
        }
        tx.add(
            MergeProof(
                id="synthetic-merge-race",
                source_id=canonical,
                target_id=aid,
                secret_hash=digest("synthetic-secret"),
                expires_at=now() + timedelta(minutes=5),
            )
        )
    service = enqueue(db, commerce, aid)
    entered, release = Event(), Event()

    def blocked():
        handlers.pop("on_get")
        entered.set()
        assert release.wait(5)

    handlers["on_get"] = blocked
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(service.run_pending)
        assert entered.wait(5)
        accounts.confirm_telegram_link(aid, "synthetic-merge-race.synthetic-secret")
        release.set()
        assert pending.result(timeout=5) == 0
    assert calls == ["GET"]
    assert service.run_pending() == 1
    with db.transaction() as tx:
        assert tx.get(Subscription, sid).account_id == canonical
        assert tx.scalar(select(Job).where(Job.kind == "panel_contact")).account_id == canonical
    assert current["email"] == "merged@synthetic.test" and current["username"] == "legacy.stable"
    assert calls.count("PATCH") == 1
