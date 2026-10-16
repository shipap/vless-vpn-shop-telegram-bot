"""Подтверждение сигнала не меняет доставку и не вызывает повторную отправку."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sunrise_core.errors import DomainError
from sunrise_infrastructure.admin import Admin
from sunrise_infrastructure.db import Account, Audit, NotificationDelivery, Session, Setting, now
from sunrise_infrastructure.notifications import NotificationService
from sunrise_infrastructure.operational_monitor import OperationalMonitor

from tests.integration.test_v10_admin_mfa import enroll, mfa_context  # noqa: F401
from tests.integration.test_v11_notifications import notification_context  # noqa: F401


@pytest.fixture
def review_context(notification_context):  # noqa: F811
    db, config, (owner, other, _) = notification_context
    accounts = SimpleNamespace(db=db, settings=config)
    admin = Admin(SimpleNamespace(db=db, accounts=accounts, public_settings=config), None)
    with db.transaction() as session:
        for index in range(2):
            notice = NotificationService(db, config).emit_in(
                session, owner, f"review-{index}", "security", "Событие", "Текст", channel="inbox"
            )
            session.add(
                NotificationDelivery(
                    id=f"review-delivery-{index}",
                    notification_id=notice.id,
                    channel="email",
                    recipient="owner@example.test",
                    state="review",
                    attempts=1,
                    available_at=now() - timedelta(hours=2 - index),
                    last_error="mail_delivery_unknown",
                )
            )
    return SimpleNamespace(db=db, config=config, owner=owner, other=other, admin=admin)


def snapshot(item, limit=50):
    return OperationalMonitor(item.db, item.config).snapshot(limit)


def acknowledge(item, row, actor=None):
    return item.admin.acknowledge_notification_delivery(
        item.owner if actor is None else actor, row["reference"], row["ack_token"], "Проверено владельцем"
    )


def delivery_values(row):
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


def test_review_hides_before_count_limit_and_keeps_delivery_and_audit(review_context):
    item = review_context
    row = snapshot(item, 1)["items"][0]
    assert row.get("can_acknowledge") is True
    assert "owner@example.test" not in str(row)
    with item.db.transaction() as session:
        before = delivery_values(session.get(NotificationDelivery, row["reference"]))
    assert acknowledge(item, row) == acknowledge(item, row)
    current = snapshot(item, 1)
    assert current["counts"]["delivery"] == current["signal_count"] == 1
    assert current["items"][0]["reference"] == "review-delivery-1"
    assert current["truncated"] is False
    with item.db.transaction() as session:
        assert delivery_values(session.get(NotificationDelivery, row["reference"])) == before
        assert session.scalar(select(func.count()).select_from(Audit)) == 1
        marker = session.scalar(select(Setting).where(Setting.key.like("delivery-review:%")))
        assert "owner@example.test" not in str(marker.value)
    acknowledge(item, current["items"][0])
    assert OperationalMonitor(item.db, item.config).notify_owner() == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("attempts", 2),
        ("last_error", "different_failure"),
        ("recipient", "changed@example.test"),
        ("channel", "telegram"),
        ("available_at", now() - timedelta(minutes=20)),
    ],
)
def test_changed_delivery_reappears_and_old_token_is_rejected(review_context, field, value):
    item = review_context
    row = snapshot(item)["items"][0]
    acknowledge(item, row)
    with item.db.transaction() as session:
        setattr(session.get(NotificationDelivery, row["reference"]), field, value)
    assert snapshot(item)["counts"]["delivery"] == 2
    with pytest.raises(DomainError) as rejected:
        acknowledge(item, row)
    assert rejected.value.status == 409


@pytest.mark.parametrize("state", ["pending", "in_flight", "delivered", "blocked", "cancelled"])
def test_active_or_terminal_delivery_cannot_be_acknowledged(review_context, state):
    item = review_context
    row = snapshot(item)["items"][0]
    with item.db.transaction() as session:
        session.get(NotificationDelivery, row["reference"]).state = state
    with pytest.raises(DomainError) as rejected:
        acknowledge(item, row)
    assert rejected.value.status == 409
    current = next((x for x in snapshot(item)["items"] if x["reference"] == row["reference"]), None)
    assert current is None or current["can_acknowledge"] is False


def test_review_requires_current_token_and_owner_capability(review_context):
    item = review_context
    row = snapshot(item)["items"][0]
    with pytest.raises(DomainError) as rejected:
        acknowledge(item, row, item.other)
    assert rejected.value.status == 404
    item.config.admin_telegram_allowlist = ["1048783819"]
    with item.db.transaction() as session:
        session.get(Account, item.other).telegram_id = 1048783819
    with pytest.raises(DomainError) as rejected:
        acknowledge(item, row, item.other)
    assert rejected.value.status == 403
    with pytest.raises(DomainError) as rejected:
        acknowledge(item, row | {"ack_token": "0" * 64})
    assert rejected.value.status == 409
    assert snapshot(item)["signal_count"] == 2


def test_http_review_enforces_mfa_csrf_confirmation_and_preserves_delivery(mfa_context):  # noqa: F811
    import pyotp

    db, app, client, _, _, _, _ = mfa_context
    endpoint = "/api/v1/admin/operations/deliveries/http-review/acknowledge"
    body = {"ack_token": "0" * 64, "confirmed": True}
    assert client.post(endpoint, json=body).status_code == 404
    auth, headers, pending = enroll(mfa_context)
    owner = int(auth["account"]["id"])
    assert client.post(endpoint, headers=headers, json=body).status_code == 403
    assert (
        client.post(
            "/api/v1/admin/mfa/enroll/confirm",
            headers=headers,
            json={"code": pyotp.TOTP(pending["secret"]).now()},
        ).status_code
        == 200
    )
    with db.transaction() as session:
        notice = NotificationService(db, app.state.settings).emit_in(
            session, owner, "http-review", "security", "Событие", "Текст", channel="inbox"
        )
        session.add(
            NotificationDelivery(
                id="http-review",
                notification_id=notice.id,
                channel="email",
                recipient="owner@example.test",
                state="review",
                last_error="mail_delivery_unknown",
            )
        )
    row = next(
        x
        for x in OperationalMonitor(db, app.state.settings).snapshot()["items"]
        if x["reference"] == "http-review"
    )
    body["ack_token"] = row["ack_token"]
    assert client.post(endpoint, json=body).status_code == 403
    assert client.post(endpoint, headers=headers, json=body | {"confirmed": False}).status_code == 400
    assert client.post(endpoint, headers=headers, json=body | {"confirmed": "true"}).status_code == 422
    with db.transaction() as session:
        login = session.scalar(select(Session).where(Session.account_id == owner))
        login.mfa_at = now() - timedelta(minutes=6)
    assert client.post(endpoint, headers=headers, json=body).status_code == 403
    with db.transaction() as session:
        session.scalar(select(Session).where(Session.account_id == owner)).mfa_at = now()
    response = client.post(endpoint, headers=headers, json=body)
    assert response.status_code == 200, response.text
    assert response.json()["delivery_state"] == "review"
    with db.transaction() as session:
        delivery = session.get(NotificationDelivery, "http-review")
        assert delivery.state == "review" and delivery.last_error == "mail_delivery_unknown"
        event = session.scalar(select(Audit).where(Audit.action == "notification_delivery_reviewed"))
        assert event.actor_id == owner and event.reason == "Подтверждённое действие через интерфейс"
