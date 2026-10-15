import json
from datetime import timedelta

import fakeredis
import pytest
from sqlalchemy import select
from sunrise_core.errors import DomainError
from sunrise_infrastructure.accounts import Accounts
from sunrise_infrastructure.db import Account, Session, Subscription, now
from sunrise_infrastructure.security import RateGuard, digest

from tests.e2e.local_auth_server import SyntheticSettings
from tests.isolated_database import isolated_database

CURRENT, OTHER, FOREIGN = (digest(value) for value in ("current-v11", "other-v11", "foreign-v11"))
TG, OWNER_TG = 70000000000000002, 70000000000000003


@pytest.fixture
def account_features_context(tmp_path):
    from sunrise_infrastructure.account_features import AccountsFeatures

    with isolated_database("v11_account_test") as db:
        config = SyntheticSettings(
            _env_file=None,
            database_url=db.engine.url.render_as_string(hide_password=False),
            secret_key="v11-isolated-account-secret-" * 3,
            local_test=True,
            email_allowlist=["old@account.test", "new@account.test", "owner@account.test"],
            admin_email_allowlist=["owner@account.test"],
            owner_id=str(OWNER_TG),
            owner_email="owner@account.test",
            mail_dir=str(tmp_path),
        )
        accounts = Accounts(db, RateGuard(fakeredis.FakeRedis()), config)
        with db.transaction() as tx:
            tx.add_all(
                [
                    Account(
                        id=101,
                        email="old@account.test",
                        email_verified=True,
                        telegram_id=TG,
                        credit_balance="9.50",
                        credit_currency="RUB",
                    ),
                    Account(id=102, email="owner@account.test", email_verified=True, telegram_id=OWNER_TG),
                    Account(id=103, telegram_id=70000000000000004),
                ]
            )
            tx.flush()
            tx.add_all(
                [
                    Session(
                        id=CURRENT,
                        account_id=101,
                        csrf="current-csrf",
                        auth_method="email_otp",
                        ip_address="127.0.0.1",
                        expires_at=now() + timedelta(days=1),
                        email_confirmed_at=now(),
                    ),
                    Session(
                        id=OTHER,
                        account_id=101,
                        csrf="other-csrf",
                        auth_method="telegram_oidc",
                        expires_at=now() + timedelta(days=1),
                    ),
                    Session(
                        id=FOREIGN,
                        account_id=102,
                        csrf="foreign-csrf",
                        auth_method="email_otp",
                        ip_address="127.0.0.1",
                        expires_at=now() + timedelta(days=1),
                        email_confirmed_at=now(),
                    ),
                    Subscription(
                        devices_limit=4,
                        id="v11-paid-sub",
                        account_id=101,
                        expires_at=now() + timedelta(days=30),
                    ),
                ]
            )
        yield db, accounts, AccountsFeatures(accounts), tmp_path


def mail_code(context, dto):
    return json.loads((context[3] / (dto["challenge_id"] + ".json")).read_text())["code"]


def test_v11_account_sessions_are_owned_and_never_expose_credentials(account_features_context):
    _, _, service, _ = account_features_context
    items = service.list_sessions(101, CURRENT)["items"]
    assert len(items) == 2 and sum(item["current"] for item in items) == 1
    assert all(item["id"] not in {CURRENT, OTHER, FOREIGN} for item in items)
    assert all(not ({"csrf", "csrf_token", "token", "mfa_at"} & item.keys()) for item in items)
    foreign = service.list_sessions(102, FOREIGN)["items"][0]["id"]
    with pytest.raises(DomainError) as failure:
        service.revoke_session(101, CURRENT, foreign)
    assert failure.value.status == 404


def test_v11_account_revoke_other_keeps_current_expiry_freshness_and_paid_rights(account_features_context):
    db, _, service, _ = account_features_context
    with db.transaction() as tx:
        row = tx.get(Session, CURRENT)
        before = (row.csrf, row.expires_at, row.confirmed_at, row.email_confirmed_at)
    other = next(item["id"] for item in service.list_sessions(101, CURRENT)["items"] if not item["current"])
    assert service.revoke_session(101, CURRENT, other)["revoked"] is True
    with db.transaction() as tx:
        row = tx.get(Session, CURRENT)
        assert (row.csrf, row.expires_at, row.confirmed_at, row.email_confirmed_at) == before
        assert tx.get(Session, OTHER) is None and tx.get(Session, FOREIGN) is not None
        assert tx.get(Subscription, "v11-paid-sub").account_id == 101


def test_v11_account_unlink_requires_current_email_proof_not_recent_telegram_login(account_features_context):
    db, _, service, _ = account_features_context
    with db.transaction() as tx:
        row = tx.get(Session, CURRENT)
        row.email_confirmed_at = None
        row.auth_method = "telegram_miniapp"
    with pytest.raises(DomainError) as failure:
        service.request_unlink(101, CURRENT, "127.0.0.1")
    assert failure.value.code == "fresh_login_required"
    with db.transaction() as tx:
        assert tx.get(Account, 101).telegram_id == TG


def test_v11_account_unlink_bot_sender_snapshot_replay_and_paid_rights(account_features_context):
    db, _, service, _ = account_features_context
    dto = service.request_unlink(101, CURRENT, "127.0.0.1")
    assert dto["stage"] == "awaiting_telegram"
    with pytest.raises(DomainError):
        service.confirm_unlink(TG + 1, dto["proof"])
    assert service.confirm_unlink(TG, dto["proof"])["stage"] == "completed"
    assert service.confirm_unlink(TG, dto["proof"])["stage"] == "completed"
    with db.transaction() as tx:
        account = tx.get(Account, 101)
        assert account.telegram_id is None and account.telegram_username is None
        assert str(account.credit_balance) == "9.500000"
        assert tx.get(Subscription, "v11-paid-sub").account_id == 101
        assert tx.get(Session, CURRENT) is not None and tx.get(Session, OTHER) is None
        assert len(list(tx.scalars(select(Account)))) == 3


def test_v11_account_unlink_rejects_changed_current_telegram(account_features_context):
    db, _, service, _ = account_features_context
    dto = service.request_unlink(101, CURRENT, "127.0.0.1")
    with db.transaction() as tx:
        tx.get(Account, 101).telegram_id = TG + 10
    with pytest.raises(DomainError):
        service.confirm_unlink(TG, dto["proof"])
    with db.transaction() as tx:
        assert tx.get(Account, 101).telegram_id == TG + 10 and tx.get(Session, OTHER) is not None


def test_v11_account_owner_fallback_is_pending_and_nonowner_cannot_approve(account_features_context):
    db, _, service, _ = account_features_context
    dto = service.request_owner_unlink(101, CURRENT, "127.0.0.1", "Нет доступа к Telegram")
    assert dto["stage"] == "owner_review"
    with db.transaction() as tx:
        assert tx.get(Account, 101).telegram_id == TG
    with pytest.raises(DomainError):
        service.approve_owner_unlink(101, dto["operation_id"], "Проверка владельца")


def test_v11_account_email_change_proves_old_then_new_without_login_otp_reuse(account_features_context):
    db, accounts, service, _ = account_features_context
    dto = service.request_email_change(101, CURRENT, "new@account.test", "127.0.0.1")
    assert dto["stage"] == "new_email"  # fresh email OTP already proves the old mailbox
    with pytest.raises(DomainError):
        accounts.verify_code(dto["challenge_id"], mail_code(account_features_context, dto), "127.0.0.1")
    result = service.verify_email_change(
        101,
        CURRENT,
        dto["operation_id"],
        dto["challenge_id"],
        mail_code(account_features_context, dto),
        "127.0.0.1",
    )
    assert result["stage"] == "completed" and result["account"]["email"] == "new@account.test"
    with db.transaction() as tx:
        assert tx.get(Account, 101).email_verified is True
        assert tx.get(Session, CURRENT) is not None and tx.get(Session, OTHER) is None
        assert tx.get(Subscription, "v11-paid-sub").account_id == 101


def test_v11_account_email_change_rejects_foreign_session_and_mailbox_collision(account_features_context):
    _, _, service, _ = account_features_context
    with pytest.raises(DomainError):
        service.request_email_change(101, FOREIGN, "new@account.test", "127.0.0.1")
    with pytest.raises(DomainError) as failure:
        service.request_email_change(101, CURRENT, "owner@account.test", "127.0.0.1")
    assert failure.value.code == "email_identity_conflict"


def test_v11_account_email_verification_bound_to_account_session_and_ip(account_features_context):
    db, _, service, _ = account_features_context
    dto = service.request_email_change(101, CURRENT, "new@account.test", "127.0.0.1")
    code = mail_code(account_features_context, dto)
    for aid, sid, ip in [(102, FOREIGN, "127.0.0.1"), (101, OTHER, "127.0.0.1"), (101, CURRENT, "127.0.0.2")]:
        with pytest.raises(DomainError):
            service.verify_email_change(aid, sid, dto["operation_id"], dto["challenge_id"], code, ip)
    with db.transaction() as tx:
        assert tx.get(Account, 101).email == "old@account.test"
    assert (
        service.verify_email_change(
            101, CURRENT, dto["operation_id"], dto["challenge_id"], code, "127.0.0.1"
        )["stage"]
        == "completed"
    )


def test_v11_account_email_add_requires_recent_server_verified_telegram_origin(account_features_context):
    db, _, service, _ = account_features_context
    tg_session = digest("tg-v11-add")
    with db.transaction() as tx:
        tx.add(
            Session(
                id=tg_session,
                account_id=103,
                csrf="tg",
                auth_method="unknown",
                expires_at=now() + timedelta(days=1),
            )
        )
    with pytest.raises(DomainError):
        service.request_email_change(103, tg_session, "new@account.test", "127.0.0.1")
    with db.transaction() as tx:
        tx.get(Session, tg_session).auth_method = "telegram_oidc"
        tx.get(Session, tg_session).ip_address = "127.0.0.2"
    with pytest.raises(DomainError) as failure:
        service.request_email_change(103, tg_session, "new@account.test", "127.0.0.1")
    assert failure.value.code == "fresh_login_required"
    with db.transaction() as tx:
        tx.get(Session, tg_session).ip_address = "127.0.0.1"
    dto = service.request_email_change(103, tg_session, "new@account.test", "127.0.0.1")
    assert dto["stage"] == "new_email"
    assert (
        service.verify_email_change(
            103,
            tg_session,
            dto["operation_id"],
            dto["challenge_id"],
            mail_code(account_features_context, dto),
            "127.0.0.1",
        )["stage"]
        == "completed"
    )


def test_v11_account_email_login_persists_server_metadata_and_alerts_only_unfamiliar_context(
    account_features_context,
):
    from sunrise_infrastructure.commerce import Commerce
    from sunrise_infrastructure.db import Notification

    db, accounts, _, _ = account_features_context
    documents = [
        {"document_type": "offer", "version": "v11", "url": "https://account.example.test/offer"},
        {"document_type": "privacy", "version": "v11", "url": "https://account.example.test/privacy"},
    ]
    Commerce(db).legal.configure(None, documents, "Синтетические документы аккаунта")
    dto = accounts.request_code("old@account.test", "127.0.0.1")
    token, result = accounts.verify_code(
        dto["challenge_id"],
        mail_code(account_features_context, dto),
        "127.0.0.1",
        legal_accepted=True,
        documents=documents,
        browser_label="Chromium · Windows",
    )
    with db.transaction() as tx:
        session = tx.get(Session, digest(token))
        assert session.auth_method == "email_otp" and session.ip_address == "127.0.0.1"
        assert session.browser_label == "Chromium · Windows" and session.public_id
        assert (
            len(
                list(
                    tx.scalars(
                        select(Notification).where(
                            Notification.account_id == 101, Notification.kind == "login"
                        )
                    )
                )
            )
            == 1
        )
    accounts.guard.redis.delete("sunrise:resend:" + digest("old@account.test"))
    dto = accounts.request_code("old@account.test", "127.0.0.1")
    accounts.verify_code(
        dto["challenge_id"],
        mail_code(account_features_context, dto),
        "127.0.0.1",
        legal_accepted=True,
        documents=documents,
        browser_label="Chromium · Windows",
    )
    with db.transaction() as tx:
        assert (
            len(
                list(
                    tx.scalars(
                        select(Notification).where(
                            Notification.account_id == 101, Notification.kind == "login"
                        )
                    )
                )
            )
            == 1
        )


def test_v11_account_only_verified_miniapp_origin_suppresses_login_notification(account_features_context):
    from urllib.parse import parse_qs, urlsplit

    from sunrise_infrastructure.commerce import Commerce
    from sunrise_infrastructure.db import Notification
    from sunrise_infrastructure.telegram_auth import TelegramAuth

    from tests.integration.test_telegram_oidc import SyntheticTelegram
    from tests.unit.test_telegram_miniapp_auth import TOKEN, signed_init

    db, accounts, _, _ = account_features_context
    config = accounts.settings
    config.bot_test_token, config.test_bot_ids = TOKEN, "123456789"
    config.test_telegram_recipients, config.telegram_miniapp_enabled = "70000000000000004", True
    documents = [
        {"document_type": "offer", "version": "v11", "url": "https://account.example.test/offer"},
        {"document_type": "privacy", "version": "v11", "url": "https://account.example.test/privacy"},
    ]
    Commerce(db).legal.configure(None, documents, "Синтетические документы аккаунта")
    signed = signed_init(
        {
            "auth_date": str(int(now().timestamp())),
            "user": json.dumps({"id": 70000000000000004, "username": "synthetic_v11"}),
        }
    )
    token, _ = TelegramAuth(accounts).miniapp(signed, "127.0.0.1", legal_accepted=True, documents=documents)
    with db.transaction() as tx:
        session = tx.get(Session, digest(token))
        assert session.auth_method == "telegram_miniapp" and session.email_confirmed_at is None
        assert (
            list(
                tx.scalars(
                    select(Notification).where(Notification.account_id == 103, Notification.kind == "login")
                )
            )
            == []
        )
    config.telegram_login_client_id = "123456789"
    config.telegram_login_client_secret = "synthetic-oidc-secret"  # noqa: S105 -- isolated fake provider
    config.telegram_login_redirect_uri = "http://127.0.0.1:8150/api/v1/auth/telegram/callback"
    config.allowed_origins = ["http://127.0.0.1:8150"]
    provider = SyntheticTelegram()
    provider.overrides = {"id": 70000000000000004}
    auth = TelegramAuth(accounts, transport=provider)
    started, browser = auth.start(documents, True, "127.0.0.1")
    params = parse_qs(urlsplit(started["authorization_url"]).query)
    provider.nonce = params["nonce"][0]
    token, _ = auth.callback("synthetic-provider-code", params["state"][0], browser, "127.0.0.1")
    with db.transaction() as tx:
        row = tx.get(Session, digest(token))
        assert row.auth_method == "telegram_oidc" and row.email_confirmed_at is None
        assert (
            len(
                list(
                    tx.scalars(
                        select(Notification).where(
                            Notification.account_id == 103, Notification.kind == "login"
                        )
                    )
                )
            )
            == 1
        )


def test_v11_account_merge_preserves_new_histories_but_revokes_stale_targets_and_staff_grants(
    account_features_context,
):
    from sunrise_infrastructure.db import (
        AccountIdentityOperation,
        CampaignVisit,
        Notification,
        NotificationDelivery,
        NotificationPreference,
        PushSubscription,
        StaffRole,
    )

    db, accounts, _, _ = account_features_context
    with db.transaction() as tx:
        tx.get(Account, 101).telegram_id = None
        tx.add_all(
            [
                Notification(
                    id="source-note",
                    account_id=101,
                    event_key="same-key",
                    kind="test",
                    title="Источник",
                    body="Тест",
                ),
                Notification(
                    id="target-note",
                    account_id=103,
                    event_key="same-key",
                    kind="test",
                    title="Цель",
                    body="Тест",
                ),
                NotificationPreference(account_id=101, browser_enabled=False),
                NotificationPreference(account_id=103, browser_enabled=True),
                PushSubscription(
                    id="target-push",
                    account_id=103,
                    endpoint_digest=digest("endpoint"),
                    subscription_ciphertext="synthetic-ciphertext",
                ),
                StaffRole(account_id=103, role="admin"),
                AccountIdentityOperation(
                    id="stale-target-identity",
                    account_id=103,
                    session_id=OTHER,
                    kind="telegram_unlink",
                    state="awaiting_telegram",
                    old_telegram_id=70000000000000004,
                    expires_at=now() + timedelta(minutes=5),
                ),
                CampaignVisit(
                    id="target-visit",
                    account_id=103,
                    campaign_id="test",
                    visitor_hash=digest("visitor"),
                    secret_hash=digest("secret"),
                ),
            ]
        )
        tx.flush()
        tx.add(
            NotificationDelivery(
                id="stale-delivery",
                notification_id="target-note",
                channel="telegram",
                recipient="70000000000000004",
                state="pending",
            )
        )
    proof = accounts.request_link(101, "70000000000000004", now(), session_id=CURRENT, ip="127.0.0.1")[
        "proof"
    ]
    result = accounts.confirm_telegram_link(103, proof)
    assert result["id"] == "101"
    with db.transaction() as tx:
        assert tx.get(Account, 103) is None
        assert tx.get(Notification, "target-note").account_id == 101
        assert tx.get(Notification, "target-note").event_key != "same-key"
        assert tx.get(NotificationDelivery, "stale-delivery").state == "cancelled"
        assert tx.get(PushSubscription, "target-push").account_id == 101
        assert tx.get(PushSubscription, "target-push").disabled_at is not None
        assert tx.get(NotificationPreference, 101).browser_enabled is False
        assert tx.get(StaffRole, 101) is None and tx.get(StaffRole, 103) is None
        assert tx.get(AccountIdentityOperation, "stale-target-identity").state == "cancelled"
        assert tx.get(CampaignVisit, "target-visit").account_id == 101
        assert tx.get(Session, CURRENT) is not None


def test_v11_account_stale_email_change_requires_old_mailbox_before_new_and_cannot_replay(
    account_features_context,
):
    db, _, service, _ = account_features_context
    with db.transaction() as tx:
        tx.get(Session, CURRENT).email_confirmed_at = now() - timedelta(minutes=6)
    old = service.request_email_change(101, CURRENT, "new@account.test", "127.0.0.1")
    assert old["stage"] == "current_email"
    assert (
        json.loads((account_features_context[3] / (old["challenge_id"] + ".json")).read_text())["to"]
        == "old@account.test"
    )
    new = service.verify_email_change(
        101,
        CURRENT,
        old["operation_id"],
        old["challenge_id"],
        mail_code(account_features_context, old),
        "127.0.0.1",
    )
    assert new["stage"] == "new_email" and new["challenge_id"] != old["challenge_id"]
    with db.transaction() as tx:
        assert tx.get(Account, 101).email == "old@account.test"
    with pytest.raises(DomainError):
        service.verify_email_change(
            101,
            CURRENT,
            old["operation_id"],
            old["challenge_id"],
            mail_code(account_features_context, old),
            "127.0.0.1",
        )
    assert (
        service.verify_email_change(
            101,
            CURRENT,
            new["operation_id"],
            new["challenge_id"],
            mail_code(account_features_context, new),
            "127.0.0.1",
        )["stage"]
        == "completed"
    )
    with pytest.raises(DomainError):
        service.verify_email_change(
            101,
            CURRENT,
            new["operation_id"],
            new["challenge_id"],
            mail_code(account_features_context, new),
            "127.0.0.1",
        )


def test_v11_account_identity_otp_ten_wrong_attempts_and_cooldown_remain_authoritative(
    account_features_context,
):
    db, _, service, _ = account_features_context
    dto = service.request_email_change(101, CURRENT, "new@account.test", "127.0.0.1")
    correct = mail_code(account_features_context, dto)
    wrong = "000000" if correct != "000000" else "111111"
    with pytest.raises(DomainError) as failure:
        service.request_email_change(101, CURRENT, "new@account.test", "127.0.0.1")
    assert failure.value.code == "resend_cooldown"
    for attempt in range(10):
        with pytest.raises(DomainError) as failure:
            service.verify_email_change(
                101, CURRENT, dto["operation_id"], dto["challenge_id"], wrong, "127.0.0.1"
            )
        assert failure.value.status == (429 if attempt == 9 else 400)
    with pytest.raises(DomainError):
        service.verify_email_change(
            101, CURRENT, dto["operation_id"], dto["challenge_id"], correct, "127.0.0.1"
        )
    with db.transaction() as tx:
        assert tx.get(Account, 101).email == "old@account.test"


def test_v11_account_unlink_expiry_and_removed_initiating_session_cannot_delete_identity(
    account_features_context,
):
    from sunrise_infrastructure.db import AccountIdentityOperation

    db, _, service, _ = account_features_context
    dto = service.request_unlink(101, CURRENT, "127.0.0.1")
    with db.transaction() as tx:
        tx.get(AccountIdentityOperation, dto["operation_id"]).expires_at = now() - timedelta(seconds=1)
    with pytest.raises(DomainError):
        service.confirm_unlink(TG, dto["proof"])
    with db.transaction() as tx:
        tx.get(AccountIdentityOperation, dto["operation_id"]).expires_at = now() + timedelta(minutes=5)
        tx.delete(tx.get(Session, CURRENT))
    with pytest.raises(DomainError):
        service.confirm_unlink(TG, dto["proof"])
    with db.transaction() as tx:
        assert tx.get(Account, 101).telegram_id == TG


def test_v11_account_two_concurrent_bot_confirmations_commit_unlink_once(account_features_context):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, local

    from sqlalchemy import event
    from sunrise_infrastructure.db import Audit

    db, _, service, _ = account_features_context
    dto = service.request_unlink(101, CURRENT, "127.0.0.1")
    barrier, observed = Barrier(2, timeout=10), local()

    def synchronized_reads(connection, cursor, statement, parameters, context, executemany):
        if (
            statement.startswith("SELECT")
            and "v11_identity_operations" in statement
            and "FOR UPDATE" not in statement
            and not getattr(observed, "read", False)
        ):
            observed.read = True
            barrier.wait()

    event.listen(db.engine, "after_cursor_execute", synchronized_reads)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(service.confirm_unlink, TG, dto["proof"]) for _ in range(2)]
            assert [future.result(timeout=20)["stage"] for future in futures] == ["completed", "completed"]
    finally:
        event.remove(db.engine, "after_cursor_execute", synchronized_reads)
    with db.transaction() as tx:
        assert tx.get(Account, 101).telegram_id is None
        assert len(list(tx.scalars(select(Audit).where(Audit.action == "telegram_unlinked")))) == 1
        assert tx.get(Subscription, "v11-paid-sub").account_id == 101


def test_v11_account_fresh_mail_proof_is_bound_to_actual_ip_and_legacy_requires_inline_proof(
    account_features_context,
):
    db, _, service, _ = account_features_context
    with pytest.raises(DomainError) as failure:
        service.request_unlink(101, CURRENT, "127.0.0.2")
    assert failure.value.code == "fresh_login_required"
    with db.transaction() as tx:
        tx.get(Session, CURRENT).ip_address = None
    with pytest.raises(DomainError) as failure:
        service.request_unlink(101, CURRENT, "127.0.0.1")
    assert failure.value.code == "fresh_login_required"


def test_v11_account_owner_review_has_bounded_24h_lifetime(account_features_context):
    from sunrise_infrastructure.db import AccountIdentityOperation

    db, _, service, _ = account_features_context
    started = now()
    dto = service.request_owner_unlink(101, CURRENT, "127.0.0.1", "Потерян доступ к Telegram")
    with db.transaction() as tx:
        expires = tx.get(AccountIdentityOperation, dto["operation_id"]).expires_at
        assert started + timedelta(hours=23, minutes=59) < expires < now() + timedelta(hours=24, seconds=1)


def test_v11_account_email_collision_is_checked_again_at_completion_case_insensitively(
    account_features_context,
):
    db, _, service, _ = account_features_context
    dto = service.request_email_change(101, CURRENT, "new@account.test", "127.0.0.1")
    with db.transaction() as tx:
        tx.get(Account, 103).email = "NEW@ACCOUNT.TEST"
    with pytest.raises(DomainError) as failure:
        service.verify_email_change(
            101,
            CURRENT,
            dto["operation_id"],
            dto["challenge_id"],
            mail_code(account_features_context, dto),
            "127.0.0.1",
        )
    assert failure.value.code == "email_identity_conflict"
    with db.transaction() as tx:
        assert tx.get(Account, 101).email == "old@account.test"
        assert tx.get(Session, OTHER) is not None


def test_v11_account_owner_approval_keeps_owner_role_and_rejects_changed_snapshot(account_features_context):
    from sunrise_infrastructure.db import StaffRole

    db, _, service, _ = account_features_context
    dto = service.request_owner_unlink(102, FOREIGN, "127.0.0.1", "Потерян доступ к Telegram")
    stale = service.request_owner_unlink(101, CURRENT, "127.0.0.1", "Потерян доступ к Telegram")
    assert (
        service.approve_owner_unlink(102, dto["operation_id"], "Проверены оба доказательства")["stage"]
        == "completed"
    )
    with db.transaction() as tx:
        assert tx.get(Account, 102).telegram_id is None
        assert tx.get(StaffRole, 102).role == "owner"
    with pytest.raises(DomainError) as unqualified:
        service.approve_owner_unlink(102, stale["operation_id"], "Проверены оба доказательства")
    assert unqualified.value.status == 404
    with db.transaction() as tx:
        tx.get(Account, 102).telegram_id = OWNER_TG
        assert tx.get(StaffRole, 102).role == "owner"
        tx.get(Account, 101).telegram_id = TG + 10
    with pytest.raises(DomainError) as failure:
        service.approve_owner_unlink(102, stale["operation_id"], "Проверены оба доказательства")
    assert failure.value.code == "identity_changed"


def test_v11_account_notification_failure_rolls_back_unlink_and_keeps_proof_usable(
    account_features_context, monkeypatch
):
    from sunrise_infrastructure.notifications import NotificationService

    db, _, service, _ = account_features_context
    dto = service.request_unlink(101, CURRENT, "127.0.0.1")
    original = NotificationService.emit_in

    def unavailable(*args, **kwargs):
        raise DomainError("notification_unavailable", "Уведомления недоступны.", 503)

    monkeypatch.setattr(NotificationService, "emit_in", unavailable)
    with pytest.raises(DomainError):
        service.confirm_unlink(TG, dto["proof"])
    with db.transaction() as tx:
        assert tx.get(Account, 101).telegram_id == TG
        assert tx.get(Session, OTHER) is not None
        assert tx.get(Subscription, "v11-paid-sub").account_id == 101
    monkeypatch.setattr(NotificationService, "emit_in", original)
    assert service.confirm_unlink(TG, dto["proof"])["stage"] == "completed"


def test_v11_account_unlink_invalidates_scoped_email_grant_without_general_freshness_extension(
    account_features_context,
):
    db, accounts, service, _ = account_features_context
    with db.transaction() as tx:
        tx.get(Session, CURRENT).email_confirmed_at = now() - timedelta(minutes=6)
    challenge = accounts.request_link_email(101, CURRENT, "127.0.0.1")
    accounts.verify_link_email(
        101, CURRENT, challenge["challenge_id"], mail_code(account_features_context, challenge), "127.0.0.1"
    )
    assert accounts._link_granted(101, CURRENT, "127.0.0.1")
    with db.transaction() as tx:
        row = tx.get(Session, CURRENT)
        before = row.confirmed_at, row.email_confirmed_at, row.expires_at, row.csrf
    proof = service.request_unlink(101, CURRENT, "127.0.0.1")["proof"]
    service.confirm_unlink(TG, proof)
    assert not accounts._link_granted(101, CURRENT, "127.0.0.1")
    with db.transaction() as tx:
        row = tx.get(Session, CURRENT)
        assert (row.confirmed_at, row.email_confirmed_at, row.expires_at, row.csrf) == before


def test_v11_account_merge_cannot_discard_unqualified_owner_grant(account_features_context):
    from sunrise_infrastructure.db import StaffRole

    db, accounts, _, _ = account_features_context
    with db.transaction() as tx:
        tx.get(Account, 101).telegram_id = None
        tx.add(StaffRole(account_id=103, role="owner"))
    proof = accounts.request_link(101, "70000000000000004", now(), session_id=CURRENT, ip="127.0.0.1")[
        "proof"
    ]
    with pytest.raises(DomainError) as failure:
        accounts.confirm_telegram_link(103, proof)
    assert failure.value.code == "owner_merge_review_required"
    with db.transaction() as tx:
        assert tx.get(Account, 101).telegram_id is None
        assert tx.get(Account, 103).telegram_id == 70000000000000004
        assert tx.get(StaffRole, 103).role == "owner"
        assert tx.get(Session, CURRENT) is not None


def test_v11_account_legacy_session_metadata_remains_unknown_and_self_revoke_works(account_features_context):
    db, _, service, _ = account_features_context
    with db.transaction() as tx:
        row = tx.get(Session, OTHER)
        row.public_id = row.created_at = row.last_seen_at = row.ip_address = row.browser_label = None
        row.auth_method = "unknown"
    legacy = next(item for item in service.list_sessions(101, CURRENT)["items"] if not item["current"])
    assert all(legacy[name] is None for name in ("created_at", "last_seen_at", "ip_address", "browser_label"))
    assert legacy["auth_method"] == "unknown" and legacy["id"] != OTHER
    current = next(item for item in service.list_sessions(101, CURRENT)["items"] if item["current"])
    assert service.revoke_session(101, CURRENT, current["id"])["current_session_revoked"] is True
    with pytest.raises(DomainError) as failure:
        service.list_sessions(101, CURRENT)
    assert failure.value.status == 401


def test_v11_account_signed_miniapp_email_add_unlink_cannot_reuse_generic_freshness_to_link(
    account_features_context,
    monkeypatch,
):
    from sunrise_infrastructure.commerce import Commerce
    from sunrise_infrastructure.db import MergeProof
    from sunrise_infrastructure.telegram_auth import TelegramAuth

    from tests.unit.test_telegram_miniapp_auth import TOKEN, signed_init

    db, accounts, service, _ = account_features_context
    config = accounts.settings
    config.bot_test_token, config.test_bot_ids = TOKEN, "123456789"
    config.test_telegram_recipients, config.telegram_miniapp_enabled = "70000000000000004", True
    documents = [
        {"document_type": kind, "version": "v11", "url": "https://account.example.test/" + kind}
        for kind in ("offer", "privacy")
    ]
    commerce = Commerce(db)
    commerce.legal.configure(None, documents, "Синтетические документы")
    signed = signed_init(
        {
            "auth_date": str(int(now().timestamp())),
            "user": json.dumps({"id": 70000000000000004, "username": "synthetic_v11"}),
        }
    )
    token, _ = TelegramAuth(accounts).miniapp(signed, "127.0.0.1", legal_accepted=True, documents=documents)
    sid = digest(token)
    add = service.request_email_change(103, sid, "new@account.test", "127.0.0.1")
    service.verify_email_change(
        103,
        sid,
        add["operation_id"],
        add["challenge_id"],
        mail_code(account_features_context, add),
        "127.0.0.1",
    )
    import time

    advanced = time.time() + 121
    monkeypatch.setattr(time, "time", lambda: advanced)
    challenge = accounts.request_link_email(103, sid, "127.0.0.1")
    accounts.verify_link_email(
        103, sid, challenge["challenge_id"], mail_code(account_features_context, challenge), "127.0.0.1"
    )
    proof = service.request_unlink(103, sid, "127.0.0.1")["proof"]
    service.confirm_unlink(70000000000000004, proof)
    target = commerce.resolve_telegram(70000000000000005)
    with db.transaction() as tx:
        session = tx.get(Session, sid)
        assert session.email_confirmed_at is None and session.confirmed_at > now() - timedelta(minutes=5)
        confirmed = session.confirmed_at
    with pytest.raises(DomainError) as failure:
        accounts.request_link(103, target["telegram_id"], confirmed, session_id=sid, ip="127.0.0.1")
    assert failure.value.code == "fresh_login_required"
    with db.transaction() as tx:
        assert list(tx.scalars(select(MergeProof))) == []
        assert tx.get(Subscription, "v11-paid-sub").account_id == 101


def test_v11_account_link_session_freshness_requires_actual_email_ip_and_ownership(account_features_context):
    db, accounts, _, _ = account_features_context
    with db.transaction() as tx:
        tx.get(Account, 101).telegram_id = None
    for sid, ip, expected in (
        (CURRENT, "127.0.0.2", "fresh_login_required"),
        (FOREIGN, "127.0.0.1", "auth_required"),
    ):
        with pytest.raises(DomainError) as failure:
            accounts.request_link(101, "70000000000000004", now(), session_id=sid, ip=ip)
        assert failure.value.code == expected
    with db.transaction() as tx:
        tx.get(Session, CURRENT).ip_address = None
    with pytest.raises(DomainError) as failure:
        accounts.request_link(101, "70000000000000004", now(), session_id=CURRENT, ip="127.0.0.1")
    assert failure.value.code == "fresh_login_required"
    challenge = accounts.request_link_email(101, CURRENT, "127.0.0.1")
    accounts.verify_link_email(
        101, CURRENT, challenge["challenge_id"], mail_code(account_features_context, challenge), "127.0.0.1"
    )
    assert accounts.request_link(101, "70000000000000004", now(), session_id=CURRENT, ip="127.0.0.1")["proof"]
