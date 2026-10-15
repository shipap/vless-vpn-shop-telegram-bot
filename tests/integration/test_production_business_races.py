"""Real PostgreSQL business races; synthetic policy is NOT production admission."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier, Event, current_thread
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select
from sunrise_core.errors import DomainError
from sunrise_infrastructure.billing import Billing
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import (
    Account,
    Alias,
    Device,
    DeviceUnlink,
    Inbox,
    Job,
    Ledger,
    Notification,
    NotificationDelivery,
    Order,
    Payment,
    Product,
    PromoClaim,
    ProviderOperation,
    Segment,
    Setting,
    StaffRole,
    Subscription,
    now,
)

from tests.isolated_database import isolated_database
from tests.legal_fixture import accept_test_legal


class SyntheticBusinessPolicy:
    """Exercises fresh marker/gate SQL without admitting a synthetic DB to production."""

    production = True

    def __init__(self, db):
        self.db = db
        self.scope = SimpleNamespace(
            scope_id="synthetic-pg-business",
            rollypay=SimpleNamespace(checkout_hosts=["checkout.synthetic.test"]),
        )

    def require_current(self, session, capability=None):
        marker = session.scalar(
            select(Setting)
            .where(Setting.key == "production_admission")
            .execution_options(populate_existing=True)
        )
        gates = session.scalar(
            select(Setting)
            .where(Setting.key == "production_runtime_gates")
            .execution_options(populate_existing=True)
        )
        if not marker or marker.value != {"scope_id": self.scope.scope_id, "synthetic_business_only": True}:
            raise DomainError("production_admission_denied", "Операция недоступна.", 503)
        if capability and (not gates or gates.value.get(capability) is not True):
            raise DomainError("production_stage_denied", "Операция недоступна.", 503)

    def require(self, capability=None):
        with self.db.transaction() as session:
            self.require_current(session, capability)


@pytest.fixture
def live_business():
    with isolated_database("prod_business") as db:
        commerce = Commerce(db, local_test=True)
        with db.transaction() as session:
            session.get(Product, "standard").internal_squad_uuid = "11111111-1111-4111-8111-111111111111"
            actor = Account(
                email="business-race@synthetic.test", email_verified=True, bonus_balance=Decimal(100)
            )
            session.add(actor)
            session.flush()
            aid = actor.id
        accept_test_legal(commerce, aid)
        original_quote = commerce.quote(aid, "standard", 30, "purchase")
        original = commerce.create_order(aid, original_quote["id"], "synthetic-original")
        commerce.test_pay(aid, original["id"])
        sid = commerce.order(aid, original["id"])["subscription_id"]
        quote = commerce.quote(aid, "standard", 30, "renew", sid, bonus_amount="5")
        pending = commerce.create_order(aid, quote["id"], "synthetic-new-renewal")
        with db.transaction() as session:
            session.get(Subscription, sid).environment = "live"
            order = session.get(Order, pending["id"])
            order.environment = "live"
            request = {
                "order_id": order.id,
                "amount": str(order.amount),
                "currency": order.currency,
                "account_id": str(aid),
                "environment": "live",
                "provider": "rollypay",
                "terminal": "synthetic-terminal",
                "scope_id": "synthetic-pg-business",
                "payment_method": "sbp",
            }
            session.add(
                ProviderOperation(
                    order_id=order.id,
                    provider_id="synthetic-payment",
                    terminal="synthetic-terminal",
                    state="created",
                    request=request,
                )
            )
            receipt = Inbox(
                event_key="synthetic-paid-receipt",
                state="pending",
                payload={"payment_id": "synthetic-payment", "order_id": order.id},
            )
            session.add(receipt)
            session.add(
                Setting(
                    key="production_admission",
                    value={"scope_id": "synthetic-pg-business", "synthetic_business_only": True},
                )
            )
            session.add(
                Setting(
                    key="production_runtime_gates",
                    value={
                        "financial_apply": True,
                        "payment_reconcile": True,
                        "panel_mutation": True,
                        "invoice_create": True,
                    },
                )
            )
            session.add(
                Setting(
                    key="legacy_promo_usage",
                    value={
                        "version": 1,
                        "scope_id": "synthetic-pg-business",
                        "environment": "live",
                        "complete": True,
                        "source_namespace": "legacy_shop_v1",
                        "source_snapshot_sha256": "a" * 64,
                        "evidence_sha256": "b" * 64,
                        "approved_at": now().isoformat(),
                        "codes": {},
                        "pending_grants": {},
                    },
                )
            )
            session.flush()
            iid = receipt.id
            fact = {
                "environment": "prod",
                "payment_id": "synthetic-payment",
                "order_id": order.id,
                "amount": str(order.amount),
                "payment_currency": order.currency,
                "payment_method": "sbp",
                "status": "paid",
                "paid_at": now().isoformat(),
            }
        commerce.local_test = False
        commerce.runtime_policy = SyntheticBusinessPolicy(db)
        yield db, commerce, aid, sid, pending["id"], iid, fact


def rights_snapshot(db, aid, sid):
    with db.transaction() as session:
        actor = session.get(Account, aid)
        sub = session.get(Subscription, sid)
        return (
            actor.bonus_balance,
            actor.bonus_reserved,
            sub.expires_at,
            sub.version,
            tuple(
                session.execute(
                    select(Segment.id, Segment.starts_at, Segment.ends_at, Segment.paid_value).order_by(
                        Segment.id
                    )
                )
            ),
            tuple(session.execute(select(Ledger.id, Ledger.amount).order_by(Ledger.id))),
            tuple(session.scalars(select(Payment.id).order_by(Payment.id))),
        )


@pytest.mark.parametrize("locked", [Account, Order, Subscription])
def test_gate_revoked_during_actual_lock_wait_rolls_back_money_and_preserves_durable_receipt(
    live_business, locked
):
    db, commerce, aid, sid, oid, iid, fact = live_business
    before = rights_snapshot(db, aid, sid)
    waiting = Event()
    table = locked.__tablename__

    def gate(connection, cursor, statement, parameters, context, executemany):
        if (
            current_thread().name.startswith("live-gate-wait")
            and f"FROM {table}" in statement
            and "FOR UPDATE" in statement
        ):
            connection.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
            waiting.set()

    get_ids = []

    def payment(identifier):
        get_ids.append(identifier)
        return dict(fact)

    billing = Billing(commerce, "synthetic-terminal", "live")
    event.listen(db.engine, "before_cursor_execute", gate)
    try:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="live-gate-wait") as pool:
            with db.transaction() as holder:
                key = aid if locked is Account else oid if locked is Order else sid
                holder.scalar(select(locked).where(locked.id == key).with_for_update())
                future = pool.submit(billing._reconcile_one, iid, SimpleNamespace(payment=payment))
                assert waiting.wait(5), "Expected actual SQL lock acquisition was not reached"
                with db.transaction() as control:
                    row = control.get(Setting, "production_runtime_gates")
                    row.value = row.value | {"financial_apply": False}
            with pytest.raises(DomainError) as failure:
                future.result(timeout=10)
            assert failure.value.code == "production_stage_denied"
    finally:
        event.remove(db.engine, "before_cursor_execute", gate)
    assert rights_snapshot(db, aid, sid) == before
    with db.transaction() as session:
        assert session.get(Inbox, iid).state == "pending"
        assert session.get(Order, oid).status == "awaiting_payment"
        row = session.get(Setting, "production_runtime_gates")
        row.value = row.value | {"financial_apply": True}
    assert billing._reconcile_one(iid, SimpleNamespace(payment=payment)) is True
    after = rights_snapshot(db, aid, sid)
    assert after[0] == before[0] - Decimal(5) and after[1] == before[1] - Decimal(5)
    assert len(after[4]) == len(before[4]) + 1 and len(after[6]) == len(before[6]) + 1
    assert billing._reconcile_one(iid, SimpleNamespace(payment=payment)) is True
    assert rights_snapshot(db, aid, sid) == after
    assert set(get_ids) == {"synthetic-payment"}


@pytest.mark.parametrize("operation", ["fulfill", "claim", "boundary"])
def test_actual_canonical_merge_and_live_claim_or_boundary_use_one_lock_order(
    live_business, monkeypatch, operation
):
    import json

    import fakeredis
    import httpx
    from sunrise_infrastructure import commerce as commerce_module
    from sunrise_infrastructure import panel_entitlements
    from sunrise_infrastructure.accounts import Accounts
    from sunrise_infrastructure.panel_v3 import RemnawaveProduction
    from sunrise_infrastructure.security import RateGuard
    from sunrise_infrastructure.settings import Settings

    db, commerce, aid, sid, _, iid, fact = live_business
    billing = Billing(commerce, "synthetic-terminal", "live")
    if operation != "fulfill":
        assert billing._reconcile_one(iid, SimpleNamespace(payment=lambda _: dict(fact)))
    with db.transaction() as session:
        actor = session.get(Account, aid)
        actor.email, actor.email_verified, actor.telegram_id = None, False, 77700001
        owner = Account(email="canonical-source@synthetic.test", email_verified=True)
        session.add(owner)
        session.flush()
        owner_id = owner.id
        jid = (
            session.scalar(select(Job.id).where(Job.payload["subscription_id"].as_string() == sid))
            if operation == "fulfill"
            else session.scalar(select(Job.id).where(Job.payload["order_id"].as_string() == fact["order_id"]))
        )
        assert jid
        future_segment = session.scalar(select(Segment).where(Segment.order_id == fact["order_id"]))
        boundary = future_segment.starts_at if future_segment else None
        if operation == "boundary":
            session.get(Job, jid).state = "done"
            future_segment.devices = 6
    accept_test_legal(commerce, owner_id)
    accounts = Accounts(
        db,
        RateGuard(fakeredis.FakeRedis()),
        Settings(_env_file=None, database_url=db.engine.url.render_as_string(hide_password=False)),
    )
    proof = accounts.request_link(owner_id, "77700001", now())["proof"]
    state = {"user": None, "calls": []}

    def handler(request):
        state["calls"].append(request.method)
        if request.method == "POST":
            body = json.loads(request.content)
            state["user"] = body | {
                "id": 8,
                "activeInternalSquads": [{"uuid": "11111111-1111-4111-8111-111111111111"}],
                "subscriptionUrl": "https://sub.synthetic.test/" + body["shortUuid"],
            }
        return httpx.Response(200, json={"response": state["user"]}) if state["user"] else httpx.Response(404)

    commerce.runtime_policy.scope.panel = SimpleNamespace(
        origin="https://panel.synthetic.test",
        contract="3.4_id",
        internal_squads=["11111111-1111-4111-8111-111111111111"],
        external_squads=[],
        subscription_hosts=["sub.synthetic.test"],
        node_uuids=[],
    )
    commerce.panel_provider = RemnawaveProduction(
        runtime_policy=commerce.runtime_policy,
        token="synthetic-token",  # noqa: S106 -- synthetic MockTransport only
        transport=httpx.MockTransport(handler),
    )  # noqa: S106 -- synthetic HTTP
    if operation == "boundary":
        monkeypatch.setattr(commerce_module, "now", lambda: boundary)
        monkeypatch.setattr(panel_entitlements, "now", lambda: boundary)
    held, waiting = Event(), Event()

    def after_merge_lock(connection, cursor, statement, parameters, context, executemany):
        if (
            current_thread().name.startswith("live-canonical-merge")
            and "FROM v5_accounts" in statement
            and "FOR UPDATE" in statement
            and not held.is_set()
        ):
            connection.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
            held.set()
            assert waiting.wait(5), "Actual production operation never reached its first account lock"

    def before_worker_lock(connection, cursor, statement, parameters, context, executemany):
        if (
            current_thread().name.startswith("live-canonical-work")
            and "FROM v5_accounts" in statement
            and "FOR UPDATE" in statement
        ):
            connection.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
            waiting.set()

    event.listen(db.engine, "after_cursor_execute", after_merge_lock)
    event.listen(db.engine, "before_cursor_execute", before_worker_lock)
    try:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="live-canonical-merge") as merges:
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="live-canonical-work") as workers:
                merge = merges.submit(accounts.confirm_telegram_link, aid, proof)
                assert held.wait(5)
                work = (
                    workers.submit(billing._reconcile_one, iid, SimpleNamespace(payment=lambda _: dict(fact)))
                    if operation == "fulfill"
                    else workers.submit(commerce.provisioning._locked, jid, sid)
                    if operation == "claim"
                    else workers.submit(commerce.apply_segment_boundaries)
                )
                assert merge.result(timeout=10)["id"] == str(owner_id)
                result = work.result(timeout=10)
    finally:
        event.remove(db.engine, "after_cursor_execute", after_merge_lock)
        event.remove(db.engine, "before_cursor_execute", before_worker_lock)
    with db.transaction() as session:
        assert session.get(Alias, aid).canonical_id == owner_id
        assert session.get(Subscription, sid).account_id == owner_id
        assert session.get(Job, jid).account_id == owner_id
        if operation == "fulfill":
            assert result is True
            assert session.scalar(select(Payment.id).where(Payment.provider_id == "synthetic-payment"))
            assert session.scalar(select(Segment.id).where(Segment.order_id == fact["order_id"]))
            assert state["calls"] == []
        elif operation == "claim":
            assert result == 1 and session.get(Job, jid).state == "done"
            assert state["calls"].count("POST") == 1
            assert session.get(Subscription, sid).panel_user_id == 8
        else:
            assert session.get(Subscription, sid).devices_limit == 6
            assert session.scalar(select(Job.id).where(Job.key.like("segment:%")))
            assert state["calls"] == []


def test_live_verified_device_post_owner_busy_rolls_back_then_recovers_by_get_without_second_post(
    live_business,
):
    import json
    from datetime import timedelta

    import httpx
    from sunrise_infrastructure.customer_operations import CustomerOperations
    from sunrise_infrastructure.panel_v3 import RemnawaveProduction
    from sunrise_infrastructure.settings import Settings

    db, commerce, aid, sid, _, iid, fact = live_business
    assert Billing(commerce, "synthetic-terminal", "live")._reconcile_one(
        iid, SimpleNamespace(payment=lambda _: dict(fact))
    )
    state = {"user": None, "devices": [], "calls": []}

    def handler(request):
        state["calls"].append((request.method, request.url.path))
        if request.url.path.startswith("/api/hwid/devices"):
            if request.method == "POST":
                state["devices"] = []
            return httpx.Response(
                200, json={"response": {"total": len(state["devices"]), "devices": state["devices"]}}
            )
        if request.method == "POST":
            body = json.loads(request.content)
            state["user"] = body | {
                "id": 8,
                "activeInternalSquads": [{"uuid": "11111111-1111-4111-8111-111111111111"}],
                "subscriptionUrl": "https://sub.synthetic.test/" + body["shortUuid"],
            }
        return httpx.Response(200, json={"response": state["user"]}) if state["user"] else httpx.Response(404)

    commerce.runtime_policy.scope.panel = SimpleNamespace(
        origin="https://panel.synthetic.test",
        contract="3.4_id",
        internal_squads=["11111111-1111-4111-8111-111111111111"],
        external_squads=[],
        subscription_hosts=["sub.synthetic.test"],
        node_uuids=[],
    )
    commerce.panel_provider = RemnawaveProduction(
        runtime_policy=commerce.runtime_policy,
        token="synthetic-token",  # noqa: S106 -- synthetic MockTransport only
        transport=httpx.MockTransport(handler),
    )  # noqa: S106 -- synthetic HTTP only
    assert commerce.provisioning.run_jobs() == 1
    with db.transaction() as session:
        owner = Account(
            email="unlink-owner@synthetic.test", email_verified=True, telegram_id=730000000000002
        )
        session.add(owner)
        session.flush()
        session.add(StaffRole(account_id=owner.id, role="owner", granted_by=owner.id))
        device = Device(
            subscription_id=sid, name="Synthetic device", panel_hwid="synthetic-hwid", generation=1
        )
        session.add(device)
        session.flush()
        owner_id, did = owner.id, device.id
    config = Settings(
        _env_file=None, admin_email_allowlist=["unlink-owner@synthetic.test"],
        admin_telegram_allowlist=["730000000000002"], owner_id=None, local_test=False
    )
    config.runtime_mode = "production"
    object.__setattr__(config, "_runtime_policy", commerce.runtime_policy)
    commerce.public_settings = config
    state["devices"] = [
        {
            "userId": 8,
            "hwid": "synthetic-hwid",
            "deviceModel": "Synthetic device",
            "createdAt": now().isoformat(),
            "updatedAt": now().isoformat(),
        }
    ]
    CustomerOperations(commerce).revoke_devices(
        aid, sid, [did], "synthetic-device-unlink", confirmed=True, expected_generations={did: 1}
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        with db.transaction() as holder:
            holder.scalar(select(Account).where(Account.id == owner_id).with_for_update())
            assert executor.submit(commerce.provisioning.run_jobs).result(timeout=10) == 0
    with db.transaction() as session:
        job = session.scalar(select(Job).where(Job.kind == "device_revoke"))
        assert job.state == "in_flight" and job.payload["revoke_started"] is True
        assert session.scalar(select(DeviceUnlink.id)) is None
        assert session.get(Device, did).revoked is False
        assert session.scalar(select(Notification.id).where(Notification.kind == "device_unlink")) is None
        job.available_at = now() - timedelta(seconds=1)
    assert len([call for call in state["calls"] if call == ("POST", "/api/hwid/devices/delete")]) == 1
    assert commerce.provisioning.run_jobs() == 1
    assert commerce.provisioning.run_jobs() == 0
    assert len([call for call in state["calls"] if call == ("POST", "/api/hwid/devices/delete")]) == 1
    with db.transaction() as session:
        assert len(list(session.scalars(select(DeviceUnlink.id)))) == 1
        assert session.get(Device, did).revoked is True
        notices = list(session.scalars(select(Notification).where(Notification.kind == "device_unlink")))
        assert len(notices) == 1 and notices[0].account_id == owner_id and notices[0].audience == "owner"
        assert (
            len(
                list(
                    session.scalars(
                        select(NotificationDelivery).where(
                            NotificationDelivery.notification_id == notices[0].id
                        )
                    )
                )
            )
            == 1
        )


@pytest.mark.parametrize("trial", [True, False])
def test_actual_concurrent_gift_claims_consume_once_without_paid_fiction(live_business, trial):
    from sunrise_infrastructure.promos import Promos, validate_promo

    db, commerce, _, _, _, _, _ = live_business
    with db.transaction() as session:
        subject = Account(email="gift-concurrency@synthetic.test", email_verified=True)
        session.add(subject)
        session.flush()
        aid = subject.id
        subject.legacy_consent = {
            "legacy_free_trial_used": 0,
            "production_access": {
                "version": 1,
                "account_id": str(aid),
                "state": "active",
                "source": "legacy_user_is_active",
                "source_account_id": str(aid),
            },
        }
        policy = session.get(Setting, "discounts")
        policy.value = policy.value | {
            "promos": [
                validate_promo(
                    {
                        "code": "GIFT",
                        "reward_type": "free_days",
                        "reward_days": 3,
                        "product_id": "standard",
                        "trial": trial,
                    }
                )
            ]
        }
    accept_test_legal(commerce, aid)
    promos = Promos(commerce)
    promos.activate(aid, "GIFT")
    barrier = Barrier(2)
    waiting = set()

    def synchronize_initial_read(connection, cursor, statement, parameters, context, executemany):
        name = current_thread().name
        if (
            name.startswith("gift-concurrency")
            and "FROM v6_promo_claims" in statement
            and "count(" not in statement
            and name not in waiting
        ):
            waiting.add(name)
            barrier.wait(timeout=5)

    event.listen(db.engine, "after_cursor_execute", synchronize_initial_read)
    try:
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="gift-concurrency") as pool:
            first, second = pool.submit(promos.claim, aid, "GIFT"), pool.submit(promos.claim, aid, "GIFT")
            assert first.result(timeout=10) == second.result(timeout=10)
    finally:
        event.remove(db.engine, "after_cursor_execute", synchronize_initial_read)
    with db.transaction() as session:
        claims = list(session.scalars(select(PromoClaim).where(PromoClaim.account_id == aid)))
        assert len(claims) == 1
        segment = session.get(Segment, claims[0].segment_id)
        assert segment.kind == "promo_gift" and segment.paid_value == 0 and segment.order_id is None
        sub = session.get(Subscription, segment.subscription_id)
        assert sub.environment == "live" and sub.devices_limit == segment.devices == 4
        assert len(list(session.scalars(select(Job.id).where(Job.key == "promo-gift:" + claims[0].id)))) == 1
        assert session.scalar(select(Payment.id).where(Payment.account_id == aid)) is None
        assert session.scalar(select(Order.id).where(Order.account_id == aid)) is None
        if trial:
            assert session.get(Setting, "promo_trial:" + str(aid)).value["claim_id"] == claims[0].id


@pytest.mark.parametrize("method", ["crypto", "xrocket", "sbp"])
def test_actual_db_quote_order_and_provider_floor_preserve_excess_bonus_and_exact_method(
    live_business, method
):
    import json

    import httpx
    from sunrise_infrastructure.providers import RollyPay

    db, commerce, _, _, _, _, _ = live_business
    with db.transaction() as session:
        subject = Account(
            email="method-floor@synthetic.test",
            email_verified=True,
            bonus_balance=Decimal(1000),
            credit_balance=Decimal(1000),
            credit_currency="RUB",
        )
        session.add(subject)
        session.flush()
        aid = subject.id
        methods = {
            "version": 1,
            "minimum_subscription_amount": "1.00",
            "methods": [
                {
                    "id": code,
                    "label": code,
                    "provider": "rollypay",
                    "provider_method": code,
                    "discount_percent": "0" if code == "sbp" else "4",
                    "enabled": True,
                    "currencies": ["RUB"],
                    "mapping_contract": "synthetic-reviewed-contract",
                }
                for code in ("crypto", "xrocket", "sbp")
            ],
        }
        session.add(Setting(key="payment_methods", value=methods))
        discounts = session.get(Setting, "discounts")
        discounts.value = discounts.value | {"cap": "100", "bonus_cap": "100"}
    accept_test_legal(commerce, aid)
    quote = commerce.quote(
        aid, "standard", 30, "purchase", payment_method=method, bonus_amount="1000", credit_amount="1000"
    )
    assert quote["total"] == "1.00" and Decimal(quote["bonus_amount"]) < Decimal(1000)
    assert Decimal(quote["credit_amount"]) == 0
    order = commerce.create_order(aid, quote["id"], "real-shared-minimum")
    assert order["total"] == "1.00"
    commerce.runtime_policy.scope.rollypay.origin = "https://bank.synthetic.test"
    commerce.runtime_policy.scope.rollypay.terminal = "synthetic-terminal"
    requests = []

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(
            200,
            json={
                "environment": "prod",
                "order_id": body["order_id"],
                "payment_id": "synthetic-floor",
                "amount": body["amount"],
                "payment_currency": body["payment_currency"],
                "payment_method": body["payment_method"],
                "pay_url": "https://checkout.synthetic.test/own",
            },
        )

    provider = RollyPay(
        "https://bank.synthetic.test",
        "synthetic-key",
        "synthetic-terminal",
        {"bank.synthetic.test"},
        transport=httpx.MockTransport(handle),
        environment="live",
        runtime_policy=commerce.runtime_policy,
    )
    assert (
        Billing(commerce, "synthetic-terminal", "live").prepare_payment(order["id"], provider)["state"]
        == "created"
    )
    assert len(requests) == 1 and requests[0]["amount"] == "1.00" and requests[0]["test"] is False
    assert requests[0]["payment_method"] == method and requests[0]["customer_id"] == str(aid)
    stale_quote = commerce.quote(aid, "standard", 30, "purchase", payment_method=method)
    with db.transaction() as session:
        subject = session.get(Account, aid)
        assert subject.bonus_balance == Decimal(1000) and subject.bonus_reserved == Decimal(
            quote["bonus_amount"]
        )
        assert subject.credit_balance == Decimal(1000) and subject.credit_reserved == 0
        methods = session.get(Setting, "payment_methods")
        methods.value = methods.value | {"version": 2}
    with pytest.raises(DomainError) as stale:
        commerce.create_order(aid, stale_quote["id"], "new-stale-quote")
    # Existing order is idempotent even if pricing policy later changes.
    assert stale.value.code == "quote_stale"
    assert commerce.create_order(aid, quote["id"], "same-existing-quote")["id"] == order["id"]
    assert len(requests) == 1
