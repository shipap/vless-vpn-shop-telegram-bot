"""Payment account locks survive verified canonical merge without financial replay."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Event, current_thread
from uuid import uuid4

import fakeredis
from sqlalchemy import event, func, select
from sunrise_infrastructure.accounts import Accounts
from sunrise_infrastructure.billing import Billing, VerifiedPayment
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import Account, Payment, now
from sunrise_infrastructure.security import RateGuard
from sunrise_infrastructure.settings import Settings

from tests.legal_fixture import accept_test_legal
from tests.review.test_independent_review import (
    review_db as review_db,
)
from tests.review.test_independent_review import (
    test_payment_resolves_alias_created_between_alias_read_and_account_lock as original_owner_race,
)


def test_payment_merge_to_canonical_with_existing_referrer(review_db):
    db, aid, _ = review_db
    with db.transaction() as session:
        referrer = Account()
        session.add(referrer)
        session.flush()
        session.get(Account, aid).referrer_id = referrer.id
    original_owner_race(review_db)


def test_payment_referrer_alias_created_before_ordered_account_locks(review_db):
    db, aid, pid = review_db
    commerce = Commerce(db, local_test=True)
    accounts = Accounts(
        db,
        RateGuard(fakeredis.FakeRedis()),
        Settings(database_url=db.engine.url.render_as_string(hide_password=False)),
    )
    telegram = int(uuid4().int % 1000000000000) + 100000
    old_referrer = int(commerce.resolve_telegram(telegram)["id"])
    with db.transaction() as session:
        payer = Account(referrer_id=old_referrer)
        session.add(payer)
        session.flush()
        payer_id = payer.id
    accept_test_legal(commerce, payer_id)
    quote = commerce.quote(payer_id, pid, 30, "purchase")
    order = commerce.create_order(payer_id, quote["id"], str(uuid4()))
    fact = VerifiedPayment(
        "review",
        str(uuid4()),
        "review-terminal",
        "local_test",
        order["id"],
        Decimal(order["total"]),
        order["currency"],
        now(),
    )
    proof = accounts.request_link(aid, str(telegram), now())["proof"]
    reached, release = Event(), Event()

    def pause_before_account_lock(conn, cursor, statement, parameters, context, many):
        if (
            current_thread().name.startswith("review-referrer")
            and "FROM v5_accounts" in statement
            and "FOR UPDATE" in statement
            and not reached.is_set()
        ):
            reached.set()
            assert release.wait(timeout=10)

    event.listen(db.engine, "before_cursor_execute", pause_before_account_lock)
    try:
        billing = Billing(commerce, "review-terminal", "local_test")
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="review-referrer") as pool:
            future = pool.submit(billing.accept, fact)
            try:
                assert reached.wait(timeout=10)
                accounts.confirm_telegram_link(old_referrer, proof)
            finally:
                release.set()
            result = future.result(timeout=10)
    finally:
        event.remove(db.engine, "before_cursor_execute", pause_before_account_lock)
    assert billing.accept(fact)["id"] == result["id"]
    with db.transaction() as session:
        assert session.get(Account, payer_id).referrer_id == aid
        assert (
            session.scalar(select(func.count()).select_from(Payment).where(Payment.order_id == order["id"]))
            == 1
        )
    assert len(commerce.dashboard(payer_id)["subscriptions"]) == 1
