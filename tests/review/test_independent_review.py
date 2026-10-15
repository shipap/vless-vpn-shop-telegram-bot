"""Independent regressions; unique synthetic accounts, no destructive fixtures."""

import json
import os
from datetime import timedelta
from decimal import Decimal as D
from uuid import uuid4

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url
from sunrise_api.main import create_app
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import Account, Database, Product, now
from sunrise_infrastructure.settings import Settings

from tests.legal_fixture import accept_test_legal


@pytest.fixture
def review_db():
    url = os.getenv("SUNRISE_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Requires isolated synthetic PostgreSQL")
    parsed = make_url(url)
    assert parsed.database == "sunrise_test_backend" and parsed.port == 55439
    assert parsed.host in {"127.0.0.1", "localhost"}
    settings = Settings(database_url=url)
    db = Database(settings.database_url)
    with db.transaction() as s:
        a = Account(email=f"review-{uuid4()}@example.test", email_verified=True)
        p = Product(
            included_devices=4,
            currency="RUB",
            id=str(uuid4()),
            code=str(uuid4()),
            name="Проверка",
            tier_level=1,
            price=D("99"),
            extra_device_price=D("50"),
        )
        s.add_all([a, p])
        s.flush()
        aid, pid = a.id, p.id
    accept_test_legal(Commerce(db), a.id)
    yield db, aid, pid
    db.engine.dispose()


def test_annual_device_addon_is_fixed_for_the_remaining_term(review_db, monkeypatch):
    db, aid, pid = review_db
    c = Commerce(db, local_test=True)
    start = now()
    monkeypatch.setattr("sunrise_infrastructure.commerce.now", lambda: start)
    q = c.quote(aid, pid, 365, "purchase")
    o = c.create_order(aid, q["id"], str(uuid4()))
    c.test_pay(aid, o["id"])
    sid = c.dashboard(aid)["subscriptions"][0]["id"]
    monkeypatch.setattr("sunrise_infrastructure.commerce.now", lambda: start + timedelta(days=182.5))
    addon = c.quote(aid, pid, 365, "devices", sid, extra_devices=1)
    # V9: 50 per new device for the whole remaining paid term, without proration.
    assert D(addon["total"]) == D("50.00")


def test_late_payment_retains_financial_fact_and_requires_reconciliation(review_db, monkeypatch):
    db, aid, pid = review_db
    c = Commerce(db, local_test=True)
    start = now()
    monkeypatch.setattr("sunrise_infrastructure.commerce.now", lambda: start)
    q = c.quote(aid, pid, 30, "purchase")
    o = c.create_order(aid, q["id"], str(uuid4()))
    monkeypatch.setattr("sunrise_infrastructure.commerce.now", lambda: start + timedelta(days=31))
    paid = c.test_pay(aid, o["id"])
    assert len(c.dashboard(aid)["payments"]) == 1
    assert paid["status"] == "reconciliation_required"
    assert not c.dashboard(aid)["subscriptions"]


@pytest.fixture
def review_app(review_db, tmp_path):
    db, _, _ = review_db
    email = f"review-login-{uuid4()}@example.test"
    settings = Settings(
        database_url=db.engine.url.render_as_string(hide_password=False),
        local_test=True,
        email_allowlist=[email],
        admin_email_allowlist=[],
        allowed_hosts=["testserver"],
        allowed_origins=["http://testserver"],
        mail_dir=str(tmp_path),
    )
    app = create_app(settings, redis_client=fakeredis.FakeRedis())
    with TestClient(app, raise_server_exceptions=False) as client:
        yield app, client, email, tmp_path


def test_nonadmin_write_without_csrf_still_returns_real_404(review_app):
    _, client, email, mail = review_app
    cid = client.post(
        "/api/v1/auth/email/request", json={"email": email, "turnstile_token": "local-test"}
    ).json()["challenge_id"]
    code = json.loads((mail / f"{cid}.json").read_text(encoding="utf-8"))["code"]
    assert (
        client.post(
            "/api/v1/auth/email/verify",
            json={
                "challenge_id": cid,
                "code": code,
                "legal_accepted": True,
                "documents": client.get("/api/v1/legal").json()["documents"],
            },
        ).status_code
        == 200
    )
    response = client.patch(
        "/api/v1/admin/recurring", json={"enabled": False, "reason": "Проверка", "confirmed": True}
    )
    assert response.status_code == 404


def test_unexpected_error_preserves_safe_russian_error_contract(review_app, monkeypatch):
    app, client, _, _ = review_app

    def broken():
        raise RuntimeError("review-private-marker")

    monkeypatch.setattr(app.state.commerce, "catalog", broken)
    response = client.get("/api/v1/catalog")
    assert response.status_code == 500
    assert "review-private-marker" not in response.text
    assert response.headers.get("content-type", "").startswith("application/json")
    error = response.json()["error"]
    assert error["correlation_id"]
    assert any("\u0400" <= ch <= "\u04ff" for ch in error["message"])


def test_private_exception_never_escapes_to_asgi_server_logger(review_app, monkeypatch):
    app, _, _, _ = review_app

    def broken():
        raise RuntimeError("synthetic-private-customer-detail")

    monkeypatch.setattr(app.state.commerce, "catalog", broken)
    # Uvicorn logs every escaped exception with exc_info, even after a 500 response.
    with TestClient(app, raise_server_exceptions=True) as strict_client:
        try:
            response = strict_client.get("/api/v1/catalog")
        except Exception:  # noqa: BLE001 -- intentional synthetic regression trap and test-only dummy signature secret
            pytest.fail(
                "Private exception escaped the application into the server logging boundary", pytrace=False
            )
    assert response.status_code == 500


def test_streamed_body_limit_is_enforced_without_content_length(review_app):
    _, client, email, _ = review_app
    data = json.dumps({"email": email, "turnstile_token": "x" * 70000}).encode()
    response = client.post(
        "/api/v1/auth/email/request",
        content=(data[i : i + 4096] for i in range(0, len(data), 4096)),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413


def test_concurrent_verified_delivery_grants_single_payment_and_right(review_db):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sunrise_infrastructure.billing import Billing, VerifiedPayment

    db, aid, pid = review_db
    c = Commerce(db, local_test=True)
    quote = c.quote(aid, pid, 30, "purchase")
    order = c.create_order(aid, quote["id"], str(uuid4()))
    fact = VerifiedPayment(
        "review",
        str(uuid4()),
        "review-terminal",
        "local_test",
        order["id"],
        D(order["total"]),
        order["currency"],
        now(),
    )
    billing = Billing(c, "review-terminal", "local_test")
    barrier = Barrier(4)

    def deliver(_):
        barrier.wait(timeout=10)
        return billing.accept(fact)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(deliver, range(4)))
    assert len({row["id"] for row in results}) == 1
    dashboard = c.dashboard(aid)
    assert len(dashboard["payments"]) == len(dashboard["subscriptions"]) == 1
    assert len(dashboard["subscriptions"][0]["segments"]) == 1


def test_concurrent_merge_proof_consumed_once_preserves_money(review_db):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sunrise_core.errors import DomainError
    from sunrise_infrastructure.accounts import Accounts
    from sunrise_infrastructure.security import RateGuard

    db, aid, _ = review_db
    c = Commerce(db, local_test=True)
    accounts = Accounts(
        db,
        RateGuard(fakeredis.FakeRedis()),
        Settings(database_url=db.engine.url.render_as_string(hide_password=False)),
    )
    telegram = int(uuid4().int % 1000000000000) + 100000
    target = int(c.resolve_telegram(telegram)["id"])
    accept_test_legal(c, target)
    with db.transaction() as s:
        s.get(Account, target).bonus_balance = D("12.34")
    proof = accounts.request_link(aid, str(telegram), now())["proof"]
    barrier = Barrier(2)

    def merge(_):
        barrier.wait(timeout=10)
        try:
            accounts.confirm_telegram_link(target, proof)
            return "merged"
        except DomainError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(merge, range(2)))
    assert sorted(outcomes) == ["link_invalid", "merged"]
    assert D(c.dashboard(aid)["bonus_balance"]) == D("12.34")


def test_payment_resolves_alias_created_between_alias_read_and_account_lock(review_db):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, current_thread

    from sqlalchemy import event
    from sunrise_infrastructure.accounts import Accounts
    from sunrise_infrastructure.billing import Billing, VerifiedPayment
    from sunrise_infrastructure.security import RateGuard

    db, aid, pid = review_db
    c = Commerce(db, local_test=True)
    accounts = Accounts(
        db,
        RateGuard(fakeredis.FakeRedis()),
        Settings(database_url=db.engine.url.render_as_string(hide_password=False)),
    )
    telegram = int(uuid4().int % 1000000000000) + 100000
    target = int(c.resolve_telegram(telegram)["id"])
    accept_test_legal(c, target)
    quote = c.quote(target, pid, 30, "purchase")
    order = c.create_order(target, quote["id"], str(uuid4()))
    fact = VerifiedPayment(
        "review",
        str(uuid4()),
        "review-terminal",
        "local_test",
        order["id"],
        D(order["total"]),
        order["currency"],
        now(),
    )
    proof = accounts.request_link(aid, str(telegram), now())["proof"]
    reached, release = Event(), Event()

    def pause_before_account_lock(conn, cursor, statement, parameters, context, many):
        if (
            current_thread().name.startswith("review-payment")
            and "FROM v5_accounts" in statement
            and "FOR UPDATE" in statement
            and not reached.is_set()
        ):
            reached.set()
            assert release.wait(timeout=10)

    event.listen(db.engine, "before_cursor_execute", pause_before_account_lock)
    try:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="review-payment") as pool:
            future = pool.submit(Billing(c, "review-terminal", "local_test").accept, fact)
            try:
                assert reached.wait(timeout=10)
                accounts.confirm_telegram_link(target, proof)
            finally:
                release.set()
            future.result(timeout=10)
    finally:
        event.remove(db.engine, "before_cursor_execute", pause_before_account_lock)
    dashboard = c.dashboard(aid)
    assert len(dashboard["payments"]) == len(dashboard["subscriptions"]) == 1


@pytest.mark.parametrize("operation", ["quote", "dashboard", "cancel"])
def test_reenrollment_selects_current_agreement_instead_of_stopped_history(review_db, operation):
    from sunrise_core.errors import DomainError
    from sunrise_infrastructure.db import Agreement

    db, aid, pid = review_db
    c = Commerce(db, local_test=True)
    quote = c.quote(aid, pid, 30, "purchase")
    order = c.create_order(aid, quote["id"], str(uuid4()))
    c.test_pay(aid, order["id"])
    sid = c.dashboard(aid)["subscriptions"][0]["id"]
    with db.transaction() as s:
        old = Agreement(
            account_id=aid,
            subscription_id=sid,
            provider_id=str(uuid4()),
            state="stopped",
            consent_amount=D("99"),
            currency="RUB",
        )
        s.add(old)
        s.flush()
        active = Agreement(
            account_id=aid,
            subscription_id=sid,
            provider_id=str(uuid4()),
            state="enabled",
            consent_amount=D("99"),
            currency="RUB",
        )
        s.add(active)
        s.flush()
        active_id = active.id
    if operation == "quote":
        with pytest.raises(DomainError) as error:
            c.quote(aid, pid, 30, "renew", sid)
        assert error.value.code == "agreement_change_required"
    elif operation == "dashboard":
        assert c.dashboard(aid)["subscriptions"][0]["recurring"]["id"] == active_id
    else:
        assert c.cancel_agreement(aid, sid) == {"id": active_id, "state": "stop_pending"}


def test_refund_of_unrewarded_duplicate_payment_does_not_reverse_reward(review_db):
    from sunrise_infrastructure.billing import Billing, VerifiedPayment

    db, aid, pid = review_db
    c = Commerce(db, local_test=True)
    with db.transaction() as s:
        ref = Account(email=f"review-ref-{uuid4()}@example.test")
        s.add(ref)
        s.flush()
        rid = ref.id
        s.get(Account, aid).referrer_id = rid
    accept_test_legal(Commerce(db), ref.id)
    q = c.quote(aid, pid, 30, "purchase")
    o = c.create_order(aid, q["id"], str(uuid4()))
    b = Billing(c, "review-terminal", "local_test")
    facts = [
        VerifiedPayment(
            "review",
            str(uuid4()),
            "review-terminal",
            "local_test",
            o["id"],
            D(o["total"]),
            o["currency"],
            now(),
        )
        for _ in range(2)
    ]
    b.accept(facts[0])
    b.accept(facts[1])
    earned = c.dashboard(rid)["bonus_balance"]
    b.refund(facts[1].provider_id, str(uuid4()), facts[1].amount, now())
    assert c.dashboard(rid)["bonus_balance"] == earned
    b.refund(facts[0].provider_id, str(uuid4()), facts[0].amount, now())
    assert D(c.dashboard(rid)["bonus_balance"]) == 0


def test_future_paid_tier_is_applied_by_worker_at_segment_boundary(review_db, monkeypatch):
    from sunrise_infrastructure.db import Subscription

    db, aid, pid = review_db
    c = Commerce(db, local_test=True)
    start = now()
    monkeypatch.setattr("sunrise_infrastructure.commerce.now", lambda: start)
    with db.transaction() as s:
        p = Product(
            included_devices=4,
            currency="RUB",
            id=str(uuid4()),
            code=str(uuid4()),
            name="Следующий тариф",
            tier_level=2,
            price=D("249"),
            extra_device_price=D("50"),
        )
        s.add(p)
        s.flush()
        next_pid = p.id
    q = c.quote(aid, pid, 30, "purchase")
    o = c.create_order(aid, q["id"], str(uuid4()))
    c.test_pay(aid, o["id"])
    sid = c.dashboard(aid)["subscriptions"][0]["id"]
    q2 = c.quote(aid, next_pid, 30, "renew", sid)
    o2 = c.create_order(aid, q2["id"], str(uuid4()))
    c.test_pay(aid, o2["id"])
    c.run_jobs()
    with db.transaction() as s:
        assert s.get(Subscription, sid).product_id == pid
    monkeypatch.setattr("sunrise_infrastructure.commerce.now", lambda: start + timedelta(days=31))
    c.run_jobs()
    with db.transaction() as s:
        assert s.get(Subscription, sid).product_id == next_pid


def test_ip_lockout_has_targeted_persistent_admin_record(review_db):
    from sqlalchemy import select
    from sunrise_core.errors import DomainError
    from sunrise_infrastructure.accounts import Accounts
    from sunrise_infrastructure.db import Block, Challenge
    from sunrise_infrastructure.security import RateGuard, digest

    db, _, _ = review_db
    settings = Settings(database_url=db.engine.url.render_as_string(hide_password=False))
    guard = RateGuard(fakeredis.FakeRedis())
    accounts = Accounts(db, guard, settings)
    ip = "review-ip-" + str(uuid4())
    for _ in range(3):
        email = f"review-budget-{uuid4()}@example.test"
        cid = str(uuid4())
        with db.transaction() as s:
            s.add(
                Challenge(
                    id=cid,
                    email=email,
                    digest=accounts._code_digest(cid, "111111"),
                    context=digest(ip + ":" + email),
                    expires_at=now() + timedelta(minutes=10),
                )
            )
        for _ in range(10):
            with pytest.raises(DomainError):
                accounts.verify_code(cid, "000000", ip)
    with pytest.raises(DomainError):
        guard.check("ip:" + digest(ip))
    with db.transaction() as s:
        block = s.scalar(select(Block).where(Block.context == "ip:" + digest(ip), Block.revoked_at.is_(None)))
        assert block is not None, "An enforced IP block must be individually visible and revocable by admin"


@pytest.mark.parametrize("standard_slot_price", ["50", "100"])
def test_downgrade_preserves_value_of_paid_extra_devices(review_db, monkeypatch, standard_slot_price):
    from datetime import datetime

    db, aid, standard = review_db
    c = Commerce(db, local_test=True)
    start = now()
    monkeypatch.setattr("sunrise_infrastructure.commerce.now", lambda: start)
    with db.transaction() as s:
        s.get(Product, standard).extra_device_price = D(standard_slot_price)
        p = Product(
            included_devices=4,
            currency="RUB",
            id=str(uuid4()),
            code=str(uuid4()),
            name="Универсальный для проверки",
            tier_level=2,
            price=D("249"),
            extra_device_price=D("50"),
        )
        s.add(p)
        s.flush()
        universal = p.id
    q = c.quote(aid, universal, 30, "purchase")
    o = c.create_order(aid, q["id"], str(uuid4()))
    c.test_pay(aid, o["id"])
    sid = c.dashboard(aid)["subscriptions"][0]["id"]
    device = c.quote(aid, universal, 30, "devices", sid, extra_devices=1)
    device_order = c.create_order(aid, device["id"], str(uuid4()))
    c.test_pay(aid, device_order["id"])
    # Conversion uses the paid comparable snapshot, even after admin edits the catalog.
    with db.transaction() as s:
        s.get(Product, standard).extra_device_price = D("999")
    down = c.quote(aid, standard, 30, "downgrade", sid)
    resulting_seconds = D(str((datetime.fromisoformat(down["resulting_expires_at"]) - start).total_seconds()))
    # Retained slots use each tier's saved comparable device price.
    assert resulting_seconds * (D("99") + D(standard_slot_price)) / D(30 * 86400) <= D("299.00")
    assert D("299.00") - resulting_seconds * (D("99") + D(standard_slot_price)) / D(30 * 86400) < (
        D("99") + D(standard_slot_price)
    ) / D(30 * 86400)


def test_merge_preserves_orders_with_same_scoped_idempotency_key(review_db):
    from sunrise_infrastructure.accounts import Accounts
    from sunrise_infrastructure.security import RateGuard

    db, aid, pid = review_db
    c = Commerce(db, local_test=True)
    accounts = Accounts(
        db,
        RateGuard(fakeredis.FakeRedis()),
        Settings(database_url=db.engine.url.render_as_string(hide_password=False)),
    )
    telegram_id = uuid4().int % 1000000000000 + 100000
    target = c.resolve_telegram(telegram_id)
    accept_test_legal(c, target["id"])
    orders = []
    for owner in (aid, target["id"]):
        q = c.quote(owner, pid, 30, "purchase")
        o = c.create_order(owner, q["id"], "same-key-in-two-original-accounts")
        c.test_pay(owner, o["id"])
        orders.append(o)
    proof = accounts.request_link(aid, str(telegram_id), now())["proof"]
    accounts.confirm_telegram_link(target["id"], proof)
    assert len(c.dashboard(aid)["subscriptions"]) == 2
    assert len(c.dashboard(aid)["payments"]) == 2
    for order in orders:
        assert c.order(aid, order["id"])["id"] == order["id"]
        assert (
            c.create_order(aid, order["quote_id"], "same-key-in-two-original-accounts")["id"] == order["id"]
        )


def test_paid_after_cancel_cannot_spend_released_bonus_reservation(review_db):
    from sunrise_infrastructure.billing import Billing, VerifiedPayment
    from sunrise_infrastructure.db import Order

    db, aid, pid = review_db
    c = Commerce(db, local_test=True)
    with db.transaction() as s:
        s.get(Account, aid).bonus_balance = D("100")
    q = c.quote(aid, pid, 30, "purchase", bonus_amount="20.00")
    o = c.create_order(aid, q["id"], str(uuid4()))
    # Model the durable final-cancel transition, then a later authenticated payment.
    with db.transaction() as s:
        s.get(Order, o["id"]).status = "canceled"
        s.get(Account, aid).bonus_reserved = D(0)
    b = Billing(c, "review-terminal", "local_test")
    result = b.accept(
        VerifiedPayment(
            "review",
            str(uuid4()),
            "review-terminal",
            "local_test",
            o["id"],
            D(o["total"]),
            o["currency"],
            now(),
        )
    )
    assert result["status"] == "reconciliation_required"
    with db.transaction() as s:
        assert s.get(Account, aid).bonus_reserved == 0
        assert s.get(Account, aid).bonus_balance == D("100")


def test_signed_callback_reconciles_saved_provider_operation_once(review_db, monkeypatch):
    import hashlib
    import hmac
    import time

    from sqlalchemy import select
    from sunrise_infrastructure.billing import Billing
    from sunrise_infrastructure.db import Inbox, Payment
    from sunrise_infrastructure.security import digest

    db, aid, pid = review_db
    c = Commerce(db, local_test=False)
    q = c.quote(aid, pid, 30, "purchase")
    o = c.create_order(aid, q["id"], str(uuid4()))
    provider_id = "review-" + str(uuid4())

    class ProviderDouble:
        terminal = "review-terminal"

        def create_payment(self, **kwargs):
            assert kwargs["order_id"] == o["id"]
            return {
                "order_id": o["id"],
                "payment_id": provider_id,
                "pay_url": "https://pay.rollypay.io/invoice/synthetic-review",
                "test": True,
                "environment": "sandbox",
            }

        def payment(self, requested_id):
            assert requested_id == provider_id
            return {
                "order_id": o["id"],
                "payment_id": provider_id,
                "terminal_id": self.terminal,
                "test": True,
                "amount": o["total"],
                "payment_currency": o["currency"],
                "status": "paid",
                "paid_at": now().isoformat(),
            }

    provider = ProviderDouble()
    billing = Billing(c, provider.terminal, "sandbox")
    billing.prepare_payment(o["id"], provider)
    secret = "synthetic-review-signature-key"  # noqa: S105 -- intentional synthetic regression trap and test-only dummy signature secret
    # A payment callback double must stay isolated from an unrelated live SMTP tunnel.
    monkeypatch.setenv("SUNRISE_SMTP_TUNNEL_PORT", "1587")
    monkeypatch.setenv("SUNRISE_SMTP_TEST_AUTHORIZED", "false")
    settings = Settings(
        _env_file=None,
        database_url=db.engine.url.render_as_string(hide_password=False),
        secret_key="synthetic-review-secret-key-" * 3,
        local_test=False,
        smtp_tunnel_port=None,
        email_allowlist=[],
        allowed_hosts=["testserver"],
        rollypay_signing_secret=secret,
    )
    assert settings.smtp_tunnel_port is None and not settings.smtp_test_authorized
    app = create_app(settings, redis_client=fakeredis.FakeRedis())
    raw = json.dumps(
        {
            "payment_id": provider_id,
            "order_id": o["id"],
            "status": "paid",
            "amount": o["total"],
            "currency": o["currency"],
            "test": True,
        }
    ).encode()
    stamp = str(int(time.time()))
    signature = hmac.new(secret.encode(), stamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
    with TestClient(app) as client:
        assert (
            client.post(
                "/api/v1/webhooks/rollypay",
                content=raw,
                headers={"X-Timestamp": stamp, "X-Signature": "0" * 64},
            ).status_code
            == 403
        )
        for _ in range(2):
            assert (
                client.post(
                    "/api/v1/webhooks/rollypay",
                    content=raw,
                    headers={"X-Timestamp": stamp, "X-Signature": signature},
                ).status_code
                == 200
            )
    with db.transaction() as s:
        inbox = list(s.scalars(select(Inbox).where(Inbox.event_key == digest(raw.decode()))))
        assert len(inbox) == 1
        iid = inbox[0].id
    billing._reconcile_one(iid, provider)
    billing._reconcile_one(iid, provider)
    with db.transaction() as s:
        assert len(list(s.scalars(select(Payment).where(Payment.order_id == o["id"])))) == 1
        assert s.get(Inbox, iid).state == "processed"
    assert len(c.dashboard(aid)["subscriptions"]) == 1
