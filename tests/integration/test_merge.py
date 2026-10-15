import os
from decimal import Decimal as D
from uuid import uuid4

import fakeredis
import pytest
from sunrise_core.errors import DomainError
from sunrise_infrastructure.accounts import Accounts
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import Account, Alias, Database, now
from sunrise_infrastructure.security import RateGuard
from sunrise_infrastructure.settings import Settings

from tests.legal_fixture import accept_test_legal


def test_dual_identity_merge_preserves_both_purchases_and_single_use():
    url = os.getenv("SUNRISE_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Synthetic PostgreSQL required")
    db = Database(url)
    settings = Settings(database_url=url)
    accounts = Accounts(db, RateGuard(fakeredis.FakeRedis()), settings)
    c = Commerce(db, True)
    tg = int(uuid4().int % 1000000000000) + 100000
    target = c.resolve_telegram(tg)
    accept_test_legal(c, target["id"])
    with db.transaction() as s:
        a = Account(email=f"{uuid4()}@example.test", email_verified=True, bonus_balance=D("12.34"))
        s.add(a)
        s.flush()
        aid = a.id
        b = s.get(Account, int(target["id"]))
        b.credit_balance = D("10.50")
    accept_test_legal(Commerce(db), a.id)
    for owner in [aid, int(target["id"])]:
        q = c.quote(owner, "standard", 30, "purchase")
        o = c.create_order(owner, q["id"], str(uuid4()))
        c.test_pay(owner, o["id"])
    proof = accounts.request_link(aid, str(tg), now())["proof"]
    result = accounts.confirm_telegram_link(target["id"], proof)
    assert result["id"] == str(aid)
    data = c.dashboard(aid)
    assert len(data["subscriptions"]) == 2
    assert len(data["payments"]) == 2
    assert data["bonus_balance"] == "12.340000"
    with db.transaction() as s:
        assert s.get(Account, aid).credit_balance == D("10.50")
        assert s.get(Account, int(target["id"])) is None
        assert s.get(Alias, int(target["id"])).canonical_id == aid
    with pytest.raises(DomainError):
        accounts.confirm_telegram_link(target["id"], proof)


def test_link_proof_cannot_be_used_by_another_telegram():
    url = os.getenv("SUNRISE_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Synthetic PostgreSQL required")
    db = Database(url)
    accounts = Accounts(db, RateGuard(fakeredis.FakeRedis()), Settings(database_url=url))
    tg = int(uuid4().int % 1000000000000) + 100000
    Commerce(db).resolve_telegram(tg)
    with db.transaction() as s:
        a = Account(email=f"{uuid4()}@example.test", email_verified=True)
        s.add(a)
        s.flush()
        aid = a.id
    accept_test_legal(Commerce(db), a.id)
    proof = accounts.request_link(aid, str(tg), now())["proof"]
    with pytest.raises(DomainError):
        accounts.confirm_telegram_link(aid, proof)
