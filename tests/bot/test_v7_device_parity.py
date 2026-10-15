"""Real dispatcher flows with synthetic panel-service boundaries, never live IO."""

import asyncio
from copy import deepcopy
from html import unescape

import pytest
from aiogram import Bot
from sunrise_bot.adapter import BotApplication
from sunrise_bot.safety import BotSafety
from sunrise_core.errors import DomainError
from test_dispatcher import FakeCommerce, callback_update, find_button, message_update
from test_presentation import TOKEN, FakeSession

from tests.integration.test_panel_28_workflows import panel28_workflow  # noqa: F401 - fixture

AID = "9223372036854775700"
SID = "9007199254740997"


class PanelOperations:
    def __init__(self):
        self.calls = []
        self.results = {}
        self.snapshot = {
            "subscription_id": SID,
            "source": "remnawave",
            "error": None,
            "observed_at": "2026-10-09T10:00:00Z",
            "traffic_bytes": None,
            "devices": [
                {"id": "d1", "name": "Телефон <новый>", "generation": 3, "revoked": False},
                {"id": "d2", "name": "Планшет", "generation": 1, "revoked": False},
            ],
        }

    def inventory(self, aid, sid, refresh=False):
        assert (aid, sid) == (AID, SID)
        self.calls.append(("inventory", aid, sid, refresh))
        return deepcopy(self.snapshot)

    def revoke_devices(
        self, aid, sid, ids, key, confirmed=False, *, expected_generations=None, operation_mode=None
    ):
        assert confirmed is True
        assert operation_mode in {"single", "bulk"}
        assert expected_generations == {
            device["id"]: device["generation"] for device in self.snapshot["devices"] if device["id"] in ids
        }
        self.calls.append(("revoke", aid, sid, ids, key))
        return self.results.setdefault(key, {"status": "revoke_pending", "device_ids": ids})

    def rotate_credentials(self, aid, sid, key, confirmed=False):
        assert confirmed is True
        self.calls.append(("rotate", aid, sid, key))
        return self.results.setdefault(key, {"status": "rotate_pending"})


def fixture_app(operations=True):
    commerce, session = FakeCommerce(), FakeSession()
    app = BotApplication(
        Bot(TOKEN, session=session), commerce, BotSafety(frozenset({"700001"}), frozenset({"101", "102"}))
    )
    panel = PanelOperations() if operations else None
    app.customer_operations = panel
    return app, commerce, session, panel


async def click(app, session, label, uid=101):
    await app.dispatcher.feed_update(app.bot, callback_update(find_button(session, label), uid=uid))


async def devices(app, session):
    await app.dispatcher.feed_update(app.bot, message_update())
    await click(app, session, "Подписки")
    await click(app, session, "Устройства")


def text(session):
    return unescape(session.calls[-1].text)


def buttons(session):
    return [b for row in session.calls[-1].reply_markup.inline_keyboard for b in row]


def test_devices_use_owned_inventory_and_unknown_traffic_not_cached_names():
    async def run():
        app, _, session, _ = fixture_app()
        await devices(app, session)
        assert "Телефон &lt;новый&gt;" in session.calls[-1].text
        assert "Ноутбук" not in text(session)
        assert "неизвест" in text(session).lower() or "недоступ" in text(session).lower()
        refresh = find_button(session, "Обновить список")
        assert refresh.startswith("s:") and "d1" not in refresh
        await click(app, session, "Обновить список")
        assert app.customer_operations.calls[-1] == ("inventory", AID, SID, True)

    asyncio.run(run())


@pytest.mark.parametrize("bulk", [False, True])
def test_confirmed_unlink_is_owner_bound_idempotent_and_truthfully_queued(bulk):
    async def run():
        app, _, session, panel = fixture_app()
        await devices(app, session)
        await click(app, session, "Отключить все" if bulk else "Отключить: Телефон")
        assert not panel.results
        key = find_button(session, "Подтвердить отключение")
        await app.dispatcher.feed_update(app.bot, callback_update(key, uid=102))
        assert not panel.results
        for _ in range(2):
            await app.dispatcher.feed_update(app.bot, callback_update(key))
        calls = [item for item in panel.calls if item[0] == "revoke"]
        assert len(calls) == 2 and calls[0] == calls[1]
        assert calls[0][1:4] == (AID, SID, ["d1", "d2"] if bulk else ["d1"])
        assert len(panel.results) == 1
        assert "ожида" in text(session).lower()
        assert "устройства отключены" not in text(session).lower()

    asyncio.run(run())


def test_generation_change_after_confirmation_requires_new_selection():
    async def run():
        app, _, session, panel = fixture_app()
        await devices(app, session)
        await click(app, session, "Отключить: Телефон")
        key = find_button(session, "Подтвердить отключение")
        panel.snapshot["devices"][0]["generation"] = 4
        await app.dispatcher.feed_update(app.bot, callback_update(key))
        assert not panel.results
        assert "Действие не выполнено" in text(session)

    asyncio.run(run())


def test_rotate_has_explicit_warning_and_never_claims_completion_on_queue():
    async def run():
        app, _, session, panel = fixture_app()
        await devices(app, session)
        await click(app, session, "Перевыпустить ссылку")
        assert "старая" in text(session).lower() and "срок" in text(session).lower()
        assert not panel.results
        key = find_button(session, "Подтвердить перевыпуск")
        for _ in range(2):
            await app.dispatcher.feed_update(app.bot, callback_update(key))
        calls = [item for item in panel.calls if item[0] == "rotate"]
        assert len(calls) == 2 and calls[0] == calls[1]
        assert "ожида" in text(session).lower() and "готова" not in text(session).lower()
        assert not any(b.url for b in buttons(session))

    asyncio.run(run())


@pytest.mark.parametrize("reason", ["missing", "snapshot_error", "rotation"])
def test_unavailable_inventory_or_rotation_does_not_offer_destructive_actions(reason):
    async def run():
        app, commerce, session, panel = fixture_app(reason != "missing")
        if reason == "snapshot_error":
            panel.snapshot.update(error="panel_snapshot_unavailable", source="unavailable", devices=[])
        if reason == "rotation":
            commerce.subscriptions[0]["credential_rotation_state"] = "review"
        await devices(app, session)
        assert not any("Отключить:" in b.text or "Отключить все" in b.text for b in buttons(session))
        if reason != "missing":
            assert not any("Перевыпустить" in b.text for b in buttons(session))
        assert "Подключённых устройств пока нет" not in text(session)

    asyncio.run(run())


def test_direct_old_revoke_without_service_fails_closed():
    async def run():
        app, commerce, session, _ = fixture_app(False)
        key = app.button("101", "old", "revoke", sid=SID, device_id="d1").callback_data
        await app.dispatcher.feed_update(app.bot, callback_update(key))
        assert not any(item[0] == "revoke" for item in commerce.calls)
        assert "Действие не выполнено" in text(session)

    asyncio.run(run())


def test_common_service_domain_denial_is_shown_without_retry_or_false_success():
    async def run():
        app, _, session, panel = fixture_app()
        await devices(app, session)
        await click(app, session, "Отключить все")

        def denied(*args, **kwargs):
            raise DomainError("unlink_blocked", "Отвязка временно запрещена.", 409)

        panel.revoke_devices = denied
        await click(app, session, "Подтвердить отключение")
        assert "Отвязка временно запрещена" in text(session)
        assert not panel.results

    asyncio.run(run())


def test_unavailable_panel_after_self_confirmation_never_submits_unlink_or_asks_owner_approval():
    async def run():
        app, _, session, panel = fixture_app()
        await devices(app, session)
        await click(app, session, "Отключить: Телефон")
        panel.snapshot.update(source="unavailable", error="panel_unavailable")
        await click(app, session, "Подтвердить отключение")
        assert not any(call[0] == "revoke" for call in panel.calls)
        assert "Панель временно недоступна" in text(session)
        assert "Отвязка не отправлена" in text(session)
        assert "одобр" not in text(session).casefold()

    asyncio.run(run())


def test_missing_current_rank_never_invents_upgrade_or_downgrade():
    async def run():
        app, commerce, session, _ = fixture_app()
        commerce.subscriptions[0]["product_id"] = "hidden-product"
        await app.dispatcher.feed_update(app.bot, message_update())
        await click(app, session, "Подписки")
        assert not any("Повысить тариф" in b.text or "Понизить тариф" in b.text for b in buttons(session))

    asyncio.run(run())


def test_old_upgrade_callback_cannot_offer_tariffs_without_known_current_rank():
    async def run():
        app, commerce, session, _ = fixture_app()
        commerce.subscriptions[0]["product_id"] = "hidden-product"
        await app.execute("101", AID, "products", {"sid": SID, "intent": "upgrade"}, None, "")
        assert not any(
            b.callback_data and app.actions[b.callback_data].action == "periods" for b in buttons(session)
        )

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["individual", "bulk", "rotation"])
def test_real_panel28_dispatcher_queues_once_then_preserves_paid_rights(
    panel28_workflow,  # noqa: F811 - imported fixture
    kind,
    record_testsuite_property,
):
    from sqlalchemy import select
    from sunrise_infrastructure.db import DeviceUnlink, Job, Subscription

    async def run():
        db, _, commerce, events, operations, state, sid, _ = panel28_workflow
        events.reconcile_owned(sid, initial=True)
        session = FakeSession()
        telegram = "70000000000000001"
        app = BotApplication(
            Bot(TOKEN, session=session),
            commerce,
            BotSafety(frozenset({"700001"}), frozenset({telegram})),
            customer_operations=operations,
        )
        with db.transaction() as tx:
            previous_expiry = tx.get(Subscription, sid).expires_at
            previous_url = tx.get(Subscription, sid).connection_url
            owned_uuid = tx.get(Subscription, sid).panel_uuid
        provider_before = deepcopy((state["user"], state["devices"]))
        method = "rotate_credentials" if kind == "rotation" else "revoke_devices"
        original = getattr(operations, method)
        queue_calls = []

        def queue_without_provider_io(*args, **kwargs):
            count = len(state["calls"])
            try:
                return original(*args, **kwargs)
            finally:
                assert len(state["calls"]) == count
                queue_calls.append(method)

        setattr(operations, method, queue_without_provider_io)
        await app.execute(telegram, "301", "devices", {"sid": sid}, None, "")
        label = (
            "Перевыпустить ссылку"
            if kind == "rotation"
            else "Отключить все"
            if kind == "bulk"
            else "Отключить:"
        )
        await app.dispatcher.feed_update(
            app.bot, callback_update(find_button(session, label), uid=int(telegram))
        )
        key = find_button(
            session, "Подтвердить перевыпуск" if kind == "rotation" else "Подтвердить отключение"
        )
        before = len(state["calls"])
        for _ in range(2):
            await app.dispatcher.feed_update(app.bot, callback_update(key, uid=int(telegram)))
        reads = state["calls"][before:]
        record_testsuite_property(
            "confirmation_provider_calls_" + kind,
            [(method, path.replace(owned_uuid, "<owned_uuid>")) for method, path in reads],
        )
        assert reads == []
        assert len(queue_calls) == 2
        assert (state["user"], state["devices"]) == provider_before
        with db.transaction() as tx:
            queued = list(tx.scalars(select(Job).where(Job.state == "pending")))
            assert len(queued) == (2 if kind == "bulk" else 1)
            assert tx.get(Subscription, sid).expires_at == previous_expiry
            assert not list(tx.scalars(select(DeviceUnlink)))
        assert "Ожидается" in text(session)
        worker_before = len(state["calls"])
        assert commerce.provisioning.run_jobs() == len(queued)
        worker_calls = state["calls"][worker_before:]
        assert worker_calls[0] == ("GET", "/api/users/" + owned_uuid)
        write_indices = [i for i, (method, _) in enumerate(worker_calls) if method == "POST"]
        assert write_indices
        previous_write = -1
        for index in write_indices:
            preflight = worker_calls[previous_write + 1 : index]
            assert ("GET", "/api/users/" + owned_uuid) in preflight
            if kind != "rotation":
                assert ("GET", "/api/hwid/devices/" + owned_uuid) in preflight
            previous_write = index
        allowed_paths = {
            "/api/users/" + owned_uuid,
            "/api/hwid/devices/" + owned_uuid,
            "/api/hwid/devices/delete",
            "/api/users/" + owned_uuid + "/actions/revoke",
        }
        assert all(method in {"GET", "POST"} and path in allowed_paths for method, path in worker_calls)
        assert sum(method == "POST" for method, _ in worker_calls) == len(queued)
        record_testsuite_property(
            "worker_provider_calls_" + kind,
            [(method, path.replace(owned_uuid, "<owned_uuid>")) for method, path in worker_calls],
        )
        with db.transaction() as tx:
            assert tx.get(Subscription, sid).expires_at == previous_expiry
            assert all(tx.get(Job, job.id).state == "done" for job in queued)
            assert len(list(tx.scalars(select(DeviceUnlink)))) == (0 if kind == "rotation" else len(queued))
            if kind == "rotation":
                assert tx.get(Subscription, sid).connection_url != previous_url
        await app.dispatcher.feed_update(
            app.bot, callback_update(find_button(session, "Обновить статус"), uid=int(telegram))
        )
        assert "ожидается подтверждение" not in text(session).lower()

    asyncio.run(run())


def test_generation_race_between_bot_validation_and_locked_queue_never_targets_new_device(
    panel28_workflow,  # noqa: F811 - imported fixture
    record_testsuite_property,
):
    from sqlalchemy import select
    from sunrise_infrastructure.db import Device, DeviceUnlink, Job, Subscription

    async def run():
        db, _, commerce, events, operations, state, sid, _ = panel28_workflow
        events.reconcile_owned(sid, initial=True)
        session = FakeSession()
        telegram = "70000000000000001"
        app = BotApplication(
            Bot(TOKEN, session=session),
            commerce,
            BotSafety(frozenset({"700001"}), frozenset({telegram})),
            customer_operations=operations,
        )
        await app.execute(telegram, "301", "devices", {"sid": sid}, None, "")
        await app.dispatcher.feed_update(
            app.bot, callback_update(find_button(session, "Отключить:"), uid=int(telegram))
        )
        key = find_button(session, "Подтвердить отключение")
        original = operations.revoke_devices
        with db.transaction() as tx:
            expiry = tx.get(Subscription, sid).expires_at
            owned_uuid = tx.get(Subscription, sid).panel_uuid
        provider_before = deepcopy((state["user"], state["devices"]))
        queue_calls = []

        def race(*args, **kwargs):
            # Adapter already validated generation1; before the service locks generation2 appears.
            with db.transaction() as tx:
                device = tx.get(Device, args[2][0])
                assert device.generation == 1
                device.generation = 2
            before_queue = len(state["calls"])
            try:
                return original(*args, **kwargs)
            finally:
                assert len(state["calls"]) == before_queue
                queue_calls.append("revoke_devices")

        operations.revoke_devices = race
        before = len(state["calls"])
        await app.dispatcher.feed_update(app.bot, callback_update(key, uid=int(telegram)))
        with db.transaction() as tx:
            assert not list(tx.scalars(select(Job).where(Job.kind == "device_revoke")))
            assert not list(tx.scalars(select(DeviceUnlink)))
            assert tx.get(Subscription, sid).expires_at == expiry
        reads = state["calls"][before:]
        record_testsuite_property(
            "confirmation_provider_calls_generation_race",
            [(method, path.replace(owned_uuid, "<owned_uuid>")) for method, path in reads],
        )
        assert reads == []
        assert queue_calls == ["revoke_devices"]
        assert (state["user"], state["devices"]) == provider_before
        assert "Действие не выполнено" in text(session)
        assert "изменился" in text(session)

    asyncio.run(run())


def test_history_uses_moscow_snapshot_details_and_paired_navigation_beyond_twenty():
    async def run():
        app, commerce, session, _ = fixture_app()
        dashboard = commerce.dashboard
        payments = [
            {
                "id": str(i),
                "amount": "12.34",
                "currency": "EUR",
                "fee": None,
                "status": "paid",
                "paid_at": "2026-10-09T23:30:00Z",
                "product_name": f"Тариф <{i}>",
                "action": "devices",
                "devices": 7,
                "period_days": 31,
                "payment_method": "bank_card",
                "provisioning_status": "active",
            }
            for i in range(22)
        ]
        commerce.dashboard = lambda aid: dashboard(aid) | {"payments": payments}
        await app.execute("101", AID, "history", {"page": 20}, None, "")
        assert "Тариф &lt;20&gt;" in session.calls[-1].text
        assert "10.10.2026 02:30 МСК" in text(session)
        assert "2026-10-09T23:30" not in text(session)
        assert "7" in text(session) and "устройств" in text(session).lower()
        row = next(
            row
            for row in session.calls[-1].reply_markup.inline_keyboard
            if len(row) == 2
            and all(b.callback_data and app.actions[b.callback_data].action == "history" for b in row)
        )
        assert len(row) == 2
        await click(app, session, "Далее")
        assert "Тариф &lt;21&gt;" in session.calls[-1].text
        assert len(session.calls[-1].text) < 4096

    asyncio.run(run())
