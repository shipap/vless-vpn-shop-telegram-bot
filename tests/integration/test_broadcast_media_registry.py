"""A signed asset still requires current project registry and staff authority."""

from io import BytesIO

import pytest
from PIL import Image
from sqlalchemy import select
from sunrise_core.errors import DomainError
from sunrise_infrastructure.broadcasts import BroadcastService
from sunrise_infrastructure.db import Account, Notification, Setting, StaffRole

from tests.integration.test_node_news_and_crm import routed as routed
from tests.integration.test_subscription_engagement import paid_creation as paid_creation
from tests.integration.test_v11_panel_events import owned_panel as owned_panel


def png():
    stream = BytesIO()
    Image.new("RGB", (8, 8), "red").save(stream, format="PNG")
    return stream.getvalue()


def registry(context, tmp_path):
    from sunrise_infrastructure.broadcast_media import BroadcastMediaStore
    from sunrise_infrastructure.broadcast_media_registry import BroadcastMediaRegistry

    return BroadcastMediaRegistry(
        context[2], context[3], store=BroadcastMediaStore(tmp_path, signing_key=b"x" * 32)
    )


def test_registered_media_requires_current_actor_and_exact_reference(routed, tmp_path):
    context, _ = routed
    db, _, _, config, _, _, aid, *_ = context
    service = registry(context, tmp_path)
    ref = service.register(aid, [png()], "image/png")
    assert service.resolve(aid, ref)["bytes"] == png()
    with pytest.raises(DomainError):
        service.resolve(aid, ref | {"size_bytes": ref["size_bytes"] + 1})
    with db.transaction() as session:
        foreign = Account(
            email="foreign-broadcast-staff@example.test",
            email_verified=True,
            telegram_id=int(config.owner_id) + 1,
        )
        session.add(foreign)
        session.flush()
        other_id = foreign.id
        session.add(StaffRole(account_id=other_id, role="admin"))
    config.admin_telegram_allowlist.append(str(int(config.owner_id) + 1))
    with pytest.raises(DomainError):
        service.resolve(other_id, ref)
    with db.transaction() as session:
        session.get(Account, aid).telegram_id = None
    with pytest.raises(DomainError):
        service.resolve(aid, ref)


def test_registry_is_bound_to_current_database_schema(routed, tmp_path):
    context, _ = routed
    service = registry(context, tmp_path)
    ref = service.register(context[6], [png()], "image/png")
    with context[0].transaction() as session:
        row = session.get(Setting, "media_asset:" + ref["asset_id"])
        row.value = row.value | {"scope_sha256": "0" * 64}
    with pytest.raises(DomainError):
        service.resolve(context[6], ref)


def test_media_fanout_is_durable_separate_from_public_content(routed, tmp_path, monkeypatch):
    context, _ = routed
    db, _, commerce, config, _, _, aid, *_ = context
    service = registry(context, tmp_path)
    monkeypatch.setattr(
        "sunrise_infrastructure.broadcast_media_registry.configured_media_store", lambda config: service.store
    )
    ref = service.register(aid, [png()], "image/png")
    broadcasts = BroadcastService(commerce, config)
    nodes = [{"type": "text", "text": "Synthetic media campaign"}]
    assert broadcasts.preview(aid, nodes, media=ref)["recipient_count"] >= 1
    job = broadcasts.create(aid, nodes, "Synthetic media delivery", draft=False, media=ref)
    assert broadcasts.expand_pending() == 1
    assert broadcasts.expand_pending() == 0
    with db.transaction() as session:
        notices = list(
            session.scalars(select(Notification).where(Notification.event_key == "broadcast:" + job["id"]))
        )
        assert len(notices) == 1
        marker = session.get(Setting, "notification_media:" + notices[0].id)
        assert marker.value["media"] == ref and marker.value["job_id"] == job["id"]
        assert "proof" not in str(notices[0].nodes) and "proof" not in str(notices[0].actions)


def test_media_email_self_test_is_one_durable_attachment_and_unknown_not_retried(
    routed, tmp_path, monkeypatch
):
    from sunrise_infrastructure.db import NotificationDelivery
    from sunrise_infrastructure.notifications import NotificationService

    context, _ = routed
    db, _, commerce, config, _, _, aid, *_ = context
    service = registry(context, tmp_path)
    monkeypatch.setattr(
        "sunrise_infrastructure.broadcast_media_registry.configured_media_store", lambda config: service.store
    )
    config.email_allowlist = ["events-owner@example.test"]
    ref = service.register(aid, [png()], "image/png")
    calls = []

    class UnknownMail:
        def deliver(self, recipient, title, body, identifier, *, media=None):
            assert media == ref
            calls.append(identifier)
            raise DomainError(
                "mail_delivery_unknown", "Unknown accepted transport outcome", 503, retryable=False
            )

    with db.transaction() as session:
        for delivery in session.scalars(select(NotificationDelivery)):
            delivery.state = "cancelled"
    result = BroadcastService(commerce, config).test_self(
        aid, [{"type": "text", "text": "Synthetic attachment"}], "Synthetic email self test", media=ref
    )
    notifier = NotificationService(db, config, mail_transport=UnknownMail())
    assert notifier.deliver_pending() == 0
    assert notifier.deliver_pending() == 0
    assert len(calls) == 1
    with db.transaction() as session:
        notice = session.scalar(
            select(Notification).where(Notification.event_key == "broadcast:" + result["id"])
        )
        delivery = session.scalar(
            select(NotificationDelivery).where(NotificationDelivery.notification_id == notice.id)
        )
        assert delivery.channel == "email" and delivery.state == "review"
        assert delivery.last_error == "mail_delivery_unknown"


def test_current_media_staff_revocation_blocks_expansion_and_delivery(routed, tmp_path, monkeypatch):
    from sunrise_infrastructure.db import Job

    context, _ = routed
    db, _, commerce, config, _, _, aid, *_ = context
    service = registry(context, tmp_path)
    monkeypatch.setattr(
        "sunrise_infrastructure.broadcast_media_registry.configured_media_store", lambda config: service.store
    )
    ref = service.register(aid, [png()], "image/png")
    broadcasts = BroadcastService(commerce, config)
    created = broadcasts.create(
        aid,
        [{"type": "text", "text": "Scoped attachment"}],
        "Synthetic registry revoke",
        draft=False,
        media=ref,
    )
    with db.transaction() as session:
        session.get(Account, aid).telegram_id = None
    assert broadcasts.expand_pending() == 0
    with db.transaction() as session:
        assert session.get(Job, created["id"]).state == "review"
        assert (
            session.scalar(
                select(Notification.id).where(Notification.event_key == "broadcast:" + created["id"])
            )
            is None
        )


def test_legacy_unknown_mail_keeps_paid_fact_and_quarantines_job(routed):
    from sqlalchemy import func
    from sunrise_infrastructure.db import Job, Payment, Settlement

    context, _ = routed
    db, _, commerce, _, _, _, aid, *_ = context
    calls = []

    class UnknownMail:
        def deliver(self, *args):
            calls.append(args[-1])
            raise DomainError("mail_delivery_unknown", "Unknown send outcome", 503, retryable=False)

    commerce.mail_transport = UnknownMail()
    commerce.notification_allowlist = {"events-owner@example.test"}
    with db.transaction() as session:
        before = (
            session.scalar(select(func.count()).select_from(Payment)),
            session.scalar(select(func.count()).select_from(Settlement)),
        )
        job = Job(
            key="legacy-mail-unknown",
            kind="broadcast",
            account_id=aid,
            payload={
                "recipients": ["events-owner@example.test"],
                "nodes": [{"type": "text", "text": "Synthetic mail"}],
            },
        )
        session.add(job)
        session.flush()
        identifier = job.id
    commerce.run_jobs()
    commerce.run_jobs()
    assert calls == [identifier]
    with db.transaction() as session:
        assert session.get(Job, identifier).state == "review"
        assert session.get(Job, identifier).last_error == "mail_delivery_unknown"
        assert (
            session.scalar(select(func.count()).select_from(Payment)),
            session.scalar(select(func.count()).select_from(Settlement)),
        ) == before


@pytest.mark.parametrize("mime", ["image/png", "image/jpeg", "video/mp4"])
def test_registered_worker_media_is_sent_as_local_multipart_only(routed, tmp_path, monkeypatch, mime):
    from pathlib import Path

    import httpx
    from sunrise_infrastructure.db import NotificationDelivery
    from sunrise_infrastructure.notifications import NotificationService

    context, _ = routed
    db, _, commerce, config, _, _, aid, *_ = context
    service = registry(context, tmp_path)
    monkeypatch.setattr(
        "sunrise_infrastructure.broadcast_media_registry.configured_media_store", lambda config: service.store
    )
    if mime == "video/mp4":
        content = Path("tests/fixtures/broadcast-chromium-synthetic.mp4").read_bytes()
    elif mime == "image/jpeg":
        buffer = BytesIO()
        Image.new("RGB", (8, 8), "red").save(buffer, format="JPEG")
        content = buffer.getvalue()
    else:
        content = png()
    reference = service.register(aid, [content], mime)
    actual = service.resolve(aid, reference)["bytes"]
    config.bot_test_token, config.test_bot_ids = "123456789:synthetic-token", "123456789"
    with db.transaction() as session:
        for delivery in session.scalars(select(NotificationDelivery)):
            delivery.state = "cancelled"
    broadcasts = BroadcastService(commerce, config)
    broadcasts.create(
        aid,
        [{"type": "text", "text": "Synthetic media caption"}],
        "Synthetic registered multipart",
        draft=False,
        media=reference,
    )
    assert broadcasts.expand_pending() == 1
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"id": 123456789}})
        assert request.url.path.endswith("/sendVideo" if mime == "video/mp4" else "/sendPhoto")
        assert request.headers["content-type"].startswith("multipart/form-data;")
        assert actual in request.content and reference["proof"].encode() not in request.content
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 123}})

    original = httpx.Client
    monkeypatch.setattr(
        "sunrise_infrastructure.notifications.httpx.Client",
        lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handler)),
    )
    notifier = NotificationService(db, config)
    assert notifier.deliver_pending() == 1
    assert notifier.deliver_pending() == 0
    assert len(calls) == 2


def test_media_caption_uses_1024_limit_including_title(routed, tmp_path, monkeypatch):
    context, _ = routed
    service = registry(context, tmp_path)
    monkeypatch.setattr(
        "sunrise_infrastructure.broadcast_media_registry.configured_media_store", lambda config: service.store
    )
    reference = service.register(context[6], [png()], "image/png")
    broadcasts = BroadcastService(context[2], context[3])
    text = [{"type": "text", "text": "x" * 1024}]
    assert broadcasts.preview(context[6], text)["recipient_count"] >= 1
    with pytest.raises(DomainError) as denied:
        broadcasts.preview(context[6], text, media=reference)
    assert "1024" in denied.value.message
