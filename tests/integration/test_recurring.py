import os
from datetime import timedelta
from decimal import Decimal as D
from uuid import uuid4

import pytest
from sqlalchemy import select
from sunrise_core.errors import DomainError
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import Account, Agreement, Database, Setting, now
from sunrise_infrastructure.recurring import Recurring

from tests.legal_fixture import accept_test_legal


class RecurringProvider:
    terminal = "test-recurring"
    recurring_authorized = True

    def __init__(self):
        self.created = {}
        self.stopped = set()
        self.payments = {}

    def create_payment(self, order_id, amount, currency, account_id, *, description=None):
        payment_id = "synthetic-recurring-worker-" + order_id
        self.payments[payment_id] = {
            "payment_id": payment_id,
            "order_id": order_id,
            "amount": amount,
            "payment_currency": currency,
            "customer_id": account_id,
            "terminal_id": self.terminal,
            "test": True,
            "environment": "sandbox",
            "status": "pending",
        }
        return {
            "payment_id": payment_id,
            "order_id": order_id,
            "pay_url": "https://pay.rollypay.io/invoice/synthetic-worker",
            "test": True,
            "environment": "sandbox",
        }

    def payment(self, payment_id):
        return self.payments[payment_id]

    def plans(self):
        return {
            "items": [
                {
                    "id": "plan-standard",
                    "payer_amount_rub": "99.00",
                    "merchant_amount_rub": "99.00",
                    "interval": "month",
                    "version": 1,
                }
            ]
        }

    def create_agreement(self, terms, key):
        return self.created.setdefault(
            key,
            {
                "id": "agreement-" + key,
                "plan_id": terms["plan_id"],
                "state": "new",
                "billing_status": "consent_pending",
                "pay_url": "https://pay.example.test/consent",
            },
        )

    def agreement(self, pid):
        return {
            "id": pid,
            "state": "stop" if pid in self.stopped else "active",
            "billing_status": "stopped" if pid in self.stopped else "enabled",
        }

    def stop_agreement(self, pid):
        self.stopped.add(pid)
        return self.agreement(pid)


@pytest.fixture
def recurring():
    url = os.getenv("SUNRISE_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Synthetic PostgreSQL required")
    c = Commerce(Database(url), True)
    with c.db.transaction() as s:
        a = Account(email=f"{uuid4()}@example.test")
        s.add(a)
        s.flush()
        aid = a.id
        row = s.get(Setting, "recurring")
        row.value = {"enabled": True}
    accept_test_legal(Commerce(c.db), a.id)
    q = c.quote(aid, "standard", 30, "purchase")
    o = c.create_order(aid, q["id"], str(uuid4()))
    c.test_pay(aid, o["id"])
    sid = c.dashboard(aid)["subscriptions"][0]["id"]
    yield c, Recurring(c, RecurringProvider()), aid, sid
    with c.db.transaction() as s:
        s.get(Setting, "recurring").value = {"enabled": False}


def test_enrollment_key_cycle_once_off_preserves_servicing_and_cancel(recurring):
    c, r, aid, sid = recurring
    key = str(uuid4())
    first = r.enroll(aid, sid, "plan-standard", True, key)
    second = r.enroll(aid, sid, "plan-standard", True, key)
    assert first["id"] == second["id"]
    with c.db.transaction() as s:
        s.get(Setting, "recurring").value = {"enabled": False}
    with pytest.raises(DomainError):
        r.enroll(aid, sid, "plan-standard", True, str(uuid4()))
    paid = {
        "payment_id": "cycle-" + str(uuid4()),
        "amount": "99.00",
        "payment_currency": "RUB",
        "status": "paid",
        "subscription_id": first["provider_id"],
        "paid_at": now().isoformat(),
        "test": True,
        "terminal_id": "test-recurring",
    }
    charge = {
        "cycle_number": 1,
        "payment_id": paid["payment_id"],
        "status": "payed",
        "validation_status": "accepted",
    }
    r.service_cycle(paid, charge)
    r.service_cycle(paid, charge)
    assert len(c.dashboard(aid)["subscriptions"][0]["segments"]) == 2
    assert r.cancel(aid, sid)["state"] == "stopped"
    assert r.loyalty(sid)["percent"] == "0"


def test_review_cycle_never_grants_days(recurring):
    c, r, aid, sid = recurring
    ag = r.enroll(aid, sid, "plan-standard", True, str(uuid4()))
    paid = {
        "payment_id": "cycle-" + str(uuid4()),
        "amount": "99.00",
        "payment_currency": "RUB",
        "status": "paid",
        "subscription_id": ag["provider_id"],
        "paid_at": now().isoformat(),
        "test": True,
        "terminal_id": "test-recurring",
    }
    with pytest.raises(DomainError):
        r.service_cycle(
            paid,
            {
                "cycle_number": 1,
                "payment_id": paid["payment_id"],
                "status": "payed",
                "validation_status": "review",
            },
        )
    assert len(c.dashboard(aid)["subscriptions"][0]["segments"]) == 1


def test_manual_renewal_requires_verified_stop_and_worker_services_when_off(recurring):
    import fakeredis
    from sunrise_infrastructure.db import RecurringAttempt
    from sunrise_infrastructure.settings import Settings
    from sunrise_worker.main import Worker

    c, r, aid, sid = recurring
    ag = r.enroll(aid, sid, "plan-standard", True, str(uuid4()))
    r.provider.charges = lambda pid: {
        "items": [{"cycle_number": 1, "status": "pending", "validation_status": "accepted"}]
    }
    with c.db.transaction() as s:
        s.get(Setting, "recurring").value = {"enabled": False}
    with pytest.raises(DomainError) as exc:
        c.quote(aid, "standard", 30, "renew", sid)
    assert exc.value.code == "agreement_change_required"
    c.cancel_agreement(aid, sid)
    with pytest.raises(DomainError):
        c.quote(aid, "standard", 30, "renew", sid)
    worker = Worker(
        Settings(database_url=os.environ["SUNRISE_TEST_DATABASE_URL"]), fakeredis.FakeRedis(), r.provider
    )
    for _ in range(50):
        worker.tick()
        if c.dashboard(aid)["subscriptions"][0]["recurring"]["state"] == "stopped":
            break
    assert c.dashboard(aid)["subscriptions"][0]["recurring"]["state"] == "stopped"
    assert c.quote(aid, "standard", 30, "renew", sid)["total"] == "99.00"
    with c.db.transaction() as s:
        assert (
            s.scalar(select(RecurringAttempt).where(RecurringAttempt.agreement_id == ag["id"])).state
            == "pending"
        )


def test_annual_prepayment_does_not_earn_instant_loyalty(recurring, monkeypatch):
    import sunrise_infrastructure.recurring as module
    from sunrise_infrastructure.db import Segment

    c, r, aid, sid = recurring
    ag = r.enroll(aid, sid, "plan-standard", True, str(uuid4()))
    moment = now()
    with c.db.transaction() as s:
        s.get(Agreement, ag["id"]).consent = s.get(Agreement, ag["id"]).consent | {
            "continuous_paid_since": moment.isoformat()
        }
        segment = s.scalar(select(Segment).where(Segment.subscription_id == sid))
        segment.starts_at = moment
        segment.ends_at = moment + timedelta(days=365)
    monkeypatch.setattr(module, "now", lambda: moment + timedelta(days=1))
    assert r.loyalty(sid)["percent"] == "0"
    monkeypatch.setattr(module, "now", lambda: moment + timedelta(days=61))
    assert r.loyalty(sid)["percent"] == "5" and r.loyalty(sid)["applied_to_external_amount"] is False


def test_accrued_loyalty_survives_canonical_merge_and_enrollment_off(recurring, monkeypatch):
    import fakeredis
    import sunrise_infrastructure.recurring as module
    from sunrise_infrastructure.accounts import Accounts
    from sunrise_infrastructure.db import Segment
    from sunrise_infrastructure.security import RateGuard
    from sunrise_infrastructure.settings import Settings

    c, r, aid, sid = recurring
    ag = r.enroll(aid, sid, "plan-standard", True, str(uuid4()))
    moment = now()
    telegram = int(uuid4().int % 10**12) + 100000
    with c.db.transaction() as s:
        s.get(Account, aid).email = None
        s.get(Account, aid).telegram_id = telegram
        receiver = Account(email=str(uuid4()) + "@example.test", email_verified=True)
        s.add(receiver)
        s.flush()
        rid = receiver.id
        row = s.get(Agreement, ag["id"])
        original_amount = row.consent_amount
        row.consent = row.consent | {"continuous_paid_since": (moment - timedelta(days=61)).isoformat()}
        seg = s.scalar(select(Segment).where(Segment.subscription_id == sid))
        seg.starts_at, seg.ends_at = moment - timedelta(days=61), moment + timedelta(days=304)
    accept_test_legal(Commerce(c.db), receiver.id)
    monkeypatch.setattr(module, "now", lambda: moment)
    before = r.loyalty(sid)
    accounts = Accounts(
        c.db, RateGuard(fakeredis.FakeRedis()), Settings(database_url=os.environ["SUNRISE_TEST_DATABASE_URL"])
    )
    proof = accounts.request_link(rid, str(telegram), now())["proof"]
    accounts.confirm_telegram_link(aid, proof)
    with c.db.transaction() as s:
        s.get(Setting, "recurring").value = {"enabled": False}
        assert s.get(Agreement, ag["id"]).account_id == rid
        assert s.get(Agreement, ag["id"]).consent_amount == original_amount
    assert r.loyalty(sid) == before and before["percent"] == "5"
    assert c.dashboard(aid)["account"]["id"] == str(rid)


def test_replayed_recurring_period_after_verified_stop_and_manual_renewal_never_duplicates(recurring):
    c, r, aid, sid = recurring
    ag = r.enroll(aid, sid, "plan-standard", True, str(uuid4()))
    paid = {
        "payment_id": "cycle-" + str(uuid4()),
        "amount": "99.00",
        "payment_currency": "RUB",
        "status": "paid",
        "subscription_id": ag["provider_id"],
        "paid_at": now().isoformat(),
        "test": True,
        "terminal_id": r.provider.terminal,
    }
    charge = {
        "cycle_number": 1,
        "payment_id": paid["payment_id"],
        "status": "payed",
        "validation_status": "accepted",
    }
    r.service_cycle(paid, charge)
    assert r.cancel(aid, sid)["state"] == "stopped"
    quote = c.quote(aid, "standard", 30, "renew", sid)
    manual = c.create_order(aid, quote["id"], str(uuid4()))
    c.test_pay(aid, manual["id"])
    before = c.dashboard(aid)["subscriptions"][0]
    r.service_cycle(paid, charge)
    r.service_cycle(paid, charge)
    after = c.dashboard(aid)["subscriptions"][0]
    assert len(after["segments"]) == len(before["segments"]) == 3
    assert after["expires_at"] == before["expires_at"]


def test_enrollment_timeout_is_reconciled_without_second_create_after_off(recurring):
    c, r, aid, sid = recurring
    key = str(uuid4())
    original = r.provider.create_agreement
    calls = []

    def timeout(body, idem):
        result = original(body, idem)
        calls.append(body)
        r.provider.list_agreements = lambda page: {
            "items": [result | {"merchant_subscription_ref": body["merchant_subscription_ref"]}]
        }
        raise DomainError("provider_outcome_unknown", "Сверка", 503)

    r.provider.create_agreement = timeout
    with pytest.raises(DomainError):
        r.enroll(aid, sid, "plan-standard", True, key)
    with c.db.transaction() as s:
        s.get(Setting, "recurring").value = {"enabled": False}
    recovered = r.enroll(aid, sid, "plan-standard", True, key)
    assert recovered["provider_id"] == "agreement-" + key and len(calls) == 1


def test_owner_loyalty_policy_and_best_secondary_manual_path(recurring, monkeypatch):
    import sunrise_infrastructure.commerce as commerce_module
    import sunrise_infrastructure.recurring as module
    from sunrise_infrastructure.db import Segment

    c, r, aid, sid = recurring
    ag = r.enroll(aid, sid, "plan-standard", True, str(uuid4()))
    moment = now()
    with c.db.transaction() as s:
        policy = s.get(Setting, "discounts")
        old = policy.value
        policy.value = old | {
            "loyalty_period_days": 30,
            "loyalty_milestones": [{"elapsed_periods": 2, "percent": "15"}],
            "renewal_campaign": {"enabled": True, "percent": "10"},
            "promos": [
                {
                    "code": "LOYALTEST",
                    "enabled": True,
                    "percent": "20",
                    "audience": "renewal",
                    "per_account_limit": 1,
                }
            ],
        }
        row = s.get(Agreement, ag["id"])
        row.state = "stopped"
        row.consent = row.consent | {"continuous_paid_since": (moment - timedelta(days=61)).isoformat()}
        seg = s.scalar(select(Segment).where(Segment.subscription_id == sid))
        seg.starts_at = moment - timedelta(days=61)
        seg.ends_at = moment + timedelta(days=304)
    try:
        monkeypatch.setattr(module, "now", lambda: moment)
        monkeypatch.setattr(commerce_module, "now", lambda: moment)
        loyalty = r.loyalty(sid)
        assert (
            loyalty["percent"] == "15"
            and loyalty["period_days"] == 30
            and loyalty["applied_to_external_amount"] is False
        )
        q = c.quote(aid, "standard", 30, "renew", sid)
        assert D(q["total"]) == D("84.15")
        promo = c.quote(aid, "standard", 30, "renew", sid, promo_code="LOYALTEST")
        assert D(promo["total"]) == D("79.20")  # ONE best secondary, no 15%+10%+20% stacking.
    finally:
        with c.db.transaction() as s:
            s.get(Setting, "discounts").value = old
