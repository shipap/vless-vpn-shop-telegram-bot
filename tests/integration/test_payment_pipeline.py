from decimal import Decimal as D
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from sunrise_core.errors import DomainError
from sunrise_infrastructure.billing import Billing
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import Account, Inbox, ProviderOperation, now

from tests.isolated_database import isolated_database
from tests.legal_fixture import accept_test_legal


class SyntheticProvider:
    terminal = "synthetic-terminal"

    def __init__(self):
        self.payments = {}
        self.timeout = False

    def create_payment(
        self, order_id, amount, currency, account_id, *, description=None,
        success_redirect_url=None, fail_redirect_url=None
    ):
        pid = "test-" + order_id
        self.payments[pid] = {
            "payment_id": pid,
            "order_id": order_id,
            "amount": amount,
            "payment_currency": currency,
            "customer_id": account_id,
            "test": True,
            "environment": "sandbox",
            "terminal_id": self.terminal,
            "status": "paid",
            "paid_at": now().isoformat(),
        }
        if self.timeout:
            raise DomainError("provider_outcome_unknown", "Результат уточняется.", 503)
        return {
            "order_id": order_id,
            "payment_id": pid,
            "pay_url": "https://pay.rollypay.io/invoice/synthetic",
            "test": True,
            "environment": "sandbox",
        }

    def payment(self, pid):
        return self.payments[pid]


@pytest.fixture
def pipeline():
    with isolated_database("payment_pipeline") as database:
        connection = database.engine.connect()
        transaction = connection.begin()
        database.sessions = sessionmaker(
            bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        try:
            c = Commerce(database, False)
            with c.db.transaction() as s:
                a = Account(email=f"{uuid4()}@example.test", bonus_balance=D(100))
                s.add(a)
                s.flush()
                aid = a.id
            accept_test_legal(Commerce(c.db), a.id)
            q = c.quote(aid, "standard", 30, "purchase", bonus_amount="49.50")
            o = c.create_order(aid, q["id"], str(uuid4()))
            yield c, Billing(c, "synthetic-terminal", "sandbox"), aid, o, SyntheticProvider()
        finally:
            transaction.rollback()
            connection.close()


def enqueue(c, o):
    with c.db.transaction() as s:
        row = Inbox(event_key=str(uuid4()), payload={"payment_id": "test-" + o["id"], "order_id": o["id"]})
        s.add(row)
        s.flush()
        return row.id


def test_durable_inbox_recovers_unknown_create_once(pipeline):
    c, b, aid, o, p = pipeline
    p.timeout = True
    with pytest.raises(DomainError):
        b.prepare_payment(o["id"], p)
    iid = enqueue(c, o)
    assert b.reconcile_inbox(p) == 1
    assert c.order(aid, o["id"])["status"] == "paid"
    assert len(c.dashboard(aid)["payments"]) == 1
    assert b.reconcile_inbox(p) == 0
    with c.db.transaction() as s:
        assert s.get(Inbox, iid).state == "processed"
        assert (
            s.scalar(select(ProviderOperation).where(ProviderOperation.order_id == o["id"])).state
            == "reconciled"
        )


@pytest.mark.parametrize("malformed", [None, []])
def test_malformed_create_json_retains_unknown_state_and_recovers_without_second_create(pipeline, malformed):
    import json

    import httpx
    from sunrise_infrastructure.providers import RollyPay

    c, b, aid, order, synthetic = pipeline
    requests = []

    def transport(request):
        requests.append(request.method)
        if request.method == "POST":
            body = json.loads(request.content)
            synthetic.create_payment(
                body["order_id"], body["amount"], body["payment_currency"], body["customer_id"]
            )
            return httpx.Response(
                200, content=json.dumps(malformed), headers={"Content-Type": "application/json"}
            )
        return httpx.Response(200, json=synthetic.payment("test-" + order["id"]))

    provider = RollyPay(
        "https://payments.example.test",
        "synthetic",
        synthetic.terminal,
        {"payments.example.test"},
        transport=httpx.MockTransport(transport),
    )
    with pytest.raises(DomainError):
        b.prepare_payment(order["id"], provider)
    assert b.prepare_payment(order["id"], provider)["state"] == "outcome_unknown"
    enqueue(c, order)
    assert b.reconcile_inbox(provider) == 1
    assert c.order(aid, order["id"])["status"] == "paid"
    assert len(c.dashboard(aid)["payments"]) == 1 and requests.count("POST") == 1


def test_cancel_releases_reserve_and_bad_event_does_not_starve_queue(pipeline):
    c, b, aid, o, p = pipeline
    b.prepare_payment(o["id"], p)
    p.payments["test-" + o["id"]]["status"] = "canceled"
    with c.db.transaction() as s:
        s.add(Inbox(event_key=str(uuid4()), payload={"invalid": "shape"}))
    enqueue(c, o)
    b.reconcile_inbox(p)
    assert c.order(aid, o["id"])["status"] == "canceled"
    assert D(c.dashboard(aid)["bonus_reserved"]) == 0
    p.payments["test-" + o["id"]]["status"] = "paid"
    enqueue(c, o)
    b.reconcile_inbox(p)
    assert c.order(aid, o["id"])["status"] == "reconciliation_required"
    assert D(c.dashboard(aid)["bonus_reserved"]) == 0


def test_authorized_sandbox_order_keeps_local_safety_and_cannot_use_fake_payment(pipeline):
    existing, _, aid, _, _ = pipeline
    commerce = Commerce(existing.db, local_test=True, sandbox_payments=True)
    quote = commerce.quote(aid, "standard", 30, "purchase")
    order = commerce.create_order(aid, quote["id"], str(uuid4()))
    assert commerce.local_test is True
    assert order["environment"] == "sandbox"
    assert commerce.catalog()["capabilities"]["payments"] == "sandbox"
    with pytest.raises(DomainError):
        commerce.test_pay(aid, order["id"])
    assert commerce.order(aid, order["id"])["status"] == "awaiting_payment"


def test_order_exposes_verified_persisted_checkout_link_until_payment(pipeline):
    commerce, billing, aid, order, provider = pipeline
    billing.prepare_payment(order["id"], provider)
    url = "https://pay.rollypay.io/invoice/synthetic"
    with commerce.db.transaction() as session:
        operation = session.scalar(select(ProviderOperation).where(ProviderOperation.order_id == order["id"]))
        operation.pay_url = url
    assert commerce.order(aid, order["id"])["checkout_url"] == url
    enqueue(commerce, order)
    billing.reconcile_inbox(provider)
    assert commerce.order(aid, order["id"])["checkout_url"] is None


@pytest.mark.parametrize(
    "url",
    [
        "http://pay.rollypay.io/invoice/synthetic",
        "https://pay.rollypay.io.attacker.test/invoice",
        "https://user:password@pay.rollypay.io/invoice",
        "https://pay.rollypay.io:8443/invoice",
        "https://pay.rollypay.io/invoice#fragment",
        "https://pay.rollypay.io/invoice\n",
        "https://attacker.test/invoice",
    ],
)
def test_order_never_exposes_unverified_checkout_destination(pipeline, url):
    commerce, billing, aid, order, provider = pipeline
    billing.prepare_payment(order["id"], provider)
    with commerce.db.transaction() as session:
        operation = session.scalar(select(ProviderOperation).where(ProviderOperation.order_id == order["id"]))
        operation.pay_url = url
    assert commerce.order(aid, order["id"])["checkout_url"] is None


def test_pending_provider_status_keeps_checkout_link_without_paid_access(pipeline):
    commerce, billing, aid, order, provider = pipeline
    billing.prepare_payment(order["id"], provider)
    provider.payments["test-" + order["id"]]["status"] = "pending"
    with commerce.db.transaction() as session:
        operation = session.scalar(select(ProviderOperation).where(ProviderOperation.order_id == order["id"]))
        operation.pay_url = "https://pay.rollypay.io/invoice/synthetic"
    enqueue(commerce, order)
    billing.reconcile_inbox(provider)
    assert commerce.order(aid, order["id"])["checkout_url"] == "https://pay.rollypay.io/invoice/synthetic"
    assert commerce.dashboard(aid)["subscriptions"] == []


@pytest.mark.parametrize(
    "context",
    [
        {"terminal_id": None},
        {"terminal_id": "another-terminal"},
        {"test": False},
        {"test": None},
        {"test": "true"},
        {"environment": "live"},
    ],
)
def test_provider_lookup_must_confirm_sandbox_and_merchant_before_paid_access(pipeline, context):
    commerce, billing, aid, order, provider = pipeline
    billing.prepare_payment(order["id"], provider)
    fact = provider.payments["test-" + order["id"]]
    fact.update(context)
    for key, value in context.items():
        if value is None and key != "test":
            del fact[key]
    inbox_id = enqueue(commerce, order)
    billing.reconcile_inbox(provider)
    assert commerce.order(aid, order["id"])["status"] == "awaiting_payment"
    assert commerce.dashboard(aid)["subscriptions"] == []
    with commerce.db.transaction() as session:
        assert session.get(Inbox, inbox_id).state == "review"


@pytest.mark.parametrize(
    "context", [{"test": False}, {"test": None}, {"environment": "live"}, {"environment": None}]
)
def test_create_response_cannot_publish_payment_link_without_sandbox_proof(pipeline, context):
    commerce, billing, aid, order, provider = pipeline
    create = provider.create_payment

    def response(**body):
        result = create(**body)
        result.update(context)
        return result

    provider.create_payment = response
    result = billing.prepare_payment(order["id"], provider)
    assert result == {"state": "review", "checkout_url": None}
    assert commerce.order(aid, order["id"])["checkout_url"] is None


def test_known_sandbox_poll_persists_once_and_survives_pending_restart(pipeline):
    from datetime import timedelta

    commerce, _, aid, order, provider = pipeline
    provider.terminal = "poll-" + str(uuid4())
    billing = Billing(commerce, provider.terminal, "sandbox")
    billing.prepare_payment(order["id"], provider)
    provider.payments["test-" + order["id"]]["status"] = "pending"
    assert billing.poll_known_payments(limit=1) == 1
    assert billing.poll_known_payments(limit=1) == 0
    with commerce.db.transaction() as session:
        row = session.scalar(select(Inbox).where(Inbox.payload["order_id"].as_string() == order["id"]))
        inbox_id = row.id
    assert billing._reconcile_one(inbox_id, provider) is True
    assert commerce.dashboard(aid)["subscriptions"] == []
    provider.payments["test-" + order["id"]]["status"] = "paid"
    with commerce.db.transaction() as session:
        session.get(Inbox, inbox_id).available_at = now() - timedelta(seconds=1)
    restarted = Billing(Commerce(commerce.db), provider.terminal, "sandbox")
    assert restarted.poll_known_payments(limit=1) == 0
    assert restarted._reconcile_one(inbox_id, provider) is True
    assert restarted._reconcile_one(inbox_id, provider) is True
    assert len(commerce.dashboard(aid)["payments"]) == 1
    assert len(commerce.dashboard(aid)["subscriptions"]) == 1
    assert restarted.poll_known_payments(limit=1) == 0


def test_poll_does_not_touch_unknown_creation_live_or_other_terminal(pipeline):
    from sunrise_infrastructure.db import Order

    commerce, _, _, order, provider = pipeline
    provider.terminal = "poll-" + str(uuid4())
    billing = Billing(commerce, provider.terminal, "sandbox")
    assert billing.poll_known_payments(limit=1) == 0
    billing.prepare_payment(order["id"], provider)
    with commerce.db.transaction() as session:
        operation = session.scalar(select(ProviderOperation).where(ProviderOperation.order_id == order["id"]))
        operation.terminal = "unrelated-merchant"
    assert billing.poll_known_payments(limit=1) == 0
    with commerce.db.transaction() as session:
        operation = session.scalar(select(ProviderOperation).where(ProviderOperation.order_id == order["id"]))
        operation.terminal = provider.terminal
        session.get(Order, order["id"]).environment = "live"
    assert billing.poll_known_payments(limit=1) == 0
    with commerce.db.transaction() as session:
        session.get(Order, order["id"]).environment = "sandbox"
        operation = session.scalar(select(ProviderOperation).where(ProviderOperation.order_id == order["id"]))
        operation.provider_id, operation.state = None, "outcome_unknown"
    assert billing.poll_known_payments(limit=1) == 0


@pytest.mark.parametrize("limit", [0, 51, True])
def test_poll_limit_is_bounded(pipeline, limit):
    _, billing, _, _, _ = pipeline
    with pytest.raises(ValueError):
        billing.poll_known_payments(limit=limit)


def test_get_without_test_flag_requires_exact_saved_creation_proof(pipeline):
    commerce, billing, aid, order, provider = pipeline
    billing.prepare_payment(order["id"], provider)
    del provider.payments["test-" + order["id"]]["test"]
    inbox_id = enqueue(commerce, order)
    assert billing._reconcile_one(inbox_id, provider) is True
    assert commerce.order(aid, order["id"])["status"] == "paid"


@pytest.mark.parametrize(
    "change", ["proof_missing", "proof_other_payment", "owner_missing", "environment_missing"]
)
def test_get_missing_test_cannot_infer_sandbox_from_operation_state(pipeline, change):
    commerce, billing, aid, order, provider = pipeline
    billing.prepare_payment(order["id"], provider)
    fact = provider.payments["test-" + order["id"]]
    del fact["test"]
    with commerce.db.transaction() as session:
        operation = session.scalar(select(ProviderOperation).where(ProviderOperation.order_id == order["id"]))
        request = dict(operation.request)
        if change == "proof_missing":
            request.pop("sandbox_creation", None)
        elif change == "proof_other_payment":
            request["sandbox_creation"] = {
                "test": True,
                "environment": "sandbox",
                "payment_id": "another-payment",
                "order_id": order["id"],
            }
        elif change == "owner_missing":
            del fact["customer_id"]
        elif change == "environment_missing":
            del fact["environment"]
        operation.request = request
    enqueue(commerce, order)
    billing.reconcile_inbox(provider)
    assert commerce.order(aid, order["id"])["status"] == "awaiting_payment"
    assert commerce.dashboard(aid)["subscriptions"] == []


def test_worker_one_time_sandbox_does_not_send_synthetic_agreements_to_provider(pipeline, monkeypatch):
    import json
    from datetime import timedelta

    import fakeredis
    import httpx
    from sunrise_infrastructure.db import Agreement, Subscription
    from sunrise_infrastructure.providers import RollyPay
    from sunrise_infrastructure.settings import Settings
    from sunrise_worker import main as worker_main

    commerce, _, aid, order, synthetic = pipeline
    synthetic.terminal = "worker-" + str(uuid4())
    calls = []

    def transport(request):
        calls.append(request.url.path)
        if request.url.path.startswith("/api/v1/subscriptions"):
            pytest.fail("One-time sandbox permission must never query synthetic agreements externally")
        if request.method == "POST":
            body = json.loads(request.content)
            result = synthetic.create_payment(
                body["order_id"], body["amount"], body["payment_currency"], body["customer_id"]
            )
            synthetic.payments[result["payment_id"]]["status"] = "pending"
            return httpx.Response(200, json=result)
        payment_id = request.url.path.rsplit("/", 1)[-1]
        return (
            httpx.Response(200, json=synthetic.payment(payment_id))
            if payment_id in synthetic.payments
            else httpx.Response(404)
        )

    provider = RollyPay(
        "https://payments.example.test",
        "synthetic",
        synthetic.terminal,
        {"payments.example.test"},
        transport=httpx.MockTransport(transport),
    )
    Billing(commerce, provider.terminal, "sandbox").prepare_payment(order["id"], provider)
    with commerce.db.transaction() as session:
        subscription = Subscription(
            account_id=aid, product_id="standard", expires_at=now() + timedelta(days=30), devices_limit=4
        )
        session.add(subscription)
        session.flush()
        agreement = Agreement(
            account_id=aid,
            subscription_id=subscription.id,
            provider_id="synthetic-agreement-" + str(uuid4()),
            state="active",
            consent_amount=D("99.00"),
            currency="RUB",
            consent={"synthetic": True},
            created_at=now() - timedelta(days=3650),
        )
        session.add(agreement)
        session.flush()
        agreement_id = agreement.id
    monkeypatch.setattr(worker_main, "Database", lambda url: commerce.db)
    worker = worker_main.Worker(
        Settings(
            _env_file=None,
            database_url=commerce.db.engine.url.render_as_string(hide_password=False),
            secret_key="x" * 32,
            local_test=True,
        ),
        redis_client=fakeredis.FakeRedis(),
        provider=provider,
    )
    worker.tick()
    assert any(path.startswith("/api/v1/payments/") for path in calls)
    with commerce.db.transaction() as session:
        assert session.get(Agreement, agreement_id).consent == {"synthetic": True}
