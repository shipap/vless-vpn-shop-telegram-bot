from types import SimpleNamespace

import pytest
from sunrise_infrastructure.broadcasts import BroadcastService
from sunrise_infrastructure.db import Account, Alias


@pytest.mark.parametrize("production", [False, True])
def test_broadcast_expansion_keeps_frozen_recipients_without_live_legal_gate(production):
    from contextlib import contextmanager

    from sunrise_infrastructure.db import Job, Notification, NotificationDelivery

    policy = SimpleNamespace(
        production=production,
        scoped_payments=False,
        scope=SimpleNamespace(scope_id="synthetic-project"),
        require_current=lambda *args: None,
    )
    config = SimpleNamespace(
        _runtime_policy=policy,
        test_telegram_recipients="123456,123457",
        email_allowlist=[],
        admin_telegram_allowlist=["654321"],
    )
    actor = Account(id=1, telegram_id=654321)
    selected = Account(id=9007199254740993, telegram_id=123456, telegram_notifications_blocked=False)
    later = Account(id=9007199254740994, telegram_id=123457, telegram_notifications_blocked=False)
    job = Job(
        id="synthetic-job",
        account_id=actor.id,
        kind="broadcast",
        state="pending",
        attempts=0,
        payload={
            "source": "unified",
            "environment": "live" if production else "local_test",
            "scope_id": "synthetic-project",
            "account_ids": [str(selected.id)],
            "nodes": [{"type": "text", "text": "Synthetic broadcast"}],
        },
    )
    rows = {(Account, account.id): account for account in (actor, selected, later)}
    added = []

    def flush():
        for row in added:
            if isinstance(row, Notification):
                row.id = "synthetic-notice"

    tx = SimpleNamespace(
        get=lambda model, key, **_: rows.get((model, key)),
        scalars=lambda _: [job],
        execute=lambda *args: None,
        scalar=lambda _: None,
        add=added.append,
        flush=flush,
    )

    @contextmanager
    def transaction():
        yield tx

    commerce = SimpleNamespace(
        db=SimpleNamespace(transaction=transaction),
        accounts=None,
        mail_transport=None,
        runtime_policy=policy,
        legal=SimpleNamespace(status_in=lambda *args: {"accepted": False}),
    )
    service = BroadcastService(commerce, config)
    assert service.expand_pending() == 1
    assert job.state == "done"
    assert job.payload["account_ids"] == ["9007199254740993"]
    assert job.payload["blocked_before_enqueue"] == (0 if production else 1)
    assert [row.account_id for row in added if isinstance(row, Notification)] == (
        [9007199254740993] if production else []
    )
    assert [row.channel for row in added if isinstance(row, NotificationDelivery)] == (
        ["telegram"] if production else []
    )
    assert all(isinstance(row, (Notification, NotificationDelivery)) for row in added)


@pytest.mark.parametrize("production", [False, True])
@pytest.mark.parametrize("channel", ["telegram", "email"])
def test_broadcast_claim_requires_legal_only_outside_production(monkeypatch, production, channel):
    from contextlib import contextmanager

    from sunrise_infrastructure.db import Notification, NotificationDelivery
    from sunrise_infrastructure.legal import Legal
    from sunrise_infrastructure.notifications import NotificationService

    policy = SimpleNamespace(
        production=production, scoped_payments=False, require=lambda: None, require_current=lambda *args: None
    )
    config = SimpleNamespace(
        _runtime_policy=policy,
        test_telegram_recipients="123456",
        email_allowlist=["normal@synthetic.test"],
        admin_telegram_allowlist=[],
    )
    account = Account(
        id=9007199254740993,
        telegram_id=123456 if channel == "telegram" else None,
        telegram_notifications_blocked=False,
        email="normal@synthetic.test",
        email_verified=True,
    )
    item = Notification(
        id="synthetic-notice",
        account_id=account.id,
        kind="broadcast",
        audience="account",
        event_key="broadcast:synthetic-job",
        title="Synthetic",
        body="Synthetic body",
        nodes=[],
        actions=[],
    )
    delivery = NotificationDelivery(
        id="synthetic-delivery",
        notification_id=item.id,
        state="pending",
        attempts=0,
        channel=channel,
        recipient="123456" if channel == "telegram" else account.email,
    )
    rows = {(Account, account.id): account, (Notification, item.id): item}
    claims = iter([account, delivery, account, delivery])
    tx = SimpleNamespace(
        get=lambda model, key, **_: rows.get((model, key)),
        scalar=lambda _: next(claims),
        execute=lambda *args: SimpleNamespace(
            first=lambda: SimpleNamespace(id=delivery.id, account_id=account.id)
        ),
    )

    @contextmanager
    def transaction():
        yield tx

    monkeypatch.setattr(Legal, "status_in", lambda *args: {"accepted": False})
    monkeypatch.setattr(
        "sunrise_infrastructure.broadcast_media_registry.notification_media", lambda *args: None
    )
    service = NotificationService(SimpleNamespace(transaction=transaction), config)
    monkeypatch.setattr(service, "_recover_claims", lambda **kwargs: None)
    transported = []
    # This test stops at the transport boundary; final authority is checked separately below.
    monkeypatch.setattr(service, "_deliver", lambda *args: transported.append(args))
    assert service.deliver_pending(limit=1) == (1 if production else 0)
    assert delivery.state == ("delivered" if production else "blocked")
    assert len(transported) == (1 if production else 0)
    if not production:
        assert delivery.last_error == "legal_consent_required"


@pytest.mark.parametrize("media", [False, True])
@pytest.mark.parametrize(
    "revoke",
    [
        "staff_telegram",
        "stopped_job",
        "recipient_alias",
        "blocked",
        "scope",
        "snapshot",
        "gate",
        "legal",
        "never_accepted",
    ],
)
def test_live_broadcast_revalidates_contact_and_actor_without_legal_gate(monkeypatch, revoke, media):
    from contextlib import contextmanager

    import httpx
    from sunrise_core.errors import DomainError
    from sunrise_infrastructure import notifications
    from sunrise_infrastructure.db import Job, Notification, NotificationDelivery
    from sunrise_infrastructure.legal import Legal

    gate = [True]

    def require_current(*args):
        if not gate[0]:
            raise DomainError("live_gate_closed", "Synthetic gate closed", 403)

    policy = SimpleNamespace(
        production=True,
        scoped_payments=False,
        scope=SimpleNamespace(
            scope_id="synthetic-project",
            bot_id="100001",
            rollypay=SimpleNamespace(checkout_hosts=["pay.synthetic.test"]),
        ),
        require_current=require_current,
    )
    config = SimpleNamespace(
        runtime_mode="production",
        _runtime_policy=policy,
        bot_token="100001:synthetic-token-value-123456789",  # noqa: S106 -- synthetic transport only
        admin_email_allowlist=["staff@synthetic.test"],
        admin_telegram_allowlist=["654321"],
        owner_id=None,
        test_telegram_recipients="",
        email_allowlist=[],
        dashboard_url="",
    )
    actor = Account(id=1, telegram_id=654321, email="staff@synthetic.test", email_verified=True)
    recipient = Account(id=9007199254740993, telegram_id=123456, telegram_notifications_blocked=False)
    job = Job(
        id="synthetic-broadcast",
        account_id=actor.id,
        kind="broadcast",
        state="done",
        payload={
            "environment": "live",
            "scope_id": "synthetic-project",
            "source": "unified",
            "account_ids": [str(recipient.id)],
        },
    )
    item = Notification(
        id="synthetic-notice",
        account_id=recipient.id,
        event_key="broadcast:" + job.id,
        kind="broadcast",
        audience="account",
    )
    delivery = NotificationDelivery(
        id="synthetic-delivery",
        notification_id=item.id,
        channel="telegram",
        recipient=str(recipient.telegram_id),
        state="in_flight",
    )
    rows = {
        (Account, actor.id): actor,
        (Account, recipient.id): recipient,
        (Job, job.id): job,
        (Notification, item.id): item,
        (NotificationDelivery, delivery.id): delivery,
    }
    accepted = [revoke != "never_accepted"]
    tx = SimpleNamespace(get=lambda model, key, **_: rows.get((model, key)))

    @contextmanager
    def transaction():
        yield tx

    monkeypatch.setattr(Legal, "status_in", lambda *args: {"accepted": accepted[0]})
    if media:
        from sunrise_infrastructure import broadcast_media_registry

        reference = {"asset_id": "synthetic-asset", "mime": "image/png"}
        monkeypatch.setattr(broadcast_media_registry, "notification_media", lambda *args: reference)
        monkeypatch.setattr(
            broadcast_media_registry,
            "configured_media_store",
            lambda *args: SimpleNamespace(read=lambda media: b"synthetic-media"),
        )
    calls = []

    def handle(request):
        calls.append(request.method)
        if request.method == "GET":
            if revoke == "staff_telegram":
                actor.telegram_id = None
            elif revoke == "stopped_job":
                job.state = "stopped"
            elif revoke == "recipient_alias":
                rows[(Alias, recipient.id)] = Alias(old_id=recipient.id, canonical_id=actor.id)
            elif revoke == "blocked":
                recipient.telegram_notifications_blocked = True
            elif revoke == "scope":
                job.payload = {**job.payload, "scope_id": "other-synthetic-project"}
            elif revoke == "snapshot":
                job.payload = {**job.payload, "account_ids": []}
            elif revoke == "gate":
                gate[0] = False
            else:
                accepted[0] = False
            return httpx.Response(200, json={"ok": True, "result": {"id": 100001}})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    client = httpx.Client(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(notifications.httpx, "Client", lambda **kwargs: client)
    service = notifications.NotificationService(SimpleNamespace(transaction=transaction), config)
    payload = {
        "notification_id": item.id,
        "audience": "account",
        "title": "Рассылка",
        "body": "Сообщение",
        "nodes": None,
        "actions": [],
        **({"media": reference} if media else {}),
    }
    if revoke in {"legal", "never_accepted"}:
        service._deliver("telegram", str(recipient.telegram_id), payload, delivery.id)
        assert calls == ["GET", "POST"]
    else:
        with pytest.raises(DomainError):
            service._deliver("telegram", str(recipient.telegram_id), payload, delivery.id)
        assert calls == ["GET"]


@pytest.mark.parametrize("accepted", [False, True])
def test_live_broadcast_uses_canonical_contacts_without_legal_or_test_allowlist_gate(accepted):
    policy = SimpleNamespace(production=True, scope=SimpleNamespace(scope_id="synthetic-project"))
    config = SimpleNamespace(
        runtime_mode="production",
        _runtime_policy=policy,
        test_telegram_recipients="",
        email_allowlist=[],
        admin_email_allowlist=[],
    )
    commerce = SimpleNamespace(
        db=None,
        accounts=None,
        mail_transport=None,
        runtime_policy=policy,
        legal=SimpleNamespace(status_in=lambda *args: {"accepted": accepted}),
    )
    service = BroadcastService(commerce, config)
    tg = Account(id=9007199254740993, telegram_id=123456, telegram_notifications_blocked=False)
    email = Account(id=9007199254740994, telegram_id=None, email="normal@synthetic.test", email_verified=True)
    stale = Account(id=9007199254740995, telegram_id=123457, telegram_notifications_blocked=False)
    session = SimpleNamespace(
        scalars=lambda _: [tg, email, stale],
        get=lambda model, key, **_: (
            Alias(old_id=stale.id, canonical_id=tg.id) if model is Alias and key == stale.id else None
        ),
    )
    assert service._allowed(tg) and service._allowed(email)
    assert service.eligible_in(session) == [tg, email]
    tg.telegram_notifications_blocked = True
    tg.email, tg.email_verified = "bound@synthetic.test", True
    email.email_verified = False
    assert not service._allowed(tg) and not service._allowed(email)


@pytest.mark.parametrize("accepted", [False, True])
def test_nonproduction_broadcast_keeps_legal_and_empty_allowlist_guards(accepted):
    config = SimpleNamespace(
        _runtime_policy=None, runtime_mode="local", test_telegram_recipients="", email_allowlist=[]
    )
    commerce = SimpleNamespace(
        db=None,
        accounts=None,
        mail_transport=None,
        runtime_policy=None,
        legal=SimpleNamespace(status_in=lambda *args: {"accepted": accepted}),
    )
    service = BroadcastService(commerce, config)
    account = Account(id=9007199254740993, telegram_id=123456)
    session = SimpleNamespace(scalars=lambda _: [account])
    assert service.eligible_in(session) == ([account] if accepted else [])
    assert not service._allowed(account)


def test_live_email_broadcast_rechecks_telegram_link_before_final_send():
    from contextlib import contextmanager

    from sunrise_core.errors import DomainError
    from sunrise_infrastructure.db import Job, Notification, NotificationDelivery
    from sunrise_infrastructure.notifications import NotificationService

    policy = SimpleNamespace(
        production=True, scoped_payments=False,
        scope=SimpleNamespace(scope_id="synthetic-project"), require_current=lambda *args: None,
    )
    config = SimpleNamespace(
        _runtime_policy=policy, admin_telegram_allowlist=["654321"], owner_id=None,
        email_allowlist=[], test_telegram_recipients="",
    )
    actor = Account(id=1, telegram_id=654321)
    recipient = Account(id=2, email="recipient@synthetic.test", email_verified=True)
    job = Job(id="synthetic-job", account_id=actor.id, kind="broadcast", state="done", payload={
        "environment": "live", "scope_id": "synthetic-project", "source": "unified",
        "account_ids": [str(recipient.id)],
    })
    item = Notification(
        id="synthetic-notice", account_id=recipient.id, event_key="broadcast:" + job.id,
        kind="broadcast", audience="account",
    )
    delivery = NotificationDelivery(
        id="synthetic-delivery", notification_id=item.id, channel="email", recipient=recipient.email,
        state="in_flight",
    )
    rows = {
        (Account, actor.id): actor, (Account, recipient.id): recipient, (Job, job.id): job,
        (Notification, item.id): item, (NotificationDelivery, delivery.id): delivery,
    }

    @contextmanager
    def transaction():
        yield SimpleNamespace(get=lambda model, key, **kwargs: rows.get((model, key)))

    service = NotificationService(SimpleNamespace(transaction=transaction), config)
    payload = {"notification_id": item.id, "audience": "account"}
    assert service._validate_delivery("email", recipient.email, payload, delivery.id) == (None, None)
    # Telegram was linked after the email claim, while the transport was authenticating.
    recipient.telegram_id = 123456
    with pytest.raises(DomainError) as rejected:
        service._validate_delivery("email", recipient.email, payload, delivery.id)
    assert rejected.value.status == 404


def test_final_delivery_denies_broadcast_push_even_for_an_authorized_subscription():
    from contextlib import contextmanager

    from sunrise_core.errors import DomainError
    from sunrise_infrastructure.db import (
        Job,
        Notification,
        NotificationDelivery,
        NotificationPreference,
        PushSubscription,
    )
    from sunrise_infrastructure.notifications import NotificationService

    policy = SimpleNamespace(
        production=True, scoped_payments=False,
        scope=SimpleNamespace(scope_id="synthetic-project"), require_current=lambda *args: None,
    )
    config = SimpleNamespace(_runtime_policy=policy, admin_telegram_allowlist=["654321"], owner_id=None)
    actor = Account(id=1, telegram_id=654321)
    recipient = Account(id=2)
    job = Job(id="synthetic-job", account_id=actor.id, kind="broadcast", state="done", payload={
        "environment": "live", "scope_id": "synthetic-project", "source": "unified", "account_ids": ["2"],
    })
    item = Notification(id="notice", account_id=2, event_key="broadcast:" + job.id, kind="notice", audience="account")
    delivery = NotificationDelivery(
        id="delivery", notification_id=item.id, channel="push", recipient="subscription", state="in_flight",
    )
    rows = {
        (Account, 1): actor, (Account, 2): recipient, (Job, job.id): job,
        (Notification, item.id): item, (NotificationDelivery, delivery.id): delivery,
        (NotificationPreference, 2): NotificationPreference(account_id=2, browser_enabled=True),
        (PushSubscription, "subscription"): PushSubscription(id="subscription", account_id=2, disabled_at=None),
    }

    @contextmanager
    def transaction():
        yield SimpleNamespace(get=lambda model, key, **kwargs: rows.get((model, key)))

    service = NotificationService(SimpleNamespace(transaction=transaction), config)
    payload = {"notification_id": item.id, "audience": "account"}
    assert service._validate_delivery("push", "subscription", payload, delivery.id) == (None, None)
    item.kind = "broadcast"
    with pytest.raises(DomainError) as rejected:
        service._validate_delivery("push", "subscription", payload, delivery.id)
    assert rejected.value.status == 404


@pytest.mark.parametrize("accepted", [False, True])
def test_final_media_self_email_keeps_its_routing_exception_and_legal_gate(monkeypatch, accepted):
    from contextlib import contextmanager

    from sunrise_core.errors import DomainError
    from sunrise_infrastructure import broadcast_media_registry
    from sunrise_infrastructure.db import Job, Notification, NotificationDelivery
    from sunrise_infrastructure.legal import Legal
    from sunrise_infrastructure.notifications import NotificationService

    account = Account(id=1, telegram_id=654321, email="owner@synthetic.test", email_verified=True)
    config = SimpleNamespace(
        _runtime_policy=None, admin_telegram_allowlist=["654321"], owner_id=None,
        email_allowlist=[account.email], test_telegram_recipients="654321",
    )
    media = {"asset_id": "synthetic-image", "mime": "image/png"}
    job = Job(id="job", account_id=1, kind="broadcast", state="done", payload={"source": "media_self"})
    item = Notification(id="notice", account_id=1, event_key="broadcast:job", kind="broadcast", audience="account")
    delivery = NotificationDelivery(
        id="delivery", notification_id=item.id, channel="email", recipient=account.email, state="in_flight",
    )
    rows = {
        (Account, 1): account, (Job, job.id): job,
        (Notification, item.id): item, (NotificationDelivery, delivery.id): delivery,
    }

    @contextmanager
    def transaction():
        yield SimpleNamespace(get=lambda model, key, **kwargs: rows.get((model, key)))

    monkeypatch.setattr(Legal, "status_in", lambda *args: {"accepted": accepted})
    monkeypatch.setattr(broadcast_media_registry, "notification_media", lambda *args: media)
    store = SimpleNamespace(read=lambda reference: b"synthetic-image-bytes" if reference == media else None)
    monkeypatch.setattr(broadcast_media_registry, "configured_media_store", lambda *args: store)
    service = NotificationService(SimpleNamespace(transaction=transaction), config)
    payload = {"notification_id": item.id, "audience": "account", "media": media}
    if accepted:
        assert service._validate_delivery("email", account.email, payload, delivery.id) == (
            store, b"synthetic-image-bytes",
        )
    else:
        with pytest.raises(DomainError) as rejected:
            service._validate_delivery("email", account.email, payload, delivery.id)
        assert rejected.value.status == 404
