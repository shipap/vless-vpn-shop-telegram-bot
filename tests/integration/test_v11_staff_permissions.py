import importlib.util
from types import SimpleNamespace

import pytest
from sunrise_core.errors import DomainError

from tests.integration.test_v11_notifications import notification_context as notification_context


def staff_service(context):
    assert importlib.util.find_spec("sunrise_infrastructure.staff_permissions") is not None, (
        "Staff separation absent"
    )
    from sunrise_infrastructure.staff_permissions import StaffPermissions

    db, config, _ = context
    config.admin_email_allowlist.append("private@example.test")
    config.admin_telegram_allowlist = ["202", "201"]
    accounts = SimpleNamespace(db=db, settings=config)
    return StaffPermissions(accounts)


def test_admin_can_create_promos_but_cannot_change_money_or_review_identity(notification_context):
    staff = staff_service(notification_context)
    _, _, (owner, _, admin) = notification_context
    staff.require(admin, "promos_write")
    staff.require(admin, "broadcasts_write")
    for capability in ("wallet_write", "catalog_write", "identity_review", "backups_manage", "staff_manage"):
        with pytest.raises(DomainError) as error:
            staff.require(admin, capability)
        assert error.value.status == 403
        staff.require(owner, capability)


def test_username_and_unverified_allowlisted_email_never_grant_owner(notification_context):
    staff = staff_service(notification_context)
    db, _, (owner, email_only, _) = notification_context
    from sunrise_infrastructure.db import Account

    with db.transaction() as session:
        account = session.get(Account, email_only)
        account.telegram_username = "202"
        account.email = "unverified@example.test"
        account.email_verified = False
    with pytest.raises(DomainError) as error:
        staff.require(email_only, "wallet_write")
    assert error.value.status == 404
    with db.transaction() as session:
        session.get(Account, owner).email_verified = False
    staff.require(owner, "wallet_write")


def test_proven_owner_grant_stays_dormant_after_telegram_unlink(notification_context):
    staff = staff_service(notification_context)
    db, _, (owner, _, _) = notification_context
    from sunrise_infrastructure.db import Account

    with db.transaction() as session:
        account = session.get(Account, owner)
        staff.ensure_owner_in(session, account)
        account.telegram_id = None
    with pytest.raises(DomainError) as error:
        staff.require(owner, "identity_review")
    assert error.value.status == 404
    with db.transaction() as session:
        account = session.get(Account, owner)
        assert staff.role_in(session, account) is None
        account.telegram_id = 202
        assert staff.role_in(session, account) == "owner"


def test_allowed_telegram_id_grants_admin_access_without_email_authority(notification_context):
    staff = staff_service(notification_context)
    db, _, (_, _, candidate) = notification_context
    from sunrise_infrastructure.db import Account

    with db.transaction() as session:
        account = session.get(Account, candidate)
        account.telegram_id = 201
        account.email_verified = False
    staff.require(candidate, "users_read", bot=True)
    with db.transaction() as session:
        session.get(Account, candidate).email_verified = True
    staff.require(candidate, "users_read", bot=True)


def test_actual_backup_operations_are_owner_only_and_denial_creates_no_job(notification_context):
    from sqlalchemy import func, select
    from sunrise_infrastructure.admin_operations import AdminOperations
    from sunrise_infrastructure.commerce import Commerce
    from sunrise_infrastructure.db import Job

    staff = staff_service(notification_context)
    db, config, (owner, email_only, admin) = notification_context
    commerce = Commerce(db, local_test=True, public_settings=config)
    commerce.accounts = staff.accounts
    operations = AdminOperations(commerce, config)
    assert operations.backups(owner)["recipient"] == "owner"
    for actor, status in ((admin, 403), (email_only, 404)):
        for action in (
            lambda actor=actor: operations.backups(actor),
            lambda actor=actor: operations.request_backup(
                actor, "synthetic-backup", "Автоматический аудит", confirmed=True
            ),
        ):
            with pytest.raises(DomainError) as error:
                action()
            assert error.value.status == status
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Job).where(Job.kind == "daily_backup")) == 0
