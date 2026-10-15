"""Разбор неизвестного счёта скрывает сигнал, сохраняя финансовую неопределённость."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sunrise_core.errors import DomainError
from sunrise_infrastructure.admin import Admin
from sunrise_infrastructure.db import (
    Account,
    Audit,
    Order,
    Payment,
    ProviderOperation,
    Quote,
    Session,
    Setting,
    now,
)
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
            quote = Quote(account_id=owner, snapshot={}, expires_at=now() - timedelta(hours=1))
            session.add(quote)
            session.flush()
            session.add(
                Order(
                    id=f"unknown-order-{index}",
                    account_id=owner,
                    quote_id=quote.id,
                    idempotency_key=f"unknown-{index}",
                    amount="149.00",
                    currency="RUB",
                    environment="live",
                    created_at=now() - timedelta(hours=2 - index),
                )
            )
            session.flush()
            session.add(
                ProviderOperation(
                    id=f"unknown-op-{index}",
                    order_id=f"unknown-order-{index}",
                    terminal="synthetic-terminal",
                    state="outcome_unknown",
                    request={"order_id": f"unknown-order-{index}", "private": "must-stay-private"},
                )
            )
    return SimpleNamespace(db=db, config=config, owner=owner, other=other, admin=admin)


def current(item, limit=50):
    return OperationalMonitor(item.db, item.config).snapshot(limit)


def acknowledge(item, row, actor=None):
    return item.admin.acknowledge_provider_operation(
        item.owner if actor is None else actor,
        row["reference"],
        row["ack_token"],
        "Проверено владельцем в истории кассы",
    )


def test_acknowledgement_hides_exact_signal_before_count_and_limit_and_preserves_unknown(review_context):
    item = review_context
    row = current(item, 1)["items"][0]
    assert row.get("can_acknowledge") is True
    assert "must-stay-private" not in str(row)
    first = acknowledge(item, row)
    assert acknowledge(item, row) == first
    restarted = OperationalMonitor(item.db, item.config)
    result = restarted.snapshot(limit=1)
    assert result["signal_count"] == result["counts"]["provider_operation"] == 1
    assert result["items"][0]["reference"] == "unknown-op-1"
    assert result["truncated"] is False
    with item.db.transaction() as session:
        operation = session.get(ProviderOperation, "unknown-op-0")
        assert operation.state == "outcome_unknown" and operation.provider_id is None
        assert operation.request["private"] == "must-stay-private"
        assert session.get(Order, operation.order_id).status == "awaiting_payment"
        assert session.scalar(select(func.count()).select_from(Payment)) == 0
        assert session.scalar(select(func.count()).select_from(Audit)) == 1


def test_acknowledged_unknown_operation_no_longer_sends_owner_attention_alert(review_context):
    item = review_context
    for row in current(item)["items"]:
        acknowledge(item, row)
    assert OperationalMonitor(item.db, item.config).notify_owner() == 0


@pytest.mark.parametrize("field,value", [("state", "review"), ("terminal", "changed-terminal")])
def test_changed_operation_reappears_and_stale_acknowledgement_cannot_hide_it(review_context, field, value):
    item = review_context
    row = current(item)["items"][0]
    acknowledge(item, row)
    with item.db.transaction() as session:
        setattr(session.get(ProviderOperation, row["reference"]), field, value)
    assert current(item)["signal_count"] == 2
    with pytest.raises(DomainError) as rejected:
        acknowledge(item, row)
    assert rejected.value.status == 409


@pytest.mark.parametrize("changed", ["provider_id", "order_status", "payment", "amount"])
def test_raced_financial_evidence_rejects_stale_ack_and_never_disappears(review_context, changed):
    item = review_context
    row = current(item)["items"][0]
    acknowledge(item, row)
    with item.db.transaction() as session:
        operation = session.get(ProviderOperation, row["reference"])
        order = session.get(Order, operation.order_id)
        if changed == "provider_id":
            operation.provider_id = "late-invoice"
        elif changed == "order_status":
            order.status = "paid"
        elif changed == "amount":
            order.amount = "150.00"
        else:
            session.add(
                Payment(
                    provider="rollypay",
                    provider_id="late-payment",
                    terminal=operation.terminal,
                    environment="live",
                    order_id=order.id,
                    account_id=order.account_id,
                    amount=order.amount,
                    currency=order.currency,
                )
            )
    assert current(item)["counts"]["provider_operation"] == 2
    with pytest.raises(DomainError) as rejected:
        acknowledge(item, row)
    assert rejected.value.status == 409


def test_acknowledgement_requires_current_token_and_owner_capability(review_context):
    item = review_context
    row = current(item)["items"][0]
    with pytest.raises(DomainError) as rejected:
        acknowledge(item, row, actor=item.other)
    assert rejected.value.status == 404
    item.config.admin_telegram_allowlist = ["1048783819"]
    with item.db.transaction() as session:
        session.get(Account, item.other).telegram_id = 1048783819
    with pytest.raises(DomainError) as rejected:
        acknowledge(item, row, actor=item.other)
    assert rejected.value.status == 403
    with pytest.raises(DomainError) as rejected:
        acknowledge(item, row | {"ack_token": "0" * 64})
    assert rejected.value.status == 409
    assert current(item)["signal_count"] == 2
    with item.db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Audit)) == 0
        assert session.scalar(select(func.count()).select_from(Setting)) == 0


def test_http_review_enforces_fresh_mfa_csrf_confirmation_and_automatic_audit_reason(mfa_context):  # noqa: F811
    import pyotp

    db, app, client, _, _, _, _ = mfa_context
    endpoint = "/api/v1/admin/operations/provider/http-unknown/acknowledge"
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
        quote = Quote(account_id=owner, snapshot={}, expires_at=now())
        session.add(quote)
        session.flush()
        order = Order(
            account_id=owner,
            quote_id=quote.id,
            idempotency_key="http-unknown",
            amount="149.00",
            currency="RUB",
        )
        session.add(order)
        session.flush()
        session.add(
            ProviderOperation(
                id="http-unknown",
                order_id=order.id,
                terminal="synthetic",
                state="outcome_unknown",
                request={},
            )
        )
    row = OperationalMonitor(db, app.state.settings).snapshot()["items"][0]
    body["ack_token"] = row["ack_token"]
    assert client.post(endpoint, json=body).status_code == 403
    assert client.post(endpoint, headers=headers, json=body | {"confirmed": False}).status_code == 400
    assert client.post(endpoint, headers=headers, json=body | {"confirmed": "true"}).status_code == 422
    with db.transaction() as session:
        login = session.scalar(select(Session).where(Session.account_id == owner))
        login.mfa_at = now() - timedelta(minutes=6)
    assert client.post(endpoint, headers=headers, json=body).status_code == 403
    with db.transaction() as session:
        login = session.scalar(select(Session).where(Session.account_id == owner))
        login.mfa_at = now()
    response = client.post(endpoint, headers=headers, json=body)
    assert response.status_code == 200, response.text
    assert response.json()["payment_state"] == "outcome_unknown"
    with db.transaction() as session:
        event = session.scalar(select(Audit).where(Audit.action == "provider_operation_reviewed"))
        assert event.actor_id == owner and event.reason == "Подтверждённое действие через интерфейс"
