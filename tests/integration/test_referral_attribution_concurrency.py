from concurrent.futures import ThreadPoolExecutor
from threading import Event

from sqlalchemy import event, select
from sunrise_infrastructure.db import Account, Alias, Ledger, now
from sunrise_infrastructure.referral_attribution import assign_new_referrer

pytest_plugins = ["tests.integration.test_v10_admin_mfa"]


def test_new_registration_referral_waits_for_inviter_merge_and_uses_canonical_account(mfa_context):
    db, app, _, _, _, _, _ = mfa_context
    accounts = app.state.accounts
    with db.transaction() as session:
        canonical = Account(email="canonical-referral@example.test", email_verified=True)
        inviter = Account(telegram_id=70000000000000999)
        session.add_all([canonical, inviter])
        session.flush()
        canonical_id, inviter_id = canonical.id, inviter.id
    proof = accounts.request_link(canonical_id, str(70000000000000999), now())["proof"]
    locked, looked_up, merged = Event(), Event(), Event()

    def pause_after_inviter_lock(_connection, _cursor, statement, _parameters, _context, _executemany):
        if "FROM v5_accounts" in statement and "FOR UPDATE" in statement and not locked.is_set():
            locked.set()
            # An unlocked lookup returns now; a correct lookup waits for the merge.
            looked_up.wait(1)

    def merge():
        try:
            accounts.confirm_telegram_link(inviter_id, proof)
        finally:
            merged.set()

    def register():
        assert locked.wait(5), "Merge did not lock the inviter"
        with db.transaction() as session:
            newcomer = Account(email="new-referral@example.test", email_verified=True)
            session.add(newcomer)
            assign_new_referrer(session, newcomer, "ref_" + str(inviter_id))
            looked_up.set()
            assert merged.wait(5), "Merge did not finish"
            session.flush()
            return newcomer.id

    event.listen(accounts.db.engine, "after_cursor_execute", pause_after_inviter_lock)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            merge_result = pool.submit(merge)
            registration = pool.submit(register)
            merge_result.result(timeout=10)
            newcomer_id = registration.result(timeout=10)
    finally:
        event.remove(accounts.db.engine, "after_cursor_execute", pause_after_inviter_lock)
    with db.transaction() as session:
        assert session.get(Account, inviter_id) is None
        assert session.get(Alias, inviter_id).canonical_id == canonical_id
        assert session.get(Account, newcomer_id).referrer_id == canonical_id
        assert list(session.scalars(select(Ledger))) == []
