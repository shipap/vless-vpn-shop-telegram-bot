import os
import re
from datetime import timedelta
from uuid import uuid4

import fakeredis
import pytest
from sqlalchemy import select
from sunrise_core.errors import DomainError
from sunrise_infrastructure.accounts import Accounts
from sunrise_infrastructure.commerce import Commerce
from sunrise_infrastructure.db import Database, Job, now
from sunrise_infrastructure.security import RateGuard
from sunrise_infrastructure.settings import Settings

from tests.isolated_database import isolated_database
from tests.legal_fixture import accept_test_legal, current_test_legal

SYNTHETIC = "synthetic-password"


@pytest.fixture
def mail_db():
    with isolated_database("mail_transport_test") as db:
        yield db


def test_smtp_authorized_local_otp_keeps_synthetic_mail_in_private_sink(tmp_path):
    from sunrise_infrastructure.mail import configured_mail

    email = f"{uuid4()}@example.test"
    settings = Settings(
        _env_file=None,
        database_url=os.environ["SUNRISE_TEST_DATABASE_URL"],
        secret_key="s" * 48,
        local_test=True,
        email_allowlist=[email, "owner@example.invalid"],
        mail_dir=str(tmp_path),
        smtp_test_authorized=True,
        smtp_host="smtp.example.invalid",
        smtp_port=587,
        smtp_mode="starttls",
        smtp_sender="shop@example.invalid",
        smtp_username="synthetic-user",
        smtp_password=SYNTHETIC,
        smtp_allowed_hosts=["smtp.example.invalid"],
        smtp_tunnel_port=None,
    )
    mail = configured_mail(settings, factory=lambda **_: pytest.fail("Synthetic OTP must not use SMTP"))
    db = Database(settings.database_url)
    try:
        accounts = Accounts(db, RateGuard(fakeredis.FakeRedis()), settings, mail_transport=mail)
        challenge = accounts.request_code(email, "127.0.0.1")
        import json

        saved = json.loads((tmp_path / (challenge["challenge_id"] + ".json")).read_text("utf-8"))
        assert saved["to"] == email and len(saved["code"]) == 6
        _, auth = accounts.verify_code(
            challenge["challenge_id"],
            saved["code"],
            "127.0.0.1",
            legal_accepted=True,
            documents=current_test_legal(Commerce(db)),
        )
        assert auth["account"]["email"] == email
    finally:
        db.engine.dispose()


@pytest.mark.parametrize("email", ["owner@example.invalid", "unknown@example.invalid"])
def test_local_real_otp_without_transport_is_generic_unavailable_not_saved(tmp_path, email):
    from types import SimpleNamespace

    settings = SimpleNamespace(
        secret_key="s" * 48,
        local_test=True,
        email_allowlist=["owner@example.invalid"],
        mail_dir=str(tmp_path),
    )
    db = Database(os.environ["SUNRISE_TEST_DATABASE_URL"])
    try:
        accounts = Accounts(db, RateGuard(fakeredis.FakeRedis()), settings)
        with pytest.raises(DomainError) as error:
            accounts.request_code(email, "127.0.0.1")
        assert error.value.code == "mail_unavailable" and error.value.status == 503
        assert not list(tmp_path.glob("*.json"))
    finally:
        db.engine.dispose()


def test_otp_and_financial_notification_use_shared_injected_mail_without_rebilling(tmp_path, mail_db):
    class Mail:
        def __init__(self):
            self.messages = []
            self.fail_notification = True

        def deliver(self, recipient, subject, body, delivery_id):
            if "Оплата получена" in body and self.fail_notification:
                self.fail_notification = False
                raise DomainError("mail_unavailable", "Синтетический отказ", 503)
            self.messages.append((recipient, subject, body, delivery_id))

    email = f"{uuid4()}@example.test"
    settings = Settings(
        database_url=os.environ["SUNRISE_TEST_DATABASE_URL"],
        secret_key="s" * 48,
        local_test=True,
        email_allowlist=[email],
        mail_dir=str(tmp_path),
    )
    db = mail_db
    mail = Mail()
    accounts = Accounts(db, RateGuard(fakeredis.FakeRedis()), settings, mail_transport=mail)
    challenge = accounts.request_code(email, "127.0.0.1")
    code = re.search(r"[0-9]{6}", mail.messages[0][2]).group()
    assert not list(tmp_path.glob("*.json"))
    _, auth = accounts.verify_code(
        challenge["challenge_id"],
        code,
        "127.0.0.1",
        legal_accepted=True,
        documents=current_test_legal(Commerce(db)),
    )
    aid = auth["account"]["id"]
    c = Commerce(db, local_test=True, mail_transport=mail, notification_allowlist=[email])
    accept_test_legal(c, aid)
    q = c.quote(aid, "standard", 30, "purchase")
    o = c.create_order(aid, q["id"], str(uuid4()))
    c.test_pay(aid, o["id"])
    c.run_jobs()
    with db.transaction() as s:
        job = s.scalar(select(Job).where(Job.key == "notify:" + o["id"]))
        assert job.state == "pending" and job.last_error == "mail_unavailable"
        job.available_at = now() - timedelta(seconds=1)
    c.run_jobs()
    c.run_jobs()
    assert len(mail.messages) == 2
    assert mail.messages[1][0] == email
    dashboard = c.dashboard(aid)
    assert len(dashboard["payments"]) == 1 and len(dashboard["subscriptions"][0]["segments"]) == 1
    with db.transaction() as s:
        s.add(
            Job(
                key=str(uuid4()),
                kind="broadcast",
                account_id=int(aid),
                payload={
                    "recipients": [email],
                    "nodes": [
                        {"type": "heading", "children": [{"type": "text", "text": "Важное сообщение"}]},
                        {"type": "text", "text": "Текст владельца"},
                    ],
                },
            )
        )
    c.run_jobs()
    assert "Важное сообщение" in mail.messages[-1][2] and "Текст владельца" in mail.messages[-1][2]
    assert "{'type':" not in mail.messages[-1][2]
