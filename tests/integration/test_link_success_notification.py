import fakeredis
import pytest
from sqlalchemy import select
from sunrise_core.errors import DomainError
from sunrise_infrastructure.accounts import Accounts
from sunrise_infrastructure.db import Account, MergeProof, Notification, NotificationDelivery, now
from sunrise_infrastructure.notifications import NotificationService
from sunrise_infrastructure.security import RateGuard
from sunrise_infrastructure.settings import Settings

from tests.integration.test_v11_notifications import notification_context as notification_context


def test_link_notice_has_one_inbox_and_both_verified_channels(notification_context):
    db, config, (owner, _, _) = notification_context
    notifier = NotificationService(db, config)
    with db.transaction() as session:
        for _ in range(2):
            notifier.emit_in(
                session,
                owner,
                "telegram-linked:proof-1",
                "identity_linked",
                "Аккаунты связаны",
                "Telegram привязан. Если это сделали не вы, откройте аккаунт и подтвердите отвязку.",
                channel="all",
                target="/#account",
                actions=[{"label": "Отвязать Telegram", "target": "/#account"}],
            )
    assert notifier.list(owner)["unread_count"] == 1
    with db.transaction() as session:
        assert sorted(session.scalars(select(NotificationDelivery.channel))) == ["email", "telegram"]
        assert session.scalar(select(Notification)).target == "/#account"


def test_link_notice_actually_delivers_to_both_current_allowed_identities(notification_context):
    from types import SimpleNamespace

    db, config, (owner, _, _) = notification_context
    emails, telegram = [], []
    notifier = NotificationService(
        db,
        config,
        mail_transport=SimpleNamespace(deliver=lambda *args: emails.append(args)),
        telegram_transport=lambda *args: telegram.append(args),
    )
    with db.transaction() as session:
        notifier.emit_in(
            session,
            owner,
            "linked-delivery",
            "identity_linked",
            "Аккаунты связаны",
            "Подтвердите отвязку в аккаунте.",
            channel="all",
            target="/#account",
        )
    assert notifier.deliver_pending() == 2
    assert len(emails) == len(telegram) == 1
    assert emails[0][0] == "owner@example.test"
    assert telegram[0][0] == "202"


def test_explicit_email_channel_does_not_go_to_telegram(notification_context):
    db, config, (owner, _, _) = notification_context
    with db.transaction() as session:
        NotificationService(db, config).emit_in(
            session, owner, "explicit-email", "security", "Проверка", "Аккаунт изменён", channel="email"
        )
    with db.transaction() as session:
        assert list(session.scalars(select(NotificationDelivery.channel))) == ["email"]


def test_actual_confirmed_merge_notifies_both_identities_once(notification_context):
    db, _, (_, email_only, telegram_only) = notification_context
    with db.transaction() as session:
        session.get(Account, telegram_only).email = None
        session.get(Account, telegram_only).email_verified = False
    settings = Settings(
        _env_file=None,
        database_url=db.engine.url.render_as_string(hide_password=False),
        secret_key="synthetic-link-test-key-" * 3,
    )
    service = Accounts(db, RateGuard(fakeredis.FakeRedis()), settings)
    proof = service.request_link(email_only, "201", now())["proof"]
    linked = service.confirm_telegram_link(telegram_only, proof)
    assert linked["id"] == str(email_only)
    with db.transaction() as session:
        notices = list(session.scalars(select(Notification).where(Notification.kind == "identity_linked")))
        assert len(notices) == 1
        assert notices[0].account_id == email_only
        assert notices[0].actions == [{"label": "Отвязать Telegram", "target": "/#account"}]
        assert sorted(session.scalars(select(NotificationDelivery.channel))) == ["email", "telegram"]
        assert proof not in notices[0].body
    with pytest.raises(DomainError):
        service.confirm_telegram_link(telegram_only, proof)
    assert NotificationService(db, settings).list(email_only)["unread_count"] == 1


def test_notification_failure_rolls_back_merge_and_keeps_single_use_proof(notification_context, monkeypatch):
    db, _, (_, email_only, telegram_only) = notification_context
    with db.transaction() as session:
        session.get(Account, telegram_only).email = None
        session.get(Account, telegram_only).email_verified = False
    settings = Settings(
        _env_file=None,
        database_url=db.engine.url.render_as_string(hide_password=False),
        secret_key="synthetic-link-test-key-" * 3,
    )
    service = Accounts(db, RateGuard(fakeredis.FakeRedis()), settings)
    proof = service.request_link(email_only, "201", now())["proof"]

    def fail(*args, **kwargs):
        raise DomainError("synthetic_notification_failure", "Синтетический сбой", 503)

    monkeypatch.setattr(NotificationService, "emit_in", fail)
    with pytest.raises(DomainError):
        service.confirm_telegram_link(telegram_only, proof)
    with db.transaction() as session:
        assert session.get(Account, email_only).telegram_id is None
        assert session.get(Account, telegram_only).telegram_id == 201
        assert session.get(MergeProof, proof.split(".", 1)[0]).used is False
        assert list(session.scalars(select(Notification))) == []
