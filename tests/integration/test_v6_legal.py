import os
from uuid import uuid4

import pytest
from sunrise_core.errors import DomainError
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import Account, Database, Setting


@pytest.fixture
def legal_context():
    db = Database(os.environ["SUNRISE_TEST_DATABASE_URL"])
    assert db.engine.url.database == "sunrise_test_backend"
    c = Commerce(db, local_test=True)
    with db.transaction() as s:
        old = s.get(Setting, "legal")
        previous = old.value if old else None
        documents = [
            {"document_type": "offer", "version": "synthetic-v6", "url": "https://example.test/offer"},
            {"document_type": "privacy", "version": "synthetic-v6", "url": "https://example.test/privacy"},
        ]
        if old:
            old.value = {"documents": documents}
        else:
            s.add(Setting(key="legal", value={"documents": documents}))
        a = Account(email=f"{uuid4()}@example.test")
        s.add(a)
        s.flush()
        aid = a.id
    yield c, aid, documents
    with db.transaction() as s:
        row = s.get(Setting, "legal")
        if previous is None and row:
            s.delete(row)
        elif previous is not None:
            if row:
                row.value = previous
            else:
                s.add(Setting(key="legal", value=previous))


def test_current_explicit_legal_gate_and_immutable_history(legal_context):
    c, aid, documents = legal_context
    assert c.legal_status(aid)["accepted"] is False
    with pytest.raises(DomainError) as blocked:
        c.quote(aid, "standard", 30, "purchase")
    assert blocked.value.code == "legal_consent_required"
    status = c.accept_legal(aid, documents, "website", "checkout")
    assert status["accepted"] and len(status["history"]) == 2
    assert c.accept_legal(aid, documents, "website", "checkout")["history"] == status["history"]
    q = c.quote(aid, "standard", 30, "purchase")
    with c.db.transaction() as s:
        s.get(Setting, "legal").value = {"documents": [d | {"version": "synthetic-v7"} for d in documents]}
    with pytest.raises(DomainError) as stale:
        c.accept_legal(aid, documents, "website", "checkout")
    assert stale.value.code == "legal_versions_stale"
    with pytest.raises(DomainError) as purchase:
        c.create_order(aid, q["id"], "blocked-stale-legal")
    assert purchase.value.code == "legal_consent_required"
    assert len(c.legal_status(aid)["history"]) == 2
    assert c.dashboard(aid)["account"]["id"] == str(aid)


def test_unconfigured_legal_does_not_invent_versions(legal_context):
    c, aid, documents = legal_context
    with c.db.transaction() as s:
        s.delete(s.get(Setting, "legal"))
    assert not c.legal_status(aid)["configured"]
    with pytest.raises(DomainError) as exc:
        c.accept_legal(aid, documents, "telegram", "first_use")
    assert exc.value.code == "legal_not_configured"


def test_merge_preserves_original_consent_events_without_new_acceptance(legal_context):
    import fakeredis
    from sunrise_infrastructure.accounts import Accounts
    from sunrise_infrastructure.db import now
    from sunrise_infrastructure.security import RateGuard
    from sunrise_infrastructure.settings import Settings

    c, aid, documents = legal_context
    with c.db.transaction() as s:
        account = s.get(Account, aid)
        account.email_verified = True
        account.legacy_consent = {"tos": 1}
    tg = int(uuid4().int % 1000000000000) + 100000
    target = c.resolve_telegram(tg)
    first = c.accept_legal(aid, documents, "website", "registration")["history"]
    second = c.accept_legal(target["id"], documents, "telegram", "first_use")["history"]
    accounts = Accounts(
        c.db,
        RateGuard(fakeredis.FakeRedis()),
        Settings(database_url=c.db.engine.url.render_as_string(hide_password=False)),
    )
    proof = accounts.request_link(aid, str(tg), now())["proof"]
    accounts.confirm_telegram_link(target["id"], proof)
    state = c.legal_status(aid)
    assert state["accepted"]
    assert state["legacy_evidence"] == {"tos": 1}
    assert len(state["history"]) == 4
    original = {r["id"]: r for r in first + second}
    for row in state["history"]:
        assert row["user_id"] == str(aid)
        assert {k: v for k, v in row.items() if k != "user_id"} == {
            k: v for k, v in original[row["id"]].items() if k != "user_id"
        }


def test_new_version_preserves_already_accepted_order_fulfillment(legal_context):
    c, aid, documents = legal_context
    c.accept_legal(aid, documents, "website", "checkout")
    q = c.quote(aid, "standard", 30, "purchase")
    order = c.create_order(aid, q["id"], str(uuid4()))
    c.legal.configure(
        None, [d | {"version": "new-synthetic-version"} for d in documents], "Проверка смены версии"
    )
    c.test_pay(aid, order["id"])
    assert len(c.dashboard(aid)["subscriptions"]) == 1
    assert not c.legal_status(aid)["accepted"]
