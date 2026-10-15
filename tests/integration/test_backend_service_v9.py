from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sunrise_core.errors import DomainError
from sunrise_infrastructure.db import Account, Audit, Device, Job, Ledger, Product, Subscription
from test_bot_admin_v7 import bot_admin_v7 as bot_admin_v7


def test_username_refresh_search_and_removal(bot_admin_v7):
    f, aid, _ = bot_admin_v7
    c = f.admin.commerce
    with f.db.transaction() as s:
        tg = s.get(Account, aid).telegram_id
    assert c.resolve_telegram(tg, username="Shared_User")["telegram_username"] == "Shared_User"
    assert f.bot_users_search(aid, "@shared_user")["items"][0]["id"] == str(aid)
    assert f.admin.list_items("users", search="@shared_user")["total"] == 1
    c.resolve_telegram(tg)  # Calls without Telegram username evidence must preserve it.
    assert f.bot_users_search(aid, "SHARED_USER")["total"] == 1
    c.resolve_telegram(tg, username=None)
    assert f.bot_users_search(aid, "@shared_user")["total"] == 0


def test_owner_moderation_idempotency_and_only_unlink_restriction(bot_admin_v7):
    f, aid, did = bot_admin_v7
    with f.db.transaction() as s:
        f.settings.owner_id = str(s.get(Account, aid).telegram_id)
    f.admin.commerce.owner_id = f.settings.owner_id
    one = f.bot_user_moderate(aid, did, "warn", "Первая проверка", "warning-one", confirmed=True)
    assert one["current_warnings"] == 1 and one["unlink_blocked"] is False
    assert f.bot_user_moderate(aid, did, "warn", "Первая проверка", "warning-one", confirmed=True) == one
    two = f.bot_user_moderate(aid, did, "warn", "Вторая проверка", "warning-two", confirmed=True)
    assert two["current_warnings"] == 0 and two["total_warnings"] == 2 and two["unlink_blocked"]
    with pytest.raises(DomainError) as error:
        f.bot_user_moderate(did, aid, "block", "Нельзя менять", "unauthorized", confirmed=True)
    assert error.value.status == 404
    with f.db.transaction() as s:
        p = Product(
            included_devices=4,
            currency="RUB",
            code=uuid4().hex,
            name="Тест",
            tier_level=1,
            price=99,
            extra_device_price=50,
        )
        s.add(p)
        s.flush()
        sub = Subscription(
            devices_limit=4,
            account_id=did,
            product_id=p.id,
            status="active",
            expires_at=datetime.now(UTC) + timedelta(days=10),
        )
        s.add(sub)
        s.flush()
        device = Device(subscription_id=sub.id, name="Устройство")
        s.add(device)
        s.flush()
        sid, device_id = sub.id, device.id
    from tests.legal_fixture import accept_test_legal

    accept_test_legal(f.admin.commerce, did)
    with pytest.raises(DomainError) as error:
        f.admin.commerce.revoke_device(did, sid, device_id)
    assert error.value.code == "device_unlink_blocked"
    assert f.admin.commerce.dashboard(did)["subscriptions"][0]["status"] == "active"
    assert f.admin.commerce.quote(did, p.id, 30, "renew", sid)["total"] == "99.00"
    f.bot_user_moderate(aid, did, "unblock", "Разрешено снова", "unblock-one", confirmed=True)
    assert f.admin.commerce.revoke_device(did, sid, device_id)["status"] == "revoked"
    assert f.admin.commerce.revoke_device(did, sid, device_id)["status"] == "revoked"
    detail = f.bot_user_detail(aid, did)
    assert detail["moderation"]["unlink_total"] == 1
    assert detail["moderation"]["unlink_month"] == 1
    with f.db.transaction() as s:
        alerts = list(s.scalars(select(Job).where(Job.kind == "owner_device_unlink", Job.account_id == did)))
        assert len(alerts) == 1
        assert alerts[0].payload["target_id"] == f.settings.owner_id
        assert s.scalar(select(Audit).where(Audit.action == "device_moderation", Audit.target == str(did)))


def test_user_referral_detail_is_bounded_and_invitees_separate(bot_admin_v7):
    f, aid, did = bot_admin_v7
    with f.db.transaction() as s:
        s.get(Account, did).referrer_id = aid
        s.add(Account(email=f"{uuid4()}@example.test", referrer_id=aid))
        s.add(Ledger(account_id=aid, operation="referral_payment:" + uuid4().hex, amount=15))
        s.add(Ledger(account_id=aid, operation="reversal:unrelated-bonus-operation:refund", amount=-100))
    result = f.bot_user_detail(aid, aid, limit=1)
    assert result["referral"]["invited_total"] == 2
    assert result["referral"]["earned_total"] == "15.000000"
    assert f.bot_user_detail(aid, did)["inviter"]["id"] == str(aid)
    invitees = f.bot_user_invitees(aid, aid, limit=1)
    assert invitees["total"] == 2 and len(invitees["items"]) == 1
    assert "invitees" not in result


def test_unlink_month_uses_moscow_calendar_boundary(bot_admin_v7):
    from sunrise_infrastructure.db import DeviceUnlink
    from sunrise_infrastructure.moderation import DeviceModeration

    f, aid, did = bot_admin_v7
    with f.db.transaction() as s:
        sub = Subscription(devices_limit=4, account_id=did)
        s.add(sub)
        s.flush()
        for stamp in [datetime(2031, 1, 31, 20, 59, tzinfo=UTC), datetime(2031, 1, 31, 21, 0, tzinfo=UTC)]:
            device = Device(subscription_id=sub.id, name="Тест")
            s.add(device)
            s.flush()
            s.add(
                DeviceUnlink(
                    account_id=did,
                    original_account_id=did,
                    subscription_id=sub.id,
                    device_id=device.id,
                    completed_at=stamp,
                )
            )
        s.flush()
        stats = DeviceModeration(f.admin.commerce).state(
            s, s.get(Account, did), datetime(2031, 2, 1, 0, 0, tzinfo=UTC)
        )
        assert stats["month"] == "2031-02" and stats["unlink_month"] == 1 and stats["unlink_total"] == 2


def test_fixed_device_charge_and_undiscounted_monthly_renewal(bot_admin_v7):
    from decimal import Decimal

    from sunrise_infrastructure.db import Segment

    from tests.legal_fixture import accept_test_legal

    f, aid, _ = bot_admin_v7
    c = f.admin.commerce
    accept_test_legal(c, aid)
    with f.db.transaction() as s:
        p = Product(
            currency="RUB",
            code=uuid4().hex,
            name="Тест цены",
            tier_level=1,
            price=100,
            included_devices=4,
            extra_device_price=50,
            presentation={"extra_device_rules": [{"through_extra": None, "price": "999.00"}]},
        )
        s.add(p)
        s.flush()
        pid = p.id
    q = c.quote(aid, pid, 90, "purchase")
    o = c.create_order(aid, q["id"], uuid4().hex)
    c.test_pay(aid, o["id"])
    sid = c.order(aid, o["id"])["subscription_id"]
    with f.db.transaction() as s:
        old = s.get(Subscription, sid).expires_at
        segment = s.scalar(select(Segment).where(Segment.subscription_id == sid))
        segment.starts_at = datetime.now(UTC) - timedelta(days=45)
        segment.ends_at = datetime.now(UTC) + timedelta(days=45)
        s.get(Subscription, sid).expires_at = segment.ends_at
        old = segment.ends_at
    q = c.quote(aid, pid, 90, "devices", sid, extra_devices=1)
    assert q["total"] == "50.00" and q["discount_amount"] == "0.00"
    o = c.create_order(aid, q["id"], uuid4().hex)
    c.test_pay(aid, o["id"])
    with f.db.transaction() as s:
        assert s.get(Subscription, sid).expires_at == old
    assert Decimal(c.quote(aid, pid, 90, "renew", sid)["total"]) == Decimal("420.00")
    assert Decimal(c.quote(aid, pid, 365, "renew", sid)["total"]) == Decimal("1560.00")


def test_fixed_devices_extend_across_already_paid_future_segments(bot_admin_v7):
    from decimal import Decimal

    from sunrise_infrastructure.db import Segment

    from tests.legal_fixture import accept_test_legal

    f, aid, _ = bot_admin_v7
    c = f.admin.commerce
    accept_test_legal(c, aid)
    with f.db.transaction() as s:
        p = Product(
            currency="RUB",
            code=uuid4().hex,
            name="Тест будущих периодов",
            tier_level=1,
            price=100,
            included_devices=4,
            extra_device_price=50,
        )
        s.add(p)
        s.flush()
        pid = p.id
    q = c.quote(aid, pid, 30, "purchase")
    o = c.create_order(aid, q["id"], uuid4().hex)
    c.test_pay(aid, o["id"])
    sid = c.order(aid, o["id"])["subscription_id"]
    q = c.quote(aid, pid, 30, "renew", sid)
    o = c.create_order(aid, q["id"], uuid4().hex)
    c.test_pay(aid, o["id"])
    with f.db.transaction() as s:
        before = sum(x.paid_value for x in s.scalars(select(Segment).where(Segment.subscription_id == sid)))
    q = c.quote(aid, pid, 30, "devices", sid, extra_devices=1)
    assert q["total"] == "50.00"
    o = c.create_order(aid, q["id"], uuid4().hex)
    c.test_pay(aid, o["id"])
    with f.db.transaction() as s:
        segments = list(s.scalars(select(Segment).where(Segment.subscription_id == sid)))
        assert all(x.devices == 5 for x in segments)
        assert sum(x.paid_value for x in segments) - before == Decimal("50.00")


@pytest.mark.parametrize("initially_present", [True, False])
def test_panel_unlink_job_counts_only_observed_success_once(bot_admin_v7, initially_present):
    import httpx

    from tests.contract.test_test_integrations import BINDING, panel, user
    from tests.legal_fixture import accept_test_legal

    f, aid, did = bot_admin_v7
    c = f.admin.commerce
    with f.db.transaction() as s:
        f.settings.owner_id = str(s.get(Account, aid).telegram_id)
    c.owner_id = f.settings.owner_id
    state = {"present": initially_present, "deletes": 0}

    def handler(request):
        if request.method == "POST":
            state.update(present=False, deletes=state["deletes"] + 1)
        return httpx.Response(
            200,
            json={
                "response": {"devices": [{"hwid": "synthetic_hwid"}] if state["present"] else []}
                if "hwid" in request.url.path
                else user()
            },
        )

    with f.db.transaction() as s:
        sub = Subscription(
            devices_limit=4,
            account_id=did,
            panel_uuid=BINDING["uuid"],
            short_uuid=BINDING["short_uuid"],
            environment="local_test",
        )
        s.add(sub)
        s.flush()
        device = Device(subscription_id=sub.id, name="Синтетический клиент")
        s.add(device)
        s.flush()
        sid, device_id = sub.id, device.id
    c.panel_provider = panel(handler, bindings={sid: BINDING})
    c.device_bindings = {device_id: "synthetic_hwid"}
    accept_test_legal(c, did)
    assert c.revoke_device(did, sid, device_id)["status"] == "revoke_pending"
    assert c.run_jobs() == int(initially_present)
    assert c.run_jobs() == 0
    with f.db.transaction() as s:
        job = s.scalar(select(Job).where(Job.key == "device-revoke:" + device_id))
        assert job.state == ("done" if initially_present else "review")
    assert f.bot_user_detail(aid, did)["moderation"]["unlink_total"] == int(initially_present)
    assert state["deletes"] == int(initially_present)


def test_alerts_are_owner_only_allowlisted_and_not_generic_mail_jobs(bot_admin_v7):
    f, aid, did = bot_admin_v7
    with f.db.transaction() as s:
        f.settings.owner_id = str(s.get(Account, aid).telegram_id)
        f.admin.commerce.owner_id = f.settings.owner_id
        job = Job(
            key="alert:" + uuid4().hex,
            kind="owner_device_unlink",
            account_id=did,
            payload={"target_id": f.settings.owner_id},
        )
        s.add(job)
        s.flush()
        jid = job.id
    f.admin.commerce.run_jobs()
    with f.db.transaction() as s:
        assert s.get(Job, jid).state == "pending"
    assert f.pending_owner_alerts(aid)["items"][0]["id"] == jid
    with pytest.raises(DomainError) as error:
        f.pending_owner_alerts(did)
    assert error.value.status == 404
    assert f.claim_owner_alert(aid, jid) is not None
    assert f.acknowledge_owner_alert(aid, jid)["state"] == "done"
    assert f.pending_owner_alerts(aid)["items"] == []


def test_configured_telegram_owner_does_not_require_email_and_revocation_removes_access(bot_admin_v7):
    f, aid, did = bot_admin_v7
    with f.db.transaction() as s:
        owner = s.get(Account, aid)
        f.settings.owner_id = str(owner.telegram_id)
        owner.email_verified = False
        job = Job(
            key="unverified-owner-alert:" + uuid4().hex,
            kind="owner_device_unlink",
            account_id=did,
            payload={"target_id": f.settings.owner_id},
        )
        s.add(job)
        s.flush()
        jid = job.id
    assert f.pending_owner_alerts(aid)["items"][0]["id"] == jid
    assert f.claim_owner_alert(aid, jid) is not None
    assert f.acknowledge_owner_alert(aid, jid)["state"] == "done"
    assert f.bot_user_detail(aid, did)["can_moderate"] is True
    assert f.bot_user_moderate(
        aid, did, "block", "synthetic Telegram owner moderation", "telegram-owner-block", confirmed=True
    )["unlink_blocked"] is True
    f.settings.owner_id = None
    f.settings.admin_telegram_allowlist = []
    with pytest.raises(DomainError) as error:
        f.bot_user_moderate(aid, did, "block", "Не подтверждена почта", "unverified-block", confirmed=True)
    assert error.value.status == 404
    with pytest.raises(DomainError) as error:
        f.bot_user_detail(aid, did)
    assert error.value.status == 404


def test_web_user_detail_pages_each_history_and_authorizes_owner_controls(bot_admin_v7):
    f, aid, did = bot_admin_v7
    with f.db.transaction() as s:
        f.settings.owner_id = str(s.get(Account, aid).telegram_id)
        s.add_all(
            [
                Subscription(devices_limit=4, account_id=did, name="Первый"),
                Subscription(devices_limit=4, account_id=did, name="Второй"),
            ]
        )
    first = f.admin.user_detail(did, aid, f.settings, page=1, page_size=1)
    second = f.admin.user_detail(did, aid, f.settings, page=2, page_size=1)
    assert first["can_moderate"] is True
    assert len(first["subscriptions"]) == len(second["subscriptions"]) == 1
    assert first["subscriptions"][0]["id"] != second["subscriptions"][0]["id"]
    assert first["pagination"]["totals"]["subscriptions"] == 2
    with pytest.raises(DomainError):
        f.admin.user_detail(did, page_size=501)


def test_block_during_panel_preflight_prevents_actual_unlink_and_unblock_resumes(bot_admin_v7):
    import httpx

    from tests.contract.test_test_integrations import BINDING, panel, user
    from tests.legal_fixture import accept_test_legal

    f, aid, did = bot_admin_v7
    c = f.admin.commerce
    with f.db.transaction() as s:
        f.settings.owner_id = str(s.get(Account, aid).telegram_id)
        c.owner_id = f.settings.owner_id
        sub = Subscription(
            devices_limit=4, account_id=did, panel_uuid=BINDING["uuid"], short_uuid=BINDING["short_uuid"]
        )
        s.add(sub)
        s.flush()
        device = Device(subscription_id=sub.id, name="Тест гонки")
        s.add(device)
        s.flush()
        sid, device_id = sub.id, device.id
    state = {"block": True, "present": True, "deletes": 0}

    def handler(request):
        if request.method == "POST":
            state.update(present=False, deletes=state["deletes"] + 1)
        if "hwid" in request.url.path:
            if state["block"]:
                state["block"] = False
                f.bot_user_moderate(aid, did, "block", "Проверка гонки", "preflight-block", confirmed=True)
            return httpx.Response(
                200, json={"response": {"devices": [{"hwid": "synthetic_hwid"}] if state["present"] else []}}
            )
        return httpx.Response(200, json={"response": user()})

    c.panel_provider = panel(handler, bindings={sid: BINDING})
    c.device_bindings = {device_id: "synthetic_hwid"}
    accept_test_legal(c, did)
    c.revoke_device(did, sid, device_id)
    assert c.run_jobs() == 0
    assert state["deletes"] == 0
    assert f.bot_user_detail(aid, did)["moderation"]["unlink_total"] == 0
    f.bot_user_moderate(aid, did, "unblock", "Продолжить операцию", "preflight-unblock", confirmed=True)
    assert c.run_jobs() == 1
    assert state["deletes"] == 1
    assert f.bot_user_detail(aid, did)["moderation"]["unlink_total"] == 1


def test_independent_legal_patch_keeps_concurrently_changed_other_document(bot_admin_v7):
    f, aid, _ = bot_admin_v7
    legal = f.bot_settings(aid)["legal"]
    privacy = next(d for d in legal["documents"] if d["document_type"] == "privacy") | {
        "version": "privacy-" + uuid4().hex
    }
    offer = next(d for d in legal["documents"] if d["document_type"] == "offer") | {
        "version": "offer-" + uuid4().hex
    }
    f.bot_settings_update(
        aid,
        "legal",
        {"documents": [offer, next(d for d in legal["documents"] if d["document_type"] == "privacy")]},
        "Оферта изменена",
        confirmed=True,
    )
    saved = f.bot_settings_update(
        aid, "legal", {"documents": [privacy], "partial": True}, "Политика изменена", confirmed=True
    )
    assert next(d for d in saved["documents"] if d["document_type"] == "offer")["version"] == offer["version"]
    assert (
        next(d for d in saved["documents"] if d["document_type"] == "privacy")["version"]
        == privacy["version"]
    )
