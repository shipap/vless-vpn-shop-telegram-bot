"""Проверка отказа внутри настоящей транзакции объединения, без внешних интеграций."""

import os
from decimal import Decimal
from uuid import uuid4

import fakeredis
import pytest
from sqlalchemy import event, select
from sqlalchemy.engine import make_url
from sunrise_infrastructure.accounts import Accounts
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import Account, Alias, Database, MergeProof, Payment, Subscription, now
from sunrise_infrastructure.security import RateGuard
from sunrise_infrastructure.settings import Settings

from tests.legal_fixture import accept_test_legal


def test_mid_merge_failure_rolls_back_money_rights_and_proof_then_retry_preserves_both():
    url = os.getenv("SUNRISE_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Нужна отдельная синтетическая PostgreSQL БД")
    config = Settings(database_url=url)
    parsed = make_url(config.database_url)
    assert parsed.database == "sunrise_test_backend"
    assert parsed.host in {"127.0.0.1", "localhost"} and parsed.port == 55439
    db = Database(url)
    commerce = Commerce(db, local_test=True)
    accounts = Accounts(db, RateGuard(fakeredis.FakeRedis()), config)
    telegram = int(uuid4().int % 1000000000000) + 100000
    source_id = int(commerce.resolve_telegram(telegram)["id"])
    accept_test_legal(commerce, source_id)
    with db.transaction() as session:
        target = Account(email=f"{uuid4()}@example.test", email_verified=True, bonus_balance=Decimal("12.34"))
        session.add(target)
        session.flush()
        target_id = target.id
        source = session.get(Account, source_id)
        source.credit_balance = Decimal("23.45")
        source.bonus_balance = Decimal("6.78")
    accept_test_legal(Commerce(db), target.id)
    for account_id in (source_id, target_id):
        quote = commerce.quote(account_id, "standard", 30, "purchase")
        order = commerce.create_order(account_id, quote["id"], str(uuid4()))
        commerce.test_pay(account_id, order["id"])
    proof = accounts.request_link(target_id, str(telegram), now())["proof"]
    reached_partial_updates = []

    def inject_failure(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("UPDATE v5_payments"):
            reached_partial_updates.append(True)
            raise RuntimeError("Синтетический отказ после частичного переноса связей")

    event.listen(db.engine, "before_cursor_execute", inject_failure)
    try:
        with pytest.raises(RuntimeError, match="Синтетический отказ"):
            accounts.confirm_telegram_link(source_id, proof)
    finally:
        event.remove(db.engine, "before_cursor_execute", inject_failure)
    assert reached_partial_updates == [True]
    with db.transaction() as session:
        target = session.get(Account, target_id)
        source = session.get(Account, source_id)
        assert target.telegram_id is None and source.telegram_id == telegram
        assert target.bonus_balance == Decimal("12.34")
        assert source.bonus_balance == Decimal("6.78")
        assert target.credit_balance == 0 and source.credit_balance == Decimal("23.45")
        assert session.get(Alias, source_id) is None
        assert session.get(MergeProof, proof.split(".", 1)[0]).used is False
        assert sorted(
            session.scalars(
                select(Subscription.account_id).where(Subscription.account_id.in_([source_id, target_id]))
            )
        ) == sorted([source_id, target_id])
        assert sorted(
            session.scalars(select(Payment.account_id).where(Payment.account_id.in_([source_id, target_id])))
        ) == sorted([source_id, target_id])
    assert accounts.confirm_telegram_link(source_id, proof)["id"] == str(target_id)
    dashboard = commerce.dashboard(target_id)
    assert len(dashboard["subscriptions"]) == 2 and len(dashboard["payments"]) == 2
    assert Decimal(dashboard["bonus_balance"]) == Decimal("19.12")
    with db.transaction() as session:
        assert session.get(Account, target_id).credit_balance == Decimal("23.45")
    db.engine.dispose()
