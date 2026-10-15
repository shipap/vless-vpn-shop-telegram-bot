import hashlib
import io
import struct
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path

import pytest
from PIL import Image
from sunrise_core.errors import DomainError
from sunrise_infrastructure.broadcast_media import (
    BroadcastMediaStore,
    MediaLimits,
    attach_media,
    checked_email_bytes,
)


def picture(fmt="PNG", size=(32, 24)):
    buffer = io.BytesIO()
    Image.new("RGB", size, "red").save(buffer, format=fmt)
    return buffer.getvalue()


def box(kind, payload):
    return struct.pack(">I", len(payload) + 8) + kind + payload


def video(*, external=False, video_track=True):
    # Синтетический контейнер для проверки структуры; не доказательство воспроизведения кодека.
    handler = box(b"hdlr", b"\x00" * 8 + (b"vide" if video_track else b"soun") + b"\x00" * 12)
    url = box(b"url ", b"\x00\x00\x00" + (b"\x00https://invalid.test/x\x00" if external else b"\x01"))
    dref = box(b"dref", b"\x00" * 4 + struct.pack(">I", 1) + url)
    sample = bytearray(78)
    struct.pack_into(">H", sample, 6, 1)
    struct.pack_into(">HH", sample, 24, 32, 24)
    avcc = box(b"avcC", b"\x01\x64\x00\x1f\xff\xe1\x00")
    stsd = box(b"stsd", b"\x00" * 4 + struct.pack(">I", 1) + box(b"avc1", bytes(sample) + avcc))
    track = box(b"trak", box(b"mdia", handler + box(b"minf", box(b"dinf", dref) + box(b"stbl", stsd))))
    return box(b"ftyp", b"isom\x00\x00\x00\x00isommp42") + box(b"moov", track) + box(b"mdat", b"fixture")


@pytest.fixture
def store(tmp_path):
    return BroadcastMediaStore(tmp_path / "media", signing_key=b"synthetic-test-key" * 2)


@pytest.mark.parametrize(
    "mime,content", [("image/png", picture()), ("image/jpeg", picture("JPEG")), ("video/mp4", video())]
)
def test_upload_signed_reference_round_trip(store, mime, content):
    ref = store.upload([content[:11], content[11:]], mime=mime)
    assert set(ref) == {"asset_id", "sha256", "mime", "size_bytes", "proof"}
    assert len(ref["asset_id"]) == 32 and len(ref["proof"]) == 64
    stored = store.read(ref)
    assert ref["mime"] == mime
    assert ref["size_bytes"] == len(stored)
    assert ref["sha256"] == hashlib.sha256(stored).hexdigest()
    assert store.read(ref) == stored


def test_photos_are_reencoded_without_appended_payload(store):
    source = picture() + b"<script>untrusted</script>"
    ref = store.upload([source], mime="image/png")
    assert b"untrusted" not in store.read(ref)


@pytest.mark.parametrize(
    "mime,content",
    [
        ("image/png", b"<svg/>"),
        ("image/jpeg", picture()),
        ("text/html", b"html"),
        ("video/mp4", b"fake-ftyp-isom"),
        ("video/mp4", video()[:-1]),
        ("video/mp4", video(external=True)),
        ("video/mp4", video(video_track=False)),
    ],
)
def test_rejects_spoofed_malformed_and_external_media(store, mime, content):
    with pytest.raises(DomainError) as error:
        store.upload([content], mime=mime)
    assert error.value.code == "media_invalid"
    assert not list(store.root.iterdir())


def test_stream_limit_stops_consuming_and_leaves_no_asset(tmp_path):
    store = BroadcastMediaStore(
        tmp_path / "media", signing_key=b"x" * 32, limits=MediaLimits(photo_bytes=100)
    )

    def chunks():
        yield b"x" * 101
        pytest.fail("oversize stream must not be consumed further")

    with pytest.raises(DomainError) as error:
        store.upload(chunks(), mime="image/png")
    assert error.value.code == "media_too_large"
    assert not list(store.root.iterdir())


def test_empty_media_is_rejected(store):
    with pytest.raises(DomainError):
        store.upload([], mime="image/png")


@pytest.mark.parametrize(
    "change",
    [
        {"asset_id": "../escape"},
        {"mime": "text/html"},
        {"size_bytes": True},
        {"sha256": "0" * 64},
        {"proof": "0" * 64},
        {"path": "secret"},
    ],
)
def test_reference_tampering_is_rejected(store, change):
    ref = store.upload([picture()], mime="image/png")
    with pytest.raises(DomainError):
        store.read(ref | change)


def test_missing_or_modified_stored_bytes_fail_closed(store):
    ref = store.upload([picture()], mime="image/png")
    path = store.root / ref["asset_id"]
    path.write_bytes(b"tampered")
    with pytest.raises(DomainError) as error:
        store.read(ref)
    assert error.value.code == "media_unavailable"
    path.unlink()
    with pytest.raises(DomainError):
        store.read(ref)


def test_read_survives_store_restart(tmp_path):
    key = b"x" * 32
    store = BroadcastMediaStore(tmp_path / "media", signing_key=key)
    ref = store.upload([video()], mime="video/mp4")
    assert BroadcastMediaStore(store.root, signing_key=key).read(ref) == video()
    with pytest.raises(DomainError):
        BroadcastMediaStore(store.root, signing_key=b"y" * 32).read(ref)


def test_reparse_paths_fail_closed(store, monkeypatch):
    ref = store.upload([picture()], mime="image/png")
    monkeypatch.setattr("sunrise_infrastructure.broadcast_media._reparse", lambda path: path == store.root)
    with pytest.raises(DomainError):
        store.read(ref)
    with pytest.raises(DomainError):
        store.upload([picture()], mime="image/png")


def test_photo_dimensions_follow_telegram_bounds(store):
    with pytest.raises(DomainError):
        store.upload([picture(size=(100, 2))], mime="image/png")


@pytest.mark.parametrize(
    "limits", [MediaLimits(photo_bytes=0), MediaLimits(video_bytes=-1), MediaLimits(email_bytes=True)]
)
def test_invalid_limits_are_rejected(tmp_path, limits):
    with pytest.raises(ValueError):
        BroadcastMediaStore(tmp_path / "media", signing_key=b"x" * 32, limits=limits)


def test_short_signing_key_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        BroadcastMediaStore(tmp_path / "media", signing_key=b"x")


@pytest.mark.parametrize("mime,content", [("image/png", picture()), ("video/mp4", video())])
def test_mail_attachment_round_trip(store, mime, content):
    ref = store.upload([content], mime=mime)
    message = EmailMessage()
    message.set_content("Тестовое письмо")
    attach_media(message, store, ref)
    wire = checked_email_bytes(message, limit=12_000_000)
    parsed = BytesParser(policy=policy.default).parsebytes(wire)
    attachments = list(parsed.iter_attachments())
    assert len(attachments) == 1
    assert attachments[0].get_content_type() == mime
    assert attachments[0].get_payload(decode=True) == store.read(ref)
    assert attachments[0].get_filename().startswith("media-")
    assert b"\r\n" in wire


def test_email_size_uses_final_mime_wire_and_smtp_advertised_size(store):
    ref = store.upload([picture()], mime="image/png")
    message = EmailMessage()
    message.set_content("Тест")
    attach_media(message, store, ref)
    wire = checked_email_bytes(message, limit=12_000_000)
    assert len(wire) > ref["size_bytes"]
    assert checked_email_bytes(message, limit=len(wire), smtp_size=str(len(wire))) == wire
    for kwargs in ({"limit": len(wire) - 1}, {"limit": 12_000_000, "smtp_size": str(len(wire) - 1)}):
        with pytest.raises(DomainError) as error:
            checked_email_bytes(message, **kwargs)
        assert error.value.code == "media_email_too_large"


def test_invalid_advertised_smtp_size_fails_closed():
    message = EmailMessage()
    message.set_content("Тест")
    with pytest.raises(DomainError):
        checked_email_bytes(message, limit=10000, smtp_size="garbage")


def test_mp4_requires_video_sample_description(store):
    content = video()
    content = content.replace(b"stbl", b"free", 1)
    with pytest.raises(DomainError):
        store.upload([content], mime="video/mp4")


def test_mp4_rejects_external_sample_reference(store):
    content = video()
    index = content.index(b"avc1") + 4
    content = content[: index + 6] + b"\x00\x02" + content[index + 8 :]
    with pytest.raises(DomainError):
        store.upload([content], mime="video/mp4")


def test_mp4_rejects_malformed_codec_box(store):
    content = video()
    index = content.index(b"avcC") - 4
    content = content[:index] + b"\x7f\xff\xff\xff" + content[index + 4 :]
    with pytest.raises(DomainError):
        store.upload([content], mime="video/mp4")


def test_email_one_attachment_limit(store):
    ref = store.upload([picture()], mime="image/png")
    message = EmailMessage()
    message.set_content("Тест")
    attach_media(message, store, ref)
    with pytest.raises(DomainError):
        attach_media(message, store, ref)


def test_unsupported_smtp_size_does_not_invent_zero_limit():
    message = EmailMessage()
    message.set_content("Тест")
    for advertised in (None, "", "0"):
        assert checked_email_bytes(message, limit=10000, smtp_size=advertised)


def test_missing_asset_reparse_constructor_fails_closed(tmp_path, monkeypatch):
    root = tmp_path / "media"
    monkeypatch.setattr("sunrise_infrastructure.broadcast_media._reparse", lambda path: path == tmp_path)
    with pytest.raises(DomainError):
        BroadcastMediaStore(root, signing_key=b"x" * 32)
    assert not root.exists()


def test_failed_durable_write_leaves_no_asset(store, monkeypatch):
    def fail(descriptor):
        raise OSError("synthetic disk failure")

    monkeypatch.setattr("sunrise_infrastructure.broadcast_media.os.fsync", fail)
    with pytest.raises(DomainError) as error:
        store.upload([picture()], mime="image/png")
    assert error.value.code == "media_unavailable"
    assert not list(store.root.iterdir())


def test_collision_does_not_overwrite_existing_asset(store, monkeypatch):
    ref = store.upload([picture()], mime="image/png")
    from uuid import UUID

    monkeypatch.setattr("sunrise_infrastructure.broadcast_media.uuid4", lambda: UUID(ref["asset_id"]))
    before = store.read(ref)
    with pytest.raises(DomainError):
        store.upload([picture("JPEG")], mime="image/jpeg")
    assert store.read(ref) == before


def test_reference_shape_is_checked_before_filesystem_io(store, monkeypatch):
    def fail(path):
        pytest.fail("invalid reference must not access filesystem")

    monkeypatch.setattr("sunrise_infrastructure.broadcast_media._safe_components", fail)
    for ref in (None, [], {"asset_id": "../secret"}):
        with pytest.raises(DomainError) as error:
            store.read(ref)
        assert error.value.code == "media_invalid"


def test_image_dimensions_checked_before_decoding(store, monkeypatch):
    def fail(image, *args, **kwargs):
        pytest.fail("disallowed dimensions must not decode")

    content = picture(size=(100, 2))
    monkeypatch.setattr("PIL.PngImagePlugin.PngImageFile.load", fail)
    with pytest.raises(DomainError):
        store.upload([content], mime="image/png")


def test_canonical_postencode_limit_enforced(store):
    # Palette compresses very small; canonical RGB output is larger.
    buffer = io.BytesIO()
    Image.new("P", (64, 64)).save(buffer, format="PNG")
    source = buffer.getvalue()
    store.limits = MediaLimits(photo_bytes=len(source))
    with pytest.raises(DomainError) as error:
        store.upload([source], mime="image/png")
    assert error.value.code == "media_too_large"
    assert not list(store.root.iterdir())


def test_many_mp4_boxes_are_bounded(store):
    content = video() + box(b"free", b"") * 4097
    with pytest.raises(DomainError):
        store.upload([content], mime="video/mp4")


@pytest.mark.parametrize("orientation", [6, 8])
def test_jpeg_exif_rotation_preserves_pixel_placement(store, orientation):
    image = Image.new("RGB", (40, 20), "red")
    image.paste(Image.new("RGB", (20, 20), "blue"), (20, 0))
    exif = Image.Exif()
    exif[274] = orientation
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=100, subsampling=0, exif=exif)
    ref = store.upload([buffer.getvalue()], mime="image/jpeg")
    with Image.open(io.BytesIO(store.read(ref))) as normalized:
        assert normalized.size == (20, 40)
        assert not normalized.getexif()
        top, bottom = normalized.getpixel((10, 5)), normalized.getpixel((10, 35))
        red, blue = (top, bottom) if orientation == 6 else (bottom, top)
        assert red[0] > 200 and red[2] < 30
        assert blue[2] > 200 and blue[0] < 30


def test_real_synthetic_chromium_fragmented_mp4_accepted(store):
    content = (Path(__file__).parents[1] / "fixtures" / "broadcast-chromium-synthetic.mp4").read_bytes()
    ref = store.upload([content], mime="video/mp4")
    assert store.read(ref) == content


def test_standard_inert_mp4_metadata_accepted(store):
    content = video()
    offset = content.index(b"moov") - 4
    old_size = struct.unpack_from(">I", content, offset)[0]
    name = box(b"\xa9nam", box(b"data", b"\x00\x00\x00\x01\x00\x00\x00\x00Synthetic test"))
    metadata = box(b"udta", box(b"meta", b"\x00" * 4 + box(b"ilst", name)))
    content = (
        content[:offset]
        + struct.pack(">I", old_size + len(metadata))
        + content[offset + 4 : offset + old_size]
        + metadata
        + content[offset + old_size :]
    )
    ref = store.upload([content], mime="video/mp4")
    assert store.read(ref) == content


@pytest.mark.parametrize(
    "mutation", ["unknown_track", "external_base_offset", "run_count", "truncated_mfra", "external_dref"]
)
def test_real_fragmented_mp4_negative_boundaries(store, mutation):
    content = (Path(__file__).parents[1] / "fixtures" / "broadcast-chromium-synthetic.mp4").read_bytes()
    if mutation == "unknown_track":
        offset = content.index(b"tfhd") + 8
        content = content[:offset] + struct.pack(">I", 999) + content[offset + 4 :]
    elif mutation == "external_base_offset":
        offset = content.index(b"tfhd") + 4
        content = content[:offset] + b"\x00\x02\x00\x21" + content[offset + 4 :]
    elif mutation == "run_count":
        offset = content.index(b"trun") + 8
        content = content[:offset] + struct.pack(">I", 1_000_001) + content[offset + 4 :]
    elif mutation == "external_dref":
        offset = content.index(b"url ") + 4
        content = content[:offset] + b"\x00" * 4 + content[offset + 4 :]
    else:
        content = content[:-1]
    with pytest.raises(DomainError):
        store.upload([content], mime="video/mp4")
