from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sunrise_core.errors import DomainError
from sunrise_infrastructure.bot_admin import BotAdmin
from sunrise_infrastructure.customer_operations import CustomerOperations
from sunrise_infrastructure.db import (
    Account,
    AdminAuthenticator,
    Device,
    DeviceUnlink,
    Job,
    Setting,
    StaffRole,
    Subscription,
    now,
)
from sunrise_infrastructure.moderation import DeviceModeration
from sunrise_infrastructure.staff_permissions import StaffPermissions, route_capability


def context(*, owner=True, warnings=1, mfa_age=0):
    actor = Account(id=101, telegram_id=70123456789, email="staff@example.test", email_verified=True)
    target = Account(id=102, unlink_warnings=warnings, unlink_warnings_total=5, unlink_blocked=False)
    grant = StaffRole(account_id=actor.id, role="owner", granted_by=actor.id) if owner else None
    auth = AdminAuthenticator(account_id=actor.id, trust_verified_at=now() - timedelta(minutes=mfa_age))
    rows = {(Account, 101): actor, (Account, 102): target, (AdminAuthenticator, 101): auth}
    added = []

    def scalar(query):
        entity = query.column_descriptions[0].get("entity")
        if entity is Account:
            return actor
        if entity is StaffRole:
            return grant
        if entity is AdminAuthenticator:
            return auth
        if entity is Job:
            key = next(iter(query.compile().params.values()))
            return next((item for item in added if isinstance(item, Job) and item.key == key), None)
        return 0

    session = SimpleNamespace(
        scalar=scalar,
        get=lambda model, key, **_: rows.get((model, key)),
        add=added.append,
        execute=lambda *args: None,
        scalars=lambda *args: [],
    )

    @contextmanager
    def transaction():
        yield session

    db = SimpleNamespace(transaction=transaction)
    settings = SimpleNamespace(
        admin_email_allowlist=[actor.email], admin_telegram_allowlist=[str(actor.telegram_id)],
        owner_id=str(actor.telegram_id) if owner else None
    )
    accounts = SimpleNamespace(db=db, settings=settings)
    commerce = SimpleNamespace(
        db=db,
        accounts=accounts,
        _account=lambda session, identifier, *args, **kwargs: rows[(Account, int(identifier))],
    )
    bot_admin = BotAdmin.__new__(BotAdmin)
    bot_admin.db, bot_admin.settings = db, settings
    bot_admin.admin = SimpleNamespace(accounts=accounts, commerce=commerce)
    return DeviceModeration(commerce), bot_admin, session, actor, target, settings, rows, added


def test_verified_admin_can_moderate_device_unlinks_without_backup_or_security_authority():
    moderation, _, session, actor, _, settings, _, _ = context(owner=False)
    permissions = StaffPermissions(moderation.commerce.accounts)
    assert permissions.require_in(session, actor, "device_unlinks_manage").id == actor.id
    for capability in ("security_manage", "backups_manage"):
        with pytest.raises(DomainError) as error:
            permissions.require_in(session, actor, capability)
        assert error.value.status == 403
    assert route_capability("/api/v1/admin/users/102/device-moderation", "POST") == "device_unlinks_manage"


def test_remove_warning_preserves_history_and_idempotency():
    service, _, _, actor, target, settings, _, added = context()
    result = service.moderate(
        actor.id, target.id, "remove_warning", "Подтверждённая ошибка", "same-key", settings, True
    )
    assert result["current_warnings"] == 0 and result["total_warnings"] == 5
    assert (
        service.moderate(
            actor.id, target.id, "remove_warning", "Подтверждённая ошибка", "same-key", settings, True
        )
        == result
    )
    assert target.unlink_blocked is False
    assert len([item for item in added if isinstance(item, Job)]) == 1


def test_second_warning_blocks_only_unlinks_and_resets_current_count():
    service, _, _, actor, target, settings, _, added = context()
    result = service.moderate(actor.id, target.id, "warn", "Повторное нарушение", "second", settings, True)
    assert result["unlink_blocked"] is True and result["current_warnings"] == 0
    assert result["total_warnings"] == 6
    assert len([item for item in added if isinstance(item, Job)]) == 1


def test_same_moderation_key_cannot_change_target_or_action():
    service, _, _, actor, target, settings, _, _ = context()
    service.moderate(actor.id, target.id, "warn", "Повторное нарушение", "same", settings, True)
    with pytest.raises(DomainError) as error:
        service.moderate(actor.id, target.id, "unblock", "Проверено владельцем", "same", settings, True)
    assert error.value.code == "idempotency_conflict"


@pytest.mark.parametrize("mfa_age", [6, -1])
def test_device_moderation_rejects_stale_or_future_mfa(mfa_age):
    service, _, _, actor, target, settings, _, added = context(mfa_age=mfa_age)
    with pytest.raises(DomainError) as error:
        service.moderate(actor.id, target.id, "warn", "Проверка предупреждения", "key", settings, True)
    assert error.value.code == "mfa_required"
    assert target.unlink_warnings == 1 and not added


def test_owner_notice_resolver_reads_current_saved_event_without_exposing_network_identity():
    _, bot, _, actor, target, _, rows, _ = context()
    event = DeviceUnlink(
        id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        account_id=target.id,
        original_account_id=target.id,
        subscription_id="sub",
        device_id="device",
    )
    rows[(DeviceUnlink, event.id)] = event
    rows[(Subscription, "sub")] = Subscription(id="sub", account_id=target.id)
    assert bot.bot_owner_device_notice(actor.id, event.id) == {"account_id": "102", "event_id": event.id}
    assert bot.bot_owner_device_notice(actor.id, "a" * 32) == {"account_id": "102", "event_id": event.id}


def test_admin_cannot_resolve_owner_only_unlink_notice():
    _, bot, _, actor, _, _, _, _ = context(owner=False)
    with pytest.raises(DomainError) as error:
        bot.bot_owner_device_notice(actor.id, "a" * 32)
    assert error.value.status in {403, 404}


def test_revoked_runtime_admission_prevents_moderation_records():
    service, _, _, actor, target, settings, _, added = context()

    def denied(*args):
        raise DomainError("scoped_test_admission_denied", "Допуск отозван.", 403)

    service.commerce.runtime_policy = SimpleNamespace(require_current=denied)
    with pytest.raises(DomainError) as error:
        service.moderate(actor.id, target.id, "warn", "Подтверждение проверки", "key", settings, True)
    assert error.value.code == "scoped_test_admission_denied"
    assert target.unlink_warnings == 1 and not added


def test_xrocket_fee_estimate_is_accepted_without_changing_actual_fee_fields():
    _, bot, _, actor, _, _, _, _ = context()
    bot._mutation = lambda *args, **kwargs: None
    bot.admin._save_setting = lambda actor, key, value, reason: {"key": key, "value": value}
    result = bot.bot_settings_update(
        actor.id,
        "fee_estimates",
        {"xrocket": {"percent": "1.50", "fixed": "0", "currency": "RUB"}},
        "Подтверждённая оценка",
        confirmed=True,
    )
    assert result == {
        "key": "accounting_fee_estimates",
        "value": {
            "xrocket": {
                "percent": "1.50",
                "fixed": "0",
                "currency": "RUB",
            }
        },
    }


@pytest.mark.parametrize("legacy", [False, True])
def test_same_unlink_key_rejects_single_vs_bulk_mode_without_reclassifying_original_job(legacy):
    service, _, session, _, target, _, rows, added = context()
    device = Device(
        id="device",
        subscription_id="sub",
        name="Телефон",
        panel_hwid="synthetic",
        generation=1,
        revoked=False,
    )
    service.commerce._sub = lambda *args: SimpleNamespace(id="sub", environment="local_test")
    ops = CustomerOperations(service.commerce)
    ops._guards = lambda *args: None
    ops._before_queue = lambda *args: None
    ops._binding = lambda *args: {}
    session.scalars = lambda *args: [device]

    def add(row):
        added.append(row)
        if isinstance(row, Setting):
            rows[(Setting, row.key)] = row

    session.add = add
    session.flush = lambda: [setattr(row, "id", "job") for row in added if isinstance(row, Job)]
    result = ops.revoke_devices(target.id, "sub", [device.id], "same", True, operation_mode="single")
    rows[(Job, "job")] = next(row for row in added if isinstance(row, Job))
    if legacy:
        record = next(row for row in added if isinstance(row, Setting))
        record.value = record.value | {
            "request": {key: value for key, value in record.value["request"].items() if key != "unlink_mode"}
        }
    assert ops.revoke_devices(target.id, "sub", [device.id], "same", True, operation_mode="single") == result
    with pytest.raises(DomainError) as error:
        ops.revoke_devices(target.id, "sub", [device.id], "same", True, operation_mode="bulk")
    assert error.value.code == "idempotency_conflict"
    assert rows[(Job, "job")].payload["unlink_mode"] == "single"
