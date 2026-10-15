import os
from uuid import uuid4

import fakeredis
from sqlalchemy import select
from sunrise_infrastructure.accounts import Accounts
from sunrise_infrastructure.admin import Admin
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import Account, Audit, Database, Job, Segment, Subscription
from sunrise_infrastructure.security import RateGuard
from sunrise_infrastructure.settings import Settings

from tests.legal_fixture import accept_test_legal, current_test_legal


def test_admin_restriction_and_sync_preserve_paid_rights():
    db = Database(os.environ["SUNRISE_TEST_DATABASE_URL"])
    c = Commerce(db, local_test=True)
    admin = Admin(c, RateGuard(fakeredis.FakeRedis()))
    with db.transaction() as s:
        a = Account(email=f"{uuid4()}@example.test")
        s.add(a)
        s.flush()
        aid = a.id
    accept_test_legal(Commerce(db), a.id)
    q = c.quote(aid, "standard", 30, "purchase")
    o = c.create_order(aid, q["id"], str(uuid4()))
    c.test_pay(aid, o["id"])
    sid = c.dashboard(aid)["subscriptions"][0]["id"]
    with db.transaction() as s:
        sub = s.get(Subscription, sid)
        expiry = sub.expires_at
        segments = list(s.scalars(select(Segment.id).where(Segment.subscription_id == sid)))
    assert hasattr(admin, "subscription_operation"), "Missing reasoned restriction and safe synchronization"
    for operation, expected in [("suspend", "DISABLED"), ("resume", "ACTIVE"), ("synchronize", "ACTIVE")]:
        result = admin.subscription_operation(aid, sid, operation, "Проверка локального ограничения")
        with db.transaction() as s:
            job = s.get(Job, result["job_id"])
            assert job.payload["panel_status"] == expected
            assert s.get(Subscription, sid).expires_at == expiry
            assert list(s.scalars(select(Segment.id).where(Segment.subscription_id == sid))) == segments
    with db.transaction() as s:
        events = list(s.scalars(select(Audit).where(Audit.target == sid)))
        assert {x.action for x in events} >= {
            "subscription_suspend",
            "subscription_resume",
            "subscription_synchronize",
        }
    db.engine.dispose()


def test_login_diagnostics_persist_without_codes_or_session_secrets(tmp_path):
    url = os.environ["SUNRISE_TEST_DATABASE_URL"]
    email = f"{uuid4()}@example.test"
    config = Settings(
        database_url=url,
        secret_key="s" * 48,
        local_test=True,
        email_allowlist=[email],
        mail_dir=str(tmp_path),
    )
    db = Database(url)
    guard = RateGuard(fakeredis.FakeRedis())
    accounts = Accounts(db, guard, config)
    challenge = accounts.request_code(email, "127.0.0.1")
    import json

    code = json.loads((tmp_path / (challenge["challenge_id"] + ".json")).read_text())["code"]
    bad = "000000" if code != "000000" else "111111"
    import pytest
    from sunrise_core.errors import DomainError

    with pytest.raises(DomainError):
        accounts.verify_code(challenge["challenge_id"], bad, "127.0.0.1")
    token, auth = accounts.verify_code(
        challenge["challenge_id"],
        code,
        "127.0.0.1",
        legal_accepted=True,
        documents=current_test_legal(Commerce(db)),
    )
    admin = Admin(Commerce(db, local_test=True), guard)
    assert hasattr(admin, "login_events"), "Missing paginated protected login diagnostics"
    events = admin.login_events(search=email)["items"]
    assert {event["result"] for event in events} >= {"success", "failure"}
    assert all(event["ip_provenance"] == "direct" for event in events)
    serialized = json.dumps(events)
    assert code not in serialized and bad not in serialized and token not in serialized
    assert auth["csrf_token"] not in serialized
    db.engine.dispose()
