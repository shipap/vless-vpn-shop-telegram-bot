import pytest
from sqlalchemy import select
from sunrise_core.errors import DomainError
from sunrise_infrastructure.admin import Admin
from sunrise_infrastructure.db import Account, Audit, Setting, StaffRole

from tests.integration.test_panel_28_workflows import TG
from tests.integration.test_panel_28_workflows import panel28_workflow as panel28_workflow
from tests.integration.test_panel_entitlement_freshness import entitlement as entitlement


@pytest.mark.parametrize("role", ["owner", "admin", "not_allowlisted"])
def test_retention_policy_requires_admitted_telegram_owner_and_audits(entitlement, role):
    db, _, commerce, *_ = entitlement
    settings = commerce.accounts.settings
    settings.admin_email_allowlist = ["panel28@example.test"]
    settings.admin_telegram_allowlist = [] if role == "not_allowlisted" else [TG]
    with db.transaction() as session:
        account = session.get(Account, 301)
        account.email, account.email_verified = "panel28@example.test", True
        session.add(StaffRole(account_id=301, role="admin" if role == "admin" else "owner", granted_by=301))
    admin = Admin(commerce, None)
    if role != "owner":
        with pytest.raises(DomainError) as denied:
            admin.save_retention(301, True, 7, "Проверка политики удаления")
        assert denied.value.status == (403 if role == "admin" else 404)
        with db.transaction() as session:
            assert session.get(Setting, "subscription_retention") is None
        return
    assert admin.save_retention(301, True, 7, "Проверка политики удаления") == {
        "enabled": True,
        "days_after_expiry": 7,
    }
    with db.transaction() as session:
        assert session.get(Setting, "subscription_retention").value == {
            "enabled": True,
            "days_after_expiry": 7,
        }
        audit = session.scalar(select(Audit).where(Audit.action == "subscription_retention_update"))
        assert audit.actor_id == 301 and audit.details["after"]["enabled"] is True


@pytest.mark.parametrize("enabled,days", [("true", 7), (True, True), (True, 8)])
def test_invalid_retention_policy_is_rejected_before_write(entitlement, enabled, days):
    commerce = entitlement[2]
    with pytest.raises(DomainError) as denied:
        Admin(commerce, None).save_retention(301, enabled, days, "Проверка строгой схемы")
    assert denied.value.code == "retention_policy_invalid"
