"""Real SQL authority checks with synthetic policy and HTTP; not production admission."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event, current_thread
from types import SimpleNamespace

import fakeredis
import httpx
import pytest
from sqlalchemy import event, select
from sunrise_core.errors import DomainError
from sunrise_infrastructure.accounts import Accounts
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import Account, Alias, Job, Notification, Setting, StaffRole, Subscription, now
from sunrise_infrastructure.panel_events import PanelEvents
from sunrise_infrastructure.panel_v3 import RemnawaveProduction, production_binding_key
from sunrise_infrastructure.promos import Promos
from sunrise_infrastructure.retention import import_retention_key, legacy_operation_key
from sunrise_infrastructure.security import RateGuard
from sunrise_infrastructure.settings import Settings

from tests.integration.test_production_business_races import SyntheticBusinessPolicy
from tests.integration.test_production_business_races import live_business as live_business
from tests.isolated_database import isolated_database
from tests.legal_fixture import accept_test_legal


@pytest.mark.parametrize("spent", [1, None, 0])
def test_real_permitted_merge_preserves_trial_history_and_root_source_account_hold(spent):
    with isolated_database("prod_alias_authority") as db:
        commerce = Commerce(db, local_test=True)
        with db.transaction() as tx:
            canonical = Account(
                email="canonical@synthetic.test",
                email_verified=True,
                legacy_consent={"legacy_free_trial_used": 0},
            )
            source = Account(telegram_id=77700003, legacy_consent={"legacy_free_trial_used": spent})
            tx.add_all([canonical, source])
            tx.flush()
            cid, source_id = canonical.id, source.id
            tx.add(Setting(key=legacy_operation_key(source_id, account=True), value={"state": "review"}))
        accept_test_legal(commerce, cid)
        accounts = Accounts(db, RateGuard(fakeredis.FakeRedis()), Settings(_env_file=None))
        proof = accounts.request_link(cid, "77700003", now())["proof"]
        accounts.confirm_telegram_link(source_id, proof)
        with db.transaction() as tx:
            assert tx.get(Alias, source_id).canonical_id == cid
            actor = tx.get(Account, cid)
            assert actor.legacy_consent["merged:" + str(source_id)]["legacy_free_trial_used"] == spent
            if spent == 0:
                Promos(commerce)._require_trial_in(tx, actor)
            else:
                with pytest.raises(DomainError) as blocked:
                    Promos(commerce)._require_trial_in(tx, actor)
                assert blocked.value.code == "trial_unavailable"
        with pytest.raises(DomainError) as blocked:
            commerce.quote(cid, "standard", 30, "purchase")
        assert blocked.value.code == "legacy_operation_review"
        with db.transaction() as tx:
            assert tx.get(Setting, legacy_operation_key(source_id, account=True)).value == {"state": "review"}
        assert commerce.dashboard(cid)["subscriptions"] == []


@pytest.mark.parametrize("evidence", [None, 0, 1])
def test_root_import_alias_without_source_account_requires_exact_unused_trial_evidence(
    live_business, evidence
):
    db, commerce, *_ = live_business
    with db.transaction() as tx:
        account = Account(
            email="ghost-alias@synthetic.test",
            email_verified=True,
            legacy_consent={"legacy_free_trial_used": 0},
        )
        tx.add(account)
        tx.flush()
        source_id = 9007199254740995
        assert tx.get(Account, source_id) is None
        tx.add(Alias(old_id=source_id, canonical_id=account.id))
        if evidence is not None:
            account.legacy_consent = account.legacy_consent | {
                "merged:" + str(source_id): {"legacy_free_trial_used": evidence}
            }
        aid = account.id
    with db.transaction() as tx:
        assert tx.get(Account, source_id) is None
        account = tx.get(Account, aid)
        if evidence == 0:
            Promos(commerce)._require_trial_in(tx, account)
        else:
            with pytest.raises(DomainError) as denied:
                Promos(commerce)._require_trial_in(tx, account)
            assert denied.value.code == "trial_unavailable"


@pytest.fixture(params=[4, None], ids=["finite", "explicit-null"])
def imported_cleanup(request):
    with isolated_database("prod_import_cleanup") as db:
        config = Settings(
            _env_file=None,
            admin_email_allowlist=["owner@synthetic.test"],
            admin_telegram_allowlist=["930000000001"],
            owner_id=None,
        )
        commerce = Commerce(db, local_test=True, public_settings=config)
        with db.transaction() as tx:
            owner = Account(telegram_id=930000000001, email="owner@synthetic.test", email_verified=True)
            customer = Account(email="customer@synthetic.test", email_verified=True)
            tx.add_all([owner, customer])
            tx.flush()
            aid = customer.id
            tx.add(StaffRole(account_id=owner.id, role="owner", granted_by=owner.id))
            expiry = now() - timedelta(days=8)
            sub = Subscription(
                id="imported-cleanup",
                account_id=aid,
                environment="live",
                status="expired",
                panel_user_id=8,
                panel_uuid="22222222-2222-4222-8222-222222222222",
                short_uuid="syntheticshortuuid123",
                connection_url="https://sub.synthetic.test/syntheticshortuuid123",
                expires_at=expiry,
                devices_limit=request.param,
            )
            tx.add(sub)
            tx.add(
                Setting(
                    key="production_admission",
                    value={"scope_id": "synthetic-pg-business", "synthetic_business_only": True},
                )
            )
            tx.add(Setting(key="production_runtime_gates", value={"retention": True, "panel_mutation": True}))
            tx.add(Setting(key="subscription_retention", value={"enabled": True, "days_after_expiry": 7}))
        policy = SyntheticBusinessPolicy(db)
        policy.scope.panel = SimpleNamespace(
            origin="https://panel.synthetic.test",
            contract="3.4_id",
            internal_squads=["11111111-1111-4111-8111-111111111111"],
            external_squads=[],
            subscription_hosts=["sub.synthetic.test"],
            node_uuids=[],
        )
        binding = {
            "scope_id": policy.scope.scope_id,
            "generation": 1,
            "origin_nonce": "a" * 32,
            "user_id": "8",
            "uuid": sub.panel_uuid,
            "short_uuid": sub.short_uuid,
            "username": "preserved_customer",
        }
        root_proof = {
            "scope_id": policy.scope.scope_id,
            "generation": 1,
            "origin": "imported_project_identity",
            "contract": "3.4_id",
            "panel_host": "panel.synthetic.test",
            "subscription_id": sub.id,
            "environment": "live",
            "account_id": str(aid),
            "binding": binding,
            "connection_url": sub.connection_url,
        }
        if request.param is None:
            root_proof["capacity_evidence"] = {
                "kind": "explicit_null_preserved",
                "present": True,
                "source": "authenticated_panel_snapshot",
                "until": expiry.isoformat(),
            }
        permission = {
            "version": 1,
            "state": "approved",
            "scope_id": policy.scope.scope_id,
            "environment": "live",
            "subscription_id": sub.id,
            "account_id": str(aid),
            "origin_nonce": binding["origin_nonce"],
            "panel_user_id": "8",
            "binding_generation": 1,
            "legacy_uuid": sub.panel_uuid,
            "evidence_sha256": "b" * 64,
            "approved_at": now().isoformat(),
        }
        state = {
            "calls": [],
            "user": {
                "id": 8,
                "username": binding["username"],
                "shortUuid": sub.short_uuid,
                "status": "EXPIRED",
                "expireAt": expiry.isoformat(),
                "hwidDeviceLimit": request.param,
                "activeInternalSquads": [{"uuid": policy.scope.panel.internal_squads[0]}],
                "subscriptionUrl": sub.connection_url,
                "telegramId": None,
                "trafficLimitBytes": 0,
                "trafficLimitStrategy": "NO_RESET",
            },
        }

        def handler(request):
            state["calls"].append((request.method, request.url.path))
            if request.method == "GET" and request.url.path == "/api/hwid/devices/8":
                return httpx.Response(200, json={"response": {"total": 0, "devices": []}})
            assert request.url.path == "/api/users/8"
            if request.method == "DELETE":
                state["user"] = None
                if state.get("timeout"):
                    raise httpx.ReadTimeout("Synthetic response lost")
                return httpx.Response(200, json={"response": {"isDeleted": True}})
            assert request.method == "GET"
            if state.get("after_get"):
                state.pop("after_get")()
            return (
                httpx.Response(200, json={"response": state["user"]})
                if state["user"]
                else httpx.Response(404)
            )

        panel = RemnawaveProduction(
            runtime_policy=policy,
            token="synthetic-token",  # noqa: S106 -- MockTransport only
            transport=httpx.MockTransport(handler),
        )
        commerce.local_test, commerce.runtime_policy, commerce.panel_provider = False, policy, panel
        with db.transaction() as tx:
            tx.add(Setting(key=production_binding_key(sub.id), value=root_proof))
            tx.add(Setting(key=import_retention_key(sub.id), value=permission))
        assert PanelEvents(db, panel, config).reconcile_owned(sub.id)
        yield db, commerce, state, sub.id, permission
        panel.client.close()


@pytest.mark.parametrize("revoke", [None, "permission", "retention", "panel_mutation"])
def test_import_cleanup_fresh_post_get_authority_and_exact_user_owner_intents(imported_cleanup, revoke):
    db, commerce, state, sid, _ = imported_cleanup
    assert commerce.retention.schedule_due() == 1
    if revoke:

        def withdraw():
            with db.transaction() as tx:
                if revoke == "permission":
                    tx.get(Setting, import_retention_key(sid)).value = {"state": "revoked"}
                else:
                    gates = tx.get(Setting, "production_runtime_gates")
                    gates.value = gates.value | {revoke: False}

        state["after_get"] = withdraw
    completed = commerce.retention.run_jobs()
    with db.transaction() as tx:
        error = tx.scalar(select(Job.last_error).where(Job.kind == "subscription_cleanup"))
    assert completed == (0 if revoke else 1), (error, state["calls"])
    assert sum(method == "DELETE" for method, _ in state["calls"]) == (0 if revoke else 1)
    with db.transaction() as tx:
        assert tx.get(Subscription, sid).panel_uuid == "22222222-2222-4222-8222-222222222222"
        assert tx.get(Setting, production_binding_key(sid)).value["origin"] == "imported_project_identity"
        assert len(list(tx.scalars(select(Notification)))) == (0 if revoke else 2)
        if not revoke:
            assert tx.get(Setting, "archive:" + sid) is not None
            from sunrise_infrastructure.subscription_archive import state_in

            archive = state_in(tx, tx.get(Subscription, sid))
            assert archive.device_limit == tx.get(Subscription, sid).devices_limit
            if archive.device_limit is None:
                notice = tx.get(Notification, archive.owner_intent_id)
                assert "Лимит не задан в панели" in notice.body
                assert "None" not in notice.body and archive.binding.uuid not in notice.body


def test_import_permission_withdrawn_after_recorded_unknown_delete_keeps_get_only_recovery(
    imported_cleanup, monkeypatch
):
    db, commerce, state, sid, _ = imported_cleanup
    state["timeout"] = True
    assert commerce.retention.schedule_due() == 1
    assert commerce.retention.run_jobs() == 0
    with db.transaction() as tx:
        tx.get(Setting, import_retention_key(sid)).value = {"state": "revoked"}
        job = tx.scalar(select(Job).where(Job.kind == "subscription_cleanup"))
        moment = job.available_at + timedelta(seconds=1)
    monkeypatch.setattr("sunrise_infrastructure.retention.now", lambda: moment)
    completed = commerce.retention.run_jobs()
    with db.transaction() as tx:
        error = tx.scalar(select(Job.last_error).where(Job.kind == "subscription_cleanup"))
    assert completed == 1, (error, state["calls"])
    assert sum(method == "DELETE" for method, _ in state["calls"]) == 1
    with db.transaction() as tx:
        assert len(list(tx.scalars(select(Notification)))) == 2


@pytest.mark.parametrize("changed", ["missing", "generation", "uuid"])
def test_archived_live_history_requires_current_exact_root_binding(imported_cleanup, changed):
    from sunrise_infrastructure.subscription_archive import state_in

    db, commerce, _, sid, _ = imported_cleanup
    assert commerce.retention.schedule_due() == 1
    assert commerce.retention.run_jobs() == 1
    with db.transaction() as tx:
        record = tx.get(Setting, production_binding_key(sid))
        if changed == "missing":
            tx.delete(record)
        elif changed == "generation":
            record.value = record.value | {"generation": 2}
        else:
            record.value = record.value | {
                "binding": record.value["binding"] | {"uuid": "33333333-3333-4333-8333-333333333333"}
            }
    with db.transaction() as tx:
        with pytest.raises(DomainError) as rejected:
            state_in(tx, tx.get(Subscription, sid))
        assert rejected.value.code == "subscription_archive_invalid"


@pytest.mark.parametrize("withdrawn", ["permission", "retention", "panel_mutation"])
def test_import_cleanup_rechecks_authority_after_actual_account_lock_wait(imported_cleanup, withdrawn):
    db, commerce, state, sid, _ = imported_cleanup
    assert commerce.retention.schedule_due() == 1
    fetched, release_http, waiting = Event(), Event(), Event()

    def pause_get():
        fetched.set()
        assert release_http.wait(5)

    def observe_wait(connection, cursor, statement, parameters, context, executemany):
        if (
            current_thread().name.startswith("import-cleanup-wait")
            and release_http.is_set()
            and "FROM v5_accounts" in statement
            and "FOR UPDATE" in statement
        ):
            connection.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
            waiting.set()

    state["after_get"] = pause_get
    event.listen(db.engine, "before_cursor_execute", observe_wait)
    try:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="import-cleanup-wait") as pool:
            work = pool.submit(commerce.retention.run_jobs)
            assert fetched.wait(5)
            with db.transaction() as held:
                aid = held.get(Subscription, sid).account_id
                held.scalar(select(Account).where(Account.id == aid).with_for_update())
                release_http.set()
                assert waiting.wait(5)
                with db.transaction() as tx:
                    if withdrawn == "permission":
                        tx.get(Setting, import_retention_key(sid)).value = {"state": "revoked"}
                    else:
                        gates = tx.get(Setting, "production_runtime_gates")
                        gates.value = gates.value | {withdrawn: False}
            assert work.result(timeout=10) == 0
    finally:
        release_http.set()
        event.remove(db.engine, "before_cursor_execute", observe_wait)
    assert not any(method == "DELETE" for method, _ in state["calls"])
    with db.transaction() as tx:
        assert tx.get(Setting, "archive:" + sid) is None
        assert tx.get(Subscription, sid).panel_user_id == 8
        assert not list(tx.scalars(select(Notification)))
