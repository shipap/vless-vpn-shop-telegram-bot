from types import SimpleNamespace

import pytest
from sunrise_infrastructure.db import (
    Account,
    Device,
    DeviceUnlink,
    Job,
    Notification,
    NotificationDelivery,
    StaffRole,
)
from sunrise_infrastructure.moderation import DeviceModeration


@pytest.mark.parametrize("contact", ["telegram", "verified_email", "unverified_email"])
def test_verified_live_device_unlink_emits_canonical_owner_inbox_and_telegram_once(monkeypatch, contact):
    from sunrise_infrastructure import panel_news

    actor = Account(
        id=9007199254740993,
        telegram_id=70123456789 if contact == "telegram" else None,
        email="buyer@example.invalid" if contact != "telegram" else None,
        email_verified=contact == "verified_email",
        telegram_username="test_customer",
    )
    owner = Account(
        id=9007199254741000, telegram_id=70123456790, email="owner@synthetic.test", email_verified=True
    )
    device = Device(
        id="synthetic-device",
        subscription_id="synthetic-sub",
        panel_hwid="synthetic-hwid",
        generation=1,
        revoked=False,
        name="Синтетический телефон",
    )
    grant = StaffRole(account_id=owner.id, role="owner", granted_by=owner.id)
    rows = {(Account, actor.id): actor, (Account, owner.id): owner, (StaffRole, owner.id): grant}
    added = []

    def add(row):
        added.append(row)
        if isinstance(row, (Notification, DeviceUnlink)):
            row.id = "synthetic-" + type(row).__name__.lower()
            rows[(type(row), row.id)] = row

    def scalar(query):
        entity = getattr(query, "column_descriptions", [{}])[0].get("entity")
        return device if entity is Device else None

    session = SimpleNamespace(
        scalar=scalar,
        get=lambda model, key, **_: rows.get((model, key)),
        add=add,
        flush=lambda: None,
        execute=lambda *args: None,
        scalars=lambda *args: [],
    )
    policy = SimpleNamespace(production=True, require_current=lambda *args: None)
    config = SimpleNamespace(
        runtime_mode="production",
        _runtime_policy=policy,
        owner_id=None,
        admin_email_allowlist=[owner.email],
        admin_telegram_allowlist=[str(owner.telegram_id)],
        email_allowlist=[],
        test_telegram_recipients="",
    )
    commerce = SimpleNamespace(
        db=None,
        live=True,
        public_settings=config,
        owner_id=None,
        runtime_policy=policy,
        _account=lambda *args, **kwargs: actor,
    )
    monkeypatch.setattr(panel_news, "owner_account_in", lambda *args: owner)
    moderation = DeviceModeration(commerce)
    monkeypatch.setattr(
        moderation,
        "state",
        lambda *args: {
            "unlink_month": 1,
            "unlink_total": 1,
            "current_warnings": 0,
            "unlink_individual_total": 1,
            "unlink_bulk_total": 2,
            "unlink_individual_month": 1,
            "unlink_bulk_month": 0,
            "unlink_unknown_total": 0,
            "unlink_unknown_month": 0,
        },
    )
    assert moderation.complete(session, actor.id, device.subscription_id, device.id)
    assert len([row for row in added if isinstance(row, DeviceUnlink)]) == 1
    notices = [row for row in added if isinstance(row, Notification)]
    assert len(notices) == 1 and notices[0].account_id == owner.id and notices[0].audience == "owner"
    if contact == "telegram":
        assert "@test_customer" in notices[0].body and "70123456789" in notices[0].body
    else:
        assert "@test_customer" not in notices[0].body and "70123456789" not in notices[0].body
        assert ("buyer@example.invalid" in notices[0].body) is (contact == "verified_email")
    assert "Синтетический телефон" in notices[0].body
    assert notices[0].title == "Удаление HWID"
    assert "Текущих предупреждений 0/2" in notices[0].body
    assert "Всего удалено поштучно 1" in notices[0].body
    assert "Массовых удалений 2" in notices[0].body
    assert "Поштучно за месяц 1" in notices[0].body
    assert "Массовых за месяц 0" in notices[0].body
    assert "неизвестным типом" not in notices[0].body
    assert "synthetic-hwid" not in notices[0].body
    assert [action["operation"] for action in notices[0].actions] == [
        "device_unlink_profile",
        "device_unlink_warn",
        "device_unlink_block",
    ]
    assert all(
        action["metadata"]
        == {
            "account_id": str(actor.id),
            "unlink_event_id": "synthetic-deviceunlink",
        }
        for action in notices[0].actions
    )
    deliveries = [row for row in added if isinstance(row, NotificationDelivery)]
    assert (
        len(deliveries) == 1
        and deliveries[0].channel == "telegram"
        and deliveries[0].recipient == str(owner.telegram_id)
    )
    assert not any(isinstance(row, Job) and row.kind == "owner_device_unlink" for row in added)
    assert moderation.complete(session, actor.id, device.subscription_id, device.id) is False
    assert len([row for row in added if isinstance(row, DeviceUnlink)]) == 1
