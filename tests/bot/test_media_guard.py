import asyncio
import copy
import hashlib
import io
from time import monotonic

import aiohttp
import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import GetFile, SendPhoto, SendVideo
from aiogram.types import BufferedInputFile, File, Message
from PIL import Image
from sunrise_bot.admin_flow import AdminDraft
from sunrise_bot.media_guard import BotMediaGuard
from sunrise_bot.safety import BotSafety, GuardedSession
from sunrise_core.errors import DomainError

TOKEN = "700001:" + "x" * 35


def photo():
    buffer = io.BytesIO()
    Image.new("RGB", (32, 24), "red").save(buffer, format="JPEG")
    return buffer.getvalue()


def incoming(content=None, **changes):
    data = content or photo()
    value = dict(
        message_id=81,
        date=0,
        chat={"id": 101, "type": "private"},
        from_user={"id": 101, "is_bot": False, "first_name": "Тест"},
        photo=[
            dict(file_id="incoming-1", file_unique_id="unique-1", width=32, height=24, file_size=len(data))
        ],
        caption="Тест рассылки",
    )
    return Message.model_validate(value | changes)


class Transport(BaseSession):
    def __init__(self, data):
        super().__init__()
        self.calls = []
        self.response = File(
            file_id="incoming-1",
            file_unique_id="unique-1",
            file_size=len(data),
            file_path="photos/file_1.jpg",
        )

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        pytest.fail("wrapped downloader must never be used")
        yield b""

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        return self.response if isinstance(method, GetFile) else True


class Response:
    def __init__(self, chunks, status=200, headers=None, failure=None):
        self.status, self.headers = status, headers or {}
        self.chunks, self.failure, self.consumed = chunks, failure, 0
        self.content = self

    async def __aenter__(self):
        if self.failure:
            raise self.failure
        return self

    async def __aexit__(self, *args):
        return False

    async def iter_chunked(self, size):
        for chunk in self.chunks:
            self.consumed += 1
            yield chunk


class Client:
    def __init__(self, response):
        self.response, self.calls, self.options = response, [], []

    def __call__(self, **kwargs):
        self.options.append(kwargs)
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def setup(response=None):
    data = photo()
    transport = Transport(data)
    client = Client(response or Response([data]))
    session = GuardedSession(transport, BotSafety(frozenset({"700001"}), frozenset({"101"})))
    session.media = BotMediaGuard(session, http_factory=client)
    bot = Bot(TOKEN, session=session)
    draft = AdminDraft("800001", "broadcast", [("nodes", "Текст", "nodes")])
    return bot, session.media, draft, transport, client


async def yes():
    return True


async def no():
    return False


def test_direct_getfile_and_video_deny():
    async def run():
        bot, _, _, transport, _ = setup()
        for method in (
            GetFile(file_id="incoming-1"),
            SendVideo(chat_id=101, video=BufferedInputFile(b"video", "x.mp4")),
        ):
            with pytest.raises(PermissionError):
                await bot(method)
        assert not transport.calls

    asyncio.run(run())


def test_authorized_upload_exact_file_and_bounded_dedicated_transport():
    async def run():
        bot, guard, draft, transport, client = setup()
        async with guard.authorized_upload_scope(
            bot, incoming(), owner_id="800001", draft=draft, authorize=yes
        ) as scope:
            assert scope.mime == "image/jpeg" and scope.file_id == "incoming-1"
            result = await bot.get_file(scope.file_id)
            assert await guard.download_approved_file(bot, result) == photo()
            with pytest.raises(PermissionError):
                await bot.get_file(scope.file_id)
            with pytest.raises(PermissionError):
                await guard.download_approved_file(bot, result)
        assert len(transport.calls) == 1
        assert client.calls[0][1] == {"allow_redirects": False, "ssl": True}
        assert client.calls[0][0].startswith("https://api.telegram.org/file/bot")
        assert client.options[0]["trust_env"] is False
        assert client.options[0]["timeout"].total == 15
        with pytest.raises(PermissionError):
            await bot.get_file("incoming-1")

    asyncio.run(run())


@pytest.mark.parametrize(
    "change",
    [
        {"chat": {"id": 102, "type": "private"}},
        {"chat": {"id": 101, "type": "group"}},
        {"from_user": {"id": 102, "is_bot": False, "first_name": "Other"}},
        {"from_user": {"id": 101, "is_bot": True, "first_name": "Bot"}},
        {"message_id": 0},
        {"photo": [dict(file_id="x", file_unique_id="y", width=32, height=24)]},
        {"photo": [dict(file_id="x", file_unique_id="y", width=32, height=24, file_size=2_000_001)]},
    ],
)
def test_wrong_incoming_identity_or_size_denied(change):
    async def run():
        bot, guard, draft, transport, client = setup()
        with pytest.raises(PermissionError):
            async with guard.authorized_upload_scope(
                bot, incoming(**change), owner_id="800001", draft=draft, authorize=yes
            ):
                pytest.fail("scope must not open")
        assert not transport.calls and not client.calls

    asyncio.run(run())


@pytest.mark.parametrize(
    "change", [{"owner": "other"}, {"kind": "message"}, {"step": "reason"}, {"expires": 0}]
)
def test_wrong_draft_denied(change):
    async def run():
        bot, guard, draft, _, _ = setup()
        for key, value in change.items():
            setattr(draft, key, value)
        with pytest.raises(PermissionError):
            async with guard.authorized_upload_scope(
                bot, incoming(), owner_id="800001", draft=draft, authorize=yes
            ):
                pytest.fail("scope must not open")

    asyncio.run(run())


def test_capability_revoked_and_draft_changed_expired_and_child_task_denied():
    async def run():
        bot, guard, draft, transport, _ = setup()
        allowed = True

        async def authorize():
            return allowed

        with pytest.raises(PermissionError):
            async with guard.authorized_upload_scope(
                bot, incoming(), owner_id="800001", draft=draft, authorize=no
            ):
                pytest.fail("revoked scope opened")
        async with guard.authorized_upload_scope(
            bot, incoming(), owner_id="800001", draft=draft, authorize=authorize
        ) as scope:
            with pytest.raises(PermissionError):
                await asyncio.create_task(bot.get_file("incoming-1"))
            with pytest.raises(PermissionError):
                await bot.get_file("other-file")
            draft.revision += 1
            with pytest.raises(PermissionError):
                await bot.get_file("incoming-1")
            draft.revision -= 1
            allowed = False
            with pytest.raises(PermissionError):
                await bot.get_file("incoming-1")
            allowed = True
            scope.expires = monotonic() - 1
            with pytest.raises(PermissionError):
                await bot.get_file("incoming-1")
        assert not transport.calls

    asyncio.run(run())


def test_wrong_bot_and_forged_scope_denied():
    async def run():
        bot, guard, draft, transport, _ = setup()
        async with guard.authorized_upload_scope(
            bot, incoming(), owner_id="800001", draft=draft, authorize=yes
        ) as scope:
            other = Bot("700002:" + "x" * 35, session=bot.session)
            with pytest.raises((PermissionError, ValueError)):
                await other.get_file("incoming-1")
            token = guard._current.set(copy.copy(scope))
            try:
                with pytest.raises(PermissionError):
                    await bot.get_file("incoming-1")
            finally:
                guard._current.reset(token)
        assert not transport.calls

    asyncio.run(run())


def test_cancellation_revokes_inherited_context():
    async def run():
        bot, guard, draft, transport, _ = setup()
        detached = None
        gate = asyncio.Event()

        async def later():
            await gate.wait()
            with pytest.raises(PermissionError):
                await bot.get_file("incoming-1")

        with pytest.raises(asyncio.CancelledError):
            async with guard.authorized_upload_scope(
                bot, incoming(), owner_id="800001", draft=draft, authorize=yes
            ):
                detached = asyncio.create_task(later())
                raise asyncio.CancelledError()
        gate.set()
        await detached
        assert not transport.calls

    asyncio.run(run())


@pytest.mark.parametrize(
    "path",
    [
        "../x",
        "/photos/x",
        "https://evil.test/x",
        "photos\\x",
        "photos/%2e%2e/x",
        "photos/%2Fx",
        "photos/x?token=x",
        "photos/x#fragment",
        "photos/x\n",
        "photos//x",
    ],
)
def test_returned_telegram_path_rejected_without_download(path):
    async def run():
        bot, guard, draft, transport, client = setup()
        transport.response = transport.response.model_copy(update={"file_path": path})
        async with guard.authorized_upload_scope(
            bot, incoming(), owner_id="800001", draft=draft, authorize=yes
        ):
            with pytest.raises(PermissionError):
                await bot.get_file("incoming-1")
        assert not client.calls

    asyncio.run(run())


@pytest.mark.parametrize(
    "field,value", [("file_id", "other"), ("file_size", 1), ("file_size", None), ("file_path", None)]
)
def test_getfile_response_exact_metadata_required(field, value):
    async def run():
        bot, guard, draft, transport, _ = setup()
        transport.response = transport.response.model_copy(update={field: value})
        async with guard.authorized_upload_scope(
            bot, incoming(), owner_id="800001", draft=draft, authorize=yes
        ):
            with pytest.raises(PermissionError):
                await bot.get_file("incoming-1")

    asyncio.run(run())


@pytest.mark.parametrize(
    "response",
    [
        Response([b"secret"], status=302, headers={"Location": "http://127.0.0.1/secret"}),
        Response([photo() + b"x", b"must-not-read"]),
        Response([b"short"]),
        Response([photo()], headers={"Content-Length": "garbage"}),
        Response([photo()], headers={"Content-Length": "1"}),
    ],
)
def test_redirect_stream_overrun_truncation_and_declared_size_denied(response):
    async def run():
        bot, guard, draft, _, client = setup(response)
        async with guard.authorized_upload_scope(
            bot, incoming(), owner_id="800001", draft=draft, authorize=yes
        ):
            result = await bot.get_file("incoming-1")
            with pytest.raises(DomainError):
                await guard.download_approved_file(bot, result)
        assert len(client.calls) == 1
        assert response.consumed <= 1

    asyncio.run(run())


def test_download_transport_exception_sanitized():
    async def run():
        response = Response(
            [],
            failure=aiohttp.ClientError("https://api.telegram.org/file/bot" + TOKEN + "/photos/private.jpg"),
        )
        bot, guard, draft, _, _ = setup(response)
        async with guard.authorized_upload_scope(
            bot, incoming(), owner_id="800001", draft=draft, authorize=yes
        ):
            result = await bot.get_file("incoming-1")
            with pytest.raises(DomainError) as error:
                await guard.download_approved_file(bot, result)
            assert TOKEN not in str(error.value) and "private.jpg" not in str(error.value)
            assert error.value.__suppress_context__

    asyncio.run(run())


def asset(data=None, mime="image/jpeg"):
    data = data or photo()
    return dict(
        asset_id="a" * 32,
        sha256=hashlib.sha256(data).hexdigest(),
        mime=mime,
        size_bytes=len(data),
        proof="b" * 64,
    )


def test_registered_send_exact_bytes_recipient_and_single_use():
    async def run():
        bot, guard, _, transport, _ = setup()
        ref = asset()

        async def resolve(reference):
            return {"reference": ref, "bytes": photo()}

        async with guard.registered_asset_send_scope(
            bot, "101", ref, resolve=resolve, authorize=yes
        ) as local:
            for method in (
                SendPhoto(chat_id=102, photo=local),
                SendPhoto(chat_id=101, photo=BufferedInputFile(photo(), "other.jpg")),
                SendPhoto(chat_id=101, photo="file_id"),
            ):
                with pytest.raises(PermissionError):
                    await bot(method)
            assert await bot.send_photo(101, local)
            with pytest.raises(PermissionError):
                await bot.send_photo(101, local)
        assert len(transport.calls) == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    "resolved",
    [
        None,
        {"reference": asset(), "bytes": b"changed"},
        {"reference": asset() | {"asset_id": "c" * 32}, "bytes": photo()},
    ],
)
def test_unregistered_or_changed_asset_cannot_issue_send_scope(resolved):
    async def run():
        bot, guard, _, transport, _ = setup()

        async def resolve(reference):
            return resolved

        with pytest.raises(PermissionError):
            async with guard.registered_asset_send_scope(bot, "101", asset(), resolve=resolve, authorize=yes):
                pytest.fail("unregistered asset scope opened")
        assert not transport.calls

    asyncio.run(run())


def test_active_media_scope_never_falls_back_to_qr_and_denies_child_task():
    async def run():
        bot, guard, _, transport, _ = setup()

        async def resolve(reference):
            return {"reference": asset(), "bytes": photo()}

        async with guard.registered_asset_send_scope(
            bot, "101", asset(), resolve=resolve, authorize=yes
        ) as local:
            with pytest.raises(PermissionError):
                await bot.send_photo(101, BufferedInputFile(b"\x89PNG\r\n\x1a\nqr", "qr.png"))
            with pytest.raises(PermissionError):
                await asyncio.create_task(bot.send_photo(101, local))
        assert not transport.calls
        assert await bot.send_photo(101, BufferedInputFile(b"\x89PNG\r\n\x1a\nqr", "qr.png"))

    asyncio.run(run())


def test_registered_video_send_rejects_thumbnail_cover_and_altered_bytes():
    async def run():
        bot, guard, _, transport, _ = setup()
        data = b"registered mp4 bytes"
        ref = asset(data, "video/mp4")

        async def resolve(reference):
            return {"reference": ref, "bytes": data}

        async with guard.registered_asset_send_scope(
            bot, "101", ref, resolve=resolve, authorize=yes
        ) as local:
            for method in (
                SendVideo(chat_id=101, video=local, thumbnail=BufferedInputFile(b"thumb", "thumb.jpg")),
                SendVideo(chat_id=101, video=local, cover="remote-file"),
            ):
                with pytest.raises(PermissionError):
                    await bot(method)
            local.data = b"changed"
            with pytest.raises(PermissionError):
                await bot.send_video(101, local)
            local.data = data
            assert await bot.send_video(101, local)
        assert len(transport.calls) == 1

    asyncio.run(run())


def test_draft_changed_during_authorization_await_denied():
    async def run():
        bot, guard, draft, transport, _ = setup()
        count = 0

        async def authorize():
            nonlocal count
            count += 1
            if count == 2:
                draft.revision += 1
            return True

        async with guard.authorized_upload_scope(
            bot, incoming(), owner_id="800001", draft=draft, authorize=authorize
        ):
            with pytest.raises(PermissionError):
                await bot.get_file("incoming-1")
        assert not transport.calls

    asyncio.run(run())


def test_recipient_revocation_after_scope_issue_denied():
    async def run():
        bot, guard, _, transport, _ = setup()

        async def resolve(reference):
            return {"reference": asset(), "bytes": photo()}

        async with guard.registered_asset_send_scope(
            bot, "101", asset(), resolve=resolve, authorize=yes
        ) as local:
            bot.session.safety = BotSafety(frozenset({"700001"}), frozenset({"102"}))
            with pytest.raises(PermissionError):
                await bot.send_photo(101, local)
        assert not transport.calls

    asyncio.run(run())


def test_http_compression_denied_and_automatic_decompression_disabled():
    async def run():
        response = Response([photo()], headers={"Content-Encoding": "gzip"})
        bot, guard, draft, _, client = setup(response)
        async with guard.authorized_upload_scope(
            bot, incoming(), owner_id="800001", draft=draft, authorize=yes
        ):
            result = await bot.get_file("incoming-1")
            with pytest.raises(DomainError):
                await guard.download_approved_file(bot, result)
        assert client.options[0]["auto_decompress"] is False
        assert response.consumed == 0

    asyncio.run(run())


def test_changed_reference_during_registry_resolve_denied():
    async def run():
        bot, guard, _, transport, _ = setup()
        ref = asset()

        async def resolve(reference):
            reference["asset_id"] = "c" * 32
            return {"reference": reference, "bytes": photo()}

        with pytest.raises(PermissionError):
            async with guard.registered_asset_send_scope(bot, "101", ref, resolve=resolve, authorize=yes):
                pytest.fail("changed request must not open scope")
        assert not transport.calls

    asyncio.run(run())


def test_expired_send_scope_denied():
    async def run():
        bot, guard, _, transport, _ = setup()

        async def resolve(reference):
            return {"reference": asset(), "bytes": photo()}

        async with guard.registered_asset_send_scope(
            bot, "101", asset(), resolve=resolve, authorize=yes
        ) as local:
            guard._current.get().expires = monotonic() - 1
            with pytest.raises(PermissionError):
                await bot.send_photo(101, local)
        assert not transport.calls

    asyncio.run(run())


def test_replaced_message_and_returned_file_object_denied():
    async def run():
        bot, guard, draft, transport, client = setup()
        async with guard.authorized_upload_scope(
            bot, incoming(), owner_id="800001", draft=draft, authorize=yes
        ) as scope:
            scope.message = incoming(message_id=82)
            with pytest.raises(PermissionError):
                await bot.get_file("incoming-1")
            scope.message = incoming()
            file = await bot.get_file("incoming-1")
            with pytest.raises(PermissionError):
                await guard.download_approved_file(bot, file.model_copy())
        assert len(transport.calls) == 1 and not client.calls

    asyncio.run(run())


@pytest.mark.parametrize("suffix", ["?query=x", "/../other", "%2fother", "#fragment", "@host"])
def test_token_cannot_inject_download_url_path_or_query(suffix):
    async def run():
        bot, guard, draft, transport, client = setup()
        malformed = Bot(TOKEN + suffix, session=bot.session)
        with pytest.raises(PermissionError):
            async with guard.authorized_upload_scope(
                malformed, incoming(), owner_id="800001", draft=draft, authorize=yes
            ):
                pytest.fail("malformed token must not issue URL scope")
        assert not transport.calls and not client.calls

    asyncio.run(run())


def test_send_scope_rechecks_fresh_permission_and_consumes_before_auth_await():
    async def run():
        bot, guard, _, transport, _ = setup()
        allowed = True
        observed = []

        async def authorize():
            if guard.has_scope():
                observed.append(guard._current.get().used)
                await asyncio.sleep(0)
            return allowed

        async def resolve(reference):
            return {"reference": asset(), "bytes": photo()}

        async with guard.registered_asset_send_scope(
            bot, "101", asset(), resolve=resolve, authorize=authorize
        ) as local:
            allowed = False
            with pytest.raises(PermissionError):
                await bot.send_photo(101, local)
            allowed = True
            with pytest.raises(PermissionError):
                await bot.send_photo(101, local)
        assert observed == [True]
        assert not transport.calls and not guard._registry and not guard.has_scope()

    asyncio.run(run())


def test_send_refuses_bytes_changed_during_fresh_authorization():
    async def run():
        bot, guard, _, transport, _ = setup()
        issued = []

        async def authorize():
            if issued:
                issued[0].data = b"changed during authorization"
                await asyncio.sleep(0)
            return True

        async def resolve(reference):
            return {"reference": asset(), "bytes": photo()}

        async with guard.registered_asset_send_scope(
            bot, "101", asset(), resolve=resolve, authorize=authorize
        ) as local:
            issued.append(local)
            with pytest.raises(PermissionError):
                await bot.send_photo(101, local)
        assert not transport.calls and not guard._registry

    asyncio.run(run())
