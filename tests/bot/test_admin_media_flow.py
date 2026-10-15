import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from aiogram.types import File, MessageEntity
from sunrise_bot import copy
from sunrise_bot.admin_flow import AdminFlow
from sunrise_infrastructure.broadcast_media import BroadcastMediaStore

from tests.bot.test_admin_flow import App, Console
from tests.bot.test_media_guard import Response, incoming, setup


class MediaConsole(Console):
    def __init__(self, store):
        super().__init__()
        self.store, self.registered = store, {}
        self.media_capability = True

    def capabilities(self, actor):
        return {"capabilities": ["broadcasts_write"] if self.media_capability else []}

    def register_media(self, actor, chunks, mime):
        ref = self.store.upload(chunks, mime=mime)
        self.registered[ref["asset_id"]] = (actor, ref)
        self.calls.append(("upload", actor, ref))
        return ref

    def resolve_media(self, actor, ref):
        assert self.registered[ref["asset_id"]] == (actor, ref)
        self.calls.append(("resolve", actor, ref))
        return {"reference": ref, "bytes": self.store.read(ref)}

    def bot_broadcast_preview(self, actor, nodes, *, media=None):
        self.calls.append(("preview", actor, media))
        return {"nodes": nodes, "recipient_count": 1}

    def bot_broadcast_create(self, actor, nodes, reason, confirmed=False, *, media=None):
        self.calls.append(("create", actor, nodes, media))
        return {"id": "j1", "state": "draft", "recipient_count": 1}


def app_flow(tmp_path):
    app = App()
    console = MediaConsole(BroadcastMediaStore(tmp_path / "media", signing_key=b"synthetic-test-key" * 2))
    app.admin.bot_console = console

    async def call(method, *args):
        return {"id": "800001", "telegram_id": "101"}

    app.call = call
    bot, guard, _, transport, client = setup()
    app.bot = bot
    return app, AdminFlow(app), console, guard, transport, client


def test_bot_photo_upload_caption_preview_and_create_keep_registered_reference(tmp_path):
    async def run():
        app, flow, console, _, transport, _ = app_flow(tmp_path)
        await flow.execute("101", "800001", "admin_broadcast_compose", {}, None)
        assert await flow.message("101", {"id": "800001"}, incoming())
        draft = flow.drafts["101"]
        assert draft.values["media"] == next(iter(console.registered.values()))[1]
        assert draft.values["nodes"][0]["children"][0]["text"] == "Тест рассылки"
        assert draft.step == "review"
        assert draft.reason == copy.ADMIN_AUDIT_REASON
        draft = flow.drafts["101"]
        data = {"draft_id": draft.identifier, "draft_owner": draft.owner, "draft_revision": draft.revision}
        await flow.execute("101", "800001", "admin_broadcast_test", data, None)
        assert any(method.__api_method__ == "sendPhoto" for method in transport.calls)
        assert any(item[0] == "resolve" for item in console.calls)
        draft = flow.drafts["101"]
        data["draft_revision"] = draft.revision
        await flow.execute("101", "800001", "admin_save", data, None)
        saved = next(item for item in console.calls if item[0] == "create")
        assert saved[3] == next(iter(console.registered.values()))[1]
        assert "101" not in flow.drafts

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["caption_missing", "capability_revoked", "wrong_user", "review_step"])
def test_bot_upload_denied_before_getfile_for_bad_context(tmp_path, mode):
    async def run():
        app, flow, console, _, transport, client = app_flow(tmp_path)
        await flow.execute("101", "800001", "admin_broadcast_compose", {}, None)
        message = incoming()
        if mode == "caption_missing":
            message = incoming(caption=None)
        elif mode == "capability_revoked":
            console.media_capability = False
        elif mode == "wrong_user":
            message = incoming(from_user={"id": 102, "is_bot": False, "first_name": "Other"})
        else:
            flow.drafts["101"].step = "review"
        assert await flow.message("101", {"id": "800001"}, message)
        assert not console.registered
        assert not transport.calls and not client.calls
        assert app.views[-1]["title"] in {"Проверьте данные", "Действие недоступно"}

    asyncio.run(run())


def test_bot_real_synthetic_video_upload_and_registered_preview(tmp_path):
    async def run():
        app, flow, console, _, transport, client = app_flow(tmp_path)
        data = (Path(__file__).parents[1] / "fixtures" / "broadcast-chromium-synthetic.mp4").read_bytes()
        transport.response = File(
            file_id="incoming-1",
            file_unique_id="unique-1",
            file_size=len(data),
            file_path="videos/file_1.mp4",
        )
        client.response = Response([data])
        message = incoming(
            photo=[],
            video=dict(
                file_id="incoming-1",
                file_unique_id="unique-1",
                file_size=len(data),
                width=64,
                height=64,
                duration=1,
                mime_type="video/mp4",
            ),
        )
        await flow.execute("101", "800001", "admin_broadcast_compose", {}, None)
        await flow.message("101", {"id": "800001"}, message)
        draft = flow.drafts["101"]
        ref = draft.values["media"]
        assert console.store.read(ref) == data
        assert ref["mime"] == "video/mp4"
        await flow.message("101", {"id": "800001"}, incoming(photo=[], text="Причина рассылки", caption=None))
        draft = flow.drafts["101"]
        actions = {"draft_id": draft.identifier, "draft_owner": draft.owner, "draft_revision": draft.revision}
        await flow.execute("101", "800001", "admin_broadcast_test", actions, None)
        assert transport.calls[-1].__api_method__ == "sendVideo"

    asyncio.run(run())


def test_media_caption_preserves_utf16_custom_emoji(tmp_path):
    async def run():
        _, flow, _, _, _, _ = app_flow(tmp_path)
        await flow.execute("101", "800001", "admin_broadcast_compose", {}, None)
        entity = MessageEntity(type="custom_emoji", offset=3, length=2, custom_emoji_id="123456")
        message = incoming(caption="😀 🎁 Привет", caption_entities=[entity])
        await flow.message("101", {"id": "800001"}, message)
        children = flow.drafts["101"].values["nodes"][0]["children"]
        assert children[1] == {"type": "emoji", "id": "123456", "fallback": "🎁", "label": "Эмодзи"}

    asyncio.run(run())


@pytest.mark.parametrize(
    "mutation", ["expired", "cancelled", "replaced", "revision", "capability_revoked", "media_changed"]
)
@pytest.mark.parametrize("boundary", ["authorized", "resolve_media", "actual_send"])
def test_preview_stale_authorization_never_sends_media(tmp_path, mutation, boundary):
    async def run():
        _, flow, console, guard, transport, _ = app_flow(tmp_path)
        await flow.execute("101", "800001", "admin_broadcast_compose", {}, None)
        await flow.message("101", {"id": "800001"}, incoming())
        await flow.message("101", {"id": "800001"}, incoming(photo=[], text="Причина рассылки", caption=None))
        draft = flow.drafts["101"]

        def invalidate():
            if mutation == "expired":
                draft.expires = 0
            elif mutation == "cancelled":
                flow.cancel("101")
            elif mutation == "replaced":
                flow.drafts["101"] = replace(draft)
            elif mutation == "revision":
                draft.revision += 1
            elif mutation == "capability_revoked":
                console.media_capability = False
            else:
                draft.values["media"] = draft.values["media"] | {"asset_id": "c" * 32}

        if boundary == "authorized":
            original = flow.authorized

            async def changed(uid, aid):
                result = await original(uid, aid)
                invalidate()
                return result

            flow.authorized = changed
        elif boundary == "resolve_media":
            original = flow.call

            async def changed(method, *args, **kwargs):
                result = await original(method, *args, **kwargs)
                if method == "resolve_media":
                    invalidate()
                return result

            flow.call = changed
        else:
            original = guard.authorize_send

            async def changed(bot, method):
                invalidate()
                return await original(bot, method)

            guard.authorize_send = changed
        transport.calls.clear()
        with pytest.raises(PermissionError):
            await flow.test_media("101", "800001", draft)
        assert not transport.calls
        assert not guard._registry and not guard.has_scope()

    asyncio.run(run())
