"""Закрытые локальные медиа рассылок. Авторизацию владельца проверяет вызывающий сервис."""

import hashlib
import hmac
import io
import json
import os
import re
import stat
import struct
import warnings
from dataclasses import dataclass
from email import policy
from pathlib import Path
from uuid import uuid4

from PIL import Image, ImageOps, UnidentifiedImageError
from sunrise_core.errors import DomainError

MIMES = {"image/png": ("PNG", "png"), "image/jpeg": ("JPEG", "jpg"), "video/mp4": (None, "mp4")}
REFERENCE_FIELDS = {"asset_id", "sha256", "mime", "size_bytes", "proof"}
PROOF_DOMAIN = b"broadcast-media:v1\x00"


@dataclass(frozen=True)
class MediaLimits:
    photo_bytes: int = 2_000_000
    video_bytes: int = 8_000_000
    email_bytes: int = 12_000_000


def _invalid():
    return DomainError("media_invalid", "Загрузите корректное фото JPEG/PNG или видео MP4.", 400)


def _unavailable():
    return DomainError("media_unavailable", "Файл рассылки недоступен или изменён. Проверьте загрузку.", 409)


def _reparse(path):
    value = path.lstat()
    return stat.S_ISLNK(value.st_mode) or bool(
        getattr(value, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def _safe_components(path):
    for component in [*reversed(path.parents), path]:
        if _reparse(component):
            raise _unavailable()


def _photo(content, mime, cap):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as image:
                width, height = image.size
                if (
                    image.format != MIMES[mime][0]
                    or width <= 0
                    or height <= 0
                    or width * height > 16_000_000
                    or width + height > 10_000
                    or max(width, height) > min(width, height) * 20
                    or getattr(image, "n_frames", 1) != 1
                ):
                    raise _invalid()
                image.verify()
            with Image.open(io.BytesIO(content)) as image:
                image.load()
                image = ImageOps.exif_transpose(image)
                # Новый bitmap исключает EXIF, текстовые chunks и хвост исходного файла.
                normalized = Image.new("RGB", image.size)
                normalized.paste(image.convert("RGB"))
                buffer = io.BytesIO()
                normalized.save(buffer, format=MIMES[mime][0])
                result = buffer.getvalue()
                if len(result) > cap:
                    raise DomainError(
                        "media_too_large", "Фото после обработки превышает лимит загрузки.", 413
                    )
                return result
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        raise _invalid() from None


def _mp4(content):
    """Проверка контейнера, не декодера/антивируса: воспроизведение кодека не подтверждено."""
    count = 0

    def boxes(data, depth=0):
        nonlocal count
        if depth > 8:
            raise _invalid()
        offset, result = 0, []
        while offset < len(data):
            count += 1
            if count > 4096 or len(data) - offset < 8:
                raise _invalid()
            size, kind = struct.unpack_from(">I4s", data, offset)
            header = 8
            if size == 1:
                if len(data) - offset < 16:
                    raise _invalid()
                size = struct.unpack_from(">Q", data, offset + 8)[0]
                header = 16
            if size < header or size > len(data) - offset:
                raise _invalid()
            result.append((kind, data[offset + header : offset + size]))
            offset += size
        return result

    def single(items, kind):
        found = [data for name, data in items if name == kind]
        if len(found) != 1:
            raise _invalid()
        return found[0]

    def metadata(data, kind, depth):
        if kind == b"meta":
            if len(data) < 4 or bytes(data[:4]) != b"\x00" * 4:
                raise _invalid()
            data = data[4:]
        for child, payload in boxes(data, depth):
            if child in {b"meta", b"ilst"}:
                metadata(payload, child, depth + 1)
            elif kind == b"ilst" and child in {
                b"\xa9nam",
                b"\xa9ART",
                b"\xa9alb",
                b"\xa9day",
                b"\xa9cmt",
                b"\xa9too",
                b"\xa9gen",
                b"aART",
                b"trkn",
                b"disk",
                b"covr",
                b"cpil",
                b"pgap",
                b"tmpo",
                b"----",
            }:
                fields = boxes(payload, depth + 1)
                if any(k not in {b"data", b"mean", b"name"} or len(v) < 4 for k, v in fields):
                    raise _invalid()
            elif child not in {b"hdlr", b"free", b"\xa9nam", b"\xa9too", b"\xa9cmt"}:
                raise _invalid()

    top = boxes(memoryview(content))
    if (
        not top
        or top[0][0] != b"ftyp"
        or any(
            kind not in {b"ftyp", b"moov", b"moof", b"mdat", b"mfra", b"free", b"skip", b"wide"}
            for kind, _ in top
        )
    ):
        raise _invalid()
    ftyp = single(top, b"ftyp")
    if len(ftyp) < 8 or len(ftyp) % 4 or bytes(ftyp[:4]) not in {b"isom", b"iso2", b"mp41", b"mp42", b"avc1"}:
        raise _invalid()
    if not any(kind == b"mdat" and len(data) > 0 for kind, data in top):
        raise _invalid()
    movie = boxes(single(top, b"moov"), 1)
    if any(kind not in {b"mvhd", b"trak", b"mvex", b"udta", b"meta", b"free"} for kind, _ in movie):
        raise _invalid()
    for kind, payload in movie:
        if kind in {b"udta", b"meta"}:
            metadata(payload, kind, 2)
    tracks = [data for kind, data in movie if kind == b"trak"]
    if not 1 <= len(tracks) <= 8:
        raise _invalid()
    has_video = False
    track_ids = {}
    for track in tracks:
        track_boxes = boxes(track, 2)
        headers = [payload for kind, payload in track_boxes if kind == b"tkhd"]
        if headers:
            header = single(track_boxes, b"tkhd")
            if not header or header[0] not in {0, 1}:
                raise _invalid()
            id_offset = 20 if header[0] else 12
            if len(header) < id_offset + 4:
                raise _invalid()
            identifier = struct.unpack_from(">I", header, id_offset)[0]
            if not identifier or identifier in track_ids:
                raise _invalid()
            track_ids[identifier] = None
        else:
            identifier = None
        if any(kind not in {b"tkhd", b"mdia", b"edts", b"free"} for kind, _ in track_boxes):
            raise _invalid()
        # Edit lists не могут содержать внешние источники, но структура всё равно ограничена.
        for kind, data in track_boxes:
            if kind == b"edts" and any(k != b"elst" for k, _ in boxes(data, 3)):
                raise _invalid()
        media = boxes(single(track_boxes, b"mdia"), 3)
        if any(kind not in {b"mdhd", b"hdlr", b"minf", b"free"} for kind, _ in media):
            raise _invalid()
        handler = single(media, b"hdlr")
        if len(handler) < 24 or bytes(handler[8:12]) not in {b"vide", b"soun"}:
            raise _invalid()
        video_track = bytes(handler[8:12]) == b"vide"
        has_video |= video_track
        info = boxes(single(media, b"minf"), 4)
        if any(kind not in {b"vmhd", b"smhd", b"dinf", b"stbl", b"free"} for kind, _ in info):
            raise _invalid()
        dinf = boxes(single(info, b"dinf"), 5)
        if len(dinf) != 1:
            raise _invalid()
        dref = single(dinf, b"dref")
        if len(dref) < 8 or bytes(dref[:4]) != b"\x00" * 4:
            raise _invalid()
        references = boxes(dref[8:], 6)
        if len(references) != struct.unpack_from(">I", dref, 4)[0] or not references:
            raise _invalid()
        if any(
            kind != b"url " or bytes(data) not in {b"\x00\x00\x00\x01", b"\x00\x00\x00\x01\x00"}
            for kind, data in references
        ):
            raise _invalid()
        single(info, b"stbl")
        # shortcut: проверяем структуру контейнера и self-contained dref; полная проверка
        # кодека требует отдельно согласованного изолированного декодера.
        for kind, data in info:
            if kind == b"stbl":
                samples = boxes(data, 5)
                if any(
                    k
                    not in {
                        b"stsd",
                        b"stts",
                        b"ctts",
                        b"stsc",
                        b"stsz",
                        b"stco",
                        b"co64",
                        b"stss",
                        b"sdtp",
                        b"sgpd",
                        b"sbgp",
                    }
                    for k, _ in samples
                ):
                    raise _invalid()
                description = single(samples, b"stsd")
                if len(description) < 8 or bytes(description[:4]) != b"\x00" * 4:
                    raise _invalid()
                entries = boxes(description[8:], 6)
                if len(entries) != struct.unpack_from(">I", description, 4)[0] or not entries:
                    raise _invalid()
                if identifier is not None:
                    track_ids[identifier] = len(entries)
                for sample_kind, sample_data in entries:
                    allowed_samples = {b"avc1", b"avc3", b"hvc1", b"hev1"} if video_track else {b"mp4a"}
                    header = 78 if video_track else 28
                    if sample_kind not in allowed_samples or len(sample_data) < header:
                        raise _invalid()
                    if not 1 <= struct.unpack_from(">H", sample_data, 6)[0] <= len(references):
                        raise _invalid()
                    if video_track and 0 in struct.unpack_from(">HH", sample_data, 24):
                        raise _invalid()
                    config = boxes(sample_data[header:], 7)
                    if any(
                        k not in {b"avcC", b"hvcC", b"esds", b"btrt", b"pasp", b"colr", b"clap"}
                        for k, _ in config
                    ):
                        raise _invalid()
                    required = (
                        b"avcC" if sample_kind in {b"avc1", b"avc3"} else b"hvcC" if video_track else b"esds"
                    )
                    codec = single(config, required)
                    if len(codec) < (7 if required == b"avcC" else 23 if required == b"hvcC" else 4):
                        raise _invalid()
                    if video_track and codec[0] != 1:
                        raise _invalid()
    if not has_video:
        raise _invalid()

    fragments = [data for kind, data in top if kind == b"moof"]
    extensions = [data for kind, data in movie if kind == b"mvex"]
    defaults = {}
    if fragments:
        if len(track_ids) != len(tracks) or len(extensions) != 1:
            raise _invalid()
        for kind, data in boxes(extensions[0], 2):
            if kind == b"mehd":
                if len(data) not in {8, 12} or data[0] not in {0, 1}:
                    raise _invalid()
                continue
            if kind != b"trex" or len(data) != 24 or bytes(data[:4]) != b"\x00" * 4:
                raise _invalid()
            track_id, sample_index, _, sample_size, _ = struct.unpack_from(">IIIII", data, 4)
            if (
                track_id not in track_ids
                or track_id in defaults
                or not 1 <= sample_index <= track_ids[track_id]
            ):
                raise _invalid()
            defaults[track_id] = sample_size
        if set(defaults) != set(track_ids):
            raise _invalid()
    for fragment in fragments:
        children = boxes(fragment, 1)
        header = single(children, b"mfhd")
        if (
            len(header) != 8
            or bytes(header[:4]) != b"\x00" * 4
            or any(k not in {b"mfhd", b"traf"} for k, _ in children)
        ):
            raise _invalid()
        trafs = [data for kind, data in children if kind == b"traf"]
        if not 1 <= len(trafs) <= len(track_ids):
            raise _invalid()
        for traf in trafs:
            children = boxes(traf, 2)
            if any(k not in {b"tfhd", b"tfdt", b"trun"} for k, _ in children):
                raise _invalid()
            header = single(children, b"tfhd")
            if len(header) < 8 or header[0] != 0:
                raise _invalid()
            flags = int.from_bytes(header[1:4])
            track_id = struct.unpack_from(">I", header, 4)[0]
            # Только movie-fragment relative addressing; абсолютные data-offset не принимаются.
            if track_id not in defaults or flags & ~0x02003A or flags & 1 or not flags & 0x020000:
                raise _invalid()
            expected = 8 + 4 * sum(bool(flags & value) for value in (2, 8, 16, 32))
            if len(header) != expected:
                raise _invalid()
            if flags & 2 and not 1 <= struct.unpack_from(">I", header, 8)[0] <= track_ids[track_id]:
                raise _invalid()
            decode = single(children, b"tfdt")
            if (
                len(decode) not in {8, 12}
                or decode[0] not in {0, 1}
                or len(decode) != (12 if decode[0] else 8)
            ):
                raise _invalid()
            runs = [data for kind, data in children if kind == b"trun"]
            if not runs:
                raise _invalid()
            for run in runs:
                if len(run) < 8 or run[0] not in {0, 1}:
                    raise _invalid()
                flags = int.from_bytes(run[1:4])
                sample_count = struct.unpack_from(">I", run, 4)[0]
                if flags & ~0xF05 or not 1 <= sample_count <= 1_000_000 or not flags & 1:
                    raise _invalid()
                expected = (
                    8
                    + 4 * sum(bool(flags & value) for value in (1, 4))
                    + 4 * sample_count * sum(bool(flags & value) for value in (0x100, 0x200, 0x400, 0x800))
                )
                if len(run) != expected or struct.unpack_from(">i", run, 8)[0] < 0:
                    raise _invalid()
    for kind, payload in top:
        if kind != b"mfra":
            continue
        children = boxes(payload, 1)
        offset = single(children, b"mfro")
        if (
            len(offset) != 8
            or bytes(offset[:4]) != b"\x00" * 4
            or any(k not in {b"tfra", b"mfro"} for k, _ in children)
        ):
            raise _invalid()
        for key, data in children:
            if key != b"tfra":
                continue
            if (
                len(data) < 16
                or data[0] not in {0, 1}
                or struct.unpack_from(">I", data, 4)[0] not in track_ids
            ):
                raise _invalid()
            lengths = struct.unpack_from(">I", data, 8)[0]
            entries = struct.unpack_from(">I", data, 12)[0]
            width = (16 if data[0] else 8) + sum(((lengths >> shift) & 3) + 1 for shift in (0, 2, 4))
            if lengths >> 6 or entries > 4096 or len(data) != 16 + entries * width:
                raise _invalid()
    return content


class BroadcastMediaStore:
    def __init__(self, root, *, signing_key, limits=None):
        self.limits = limits or MediaLimits()
        if (
            not isinstance(signing_key, bytes)
            or len(signing_key) < 32
            or any(
                type(value) is not int or value <= 0
                for value in (self.limits.photo_bytes, self.limits.video_bytes, self.limits.email_bytes)
            )
        ):
            raise ValueError("media_configuration_invalid")
        self._signing_key = signing_key
        self.root = Path(os.path.abspath(root))
        try:
            for component in [*reversed(self.root.parents), self.root]:
                if component.exists() or component.is_symlink():
                    if _reparse(component) or not component.is_dir():
                        raise _unavailable()
                else:
                    component.mkdir(mode=0o700)
            _safe_components(self.root)
        except OSError:
            raise _unavailable() from None

    def _cap(self, mime):
        return self.limits.video_bytes if mime == "video/mp4" else self.limits.photo_bytes

    def _proof(self, reference):
        payload = {key: reference[key] for key in sorted(REFERENCE_FIELDS - {"proof"})}
        return hmac.new(
            self._signing_key,
            PROOF_DOMAIN
            + json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(),
            hashlib.sha256,
        ).hexdigest()

    def _validate(self, reference):
        if (
            not isinstance(reference, dict)
            or set(reference) != REFERENCE_FIELDS
            or not isinstance(reference.get("mime"), str)
            or reference["mime"] not in MIMES
            or type(reference.get("size_bytes")) is not int
            or not 0 < reference["size_bytes"] <= self._cap(reference["mime"])
            or any(
                not isinstance(reference.get(key), str)
                or not re.fullmatch(r"[0-9a-f]{" + str(length) + "}", reference[key])
                for key, length in (("asset_id", 32), ("sha256", 64), ("proof", 64))
            )
            or not hmac.compare_digest(reference["proof"], self._proof(reference))
        ):
            raise _invalid()

    def upload(self, chunks, *, mime):
        if not isinstance(mime, str) or mime not in MIMES:
            raise _invalid()
        cap = self._cap(mime)
        content = bytearray()
        for chunk in chunks:
            if not isinstance(chunk, bytes):
                raise _invalid()
            if len(content) + len(chunk) > cap:
                raise DomainError("media_too_large", "Файл превышает допустимый размер загрузки.", 413)
            content.extend(chunk)
        if not content:
            raise _invalid()
        content = _mp4(bytes(content)) if mime == "video/mp4" else _photo(bytes(content), mime, cap)
        reference = {
            "asset_id": uuid4().hex,
            "sha256": hashlib.sha256(content).hexdigest(),
            "mime": mime,
            "size_bytes": len(content),
        }
        reference["proof"] = self._proof(reference)
        path = self.root / reference["asset_id"]
        created = False
        try:
            _safe_components(self.root)
            descriptor = os.open(
                path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
            )
            created = True
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            # Ссылка одобряется только после полного reread/digest, а не создания имени.
            self.read(reference)
            return reference
        except (OSError, DomainError):
            if created:
                try:
                    _safe_components(self.root)
                    if not _reparse(path):
                        path.unlink()
                except (OSError, DomainError):
                    pass
            raise _unavailable() from None

    def read(self, reference):
        self._validate(reference)
        path = self.root / reference["asset_id"]
        try:
            _safe_components(path)
            before = path.lstat()
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_size != reference["size_bytes"]
            ):
                raise _unavailable()
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    raise _unavailable()
                content = stream.read(reference["size_bytes"] + 1)
            _safe_components(path)
            if (
                len(content) != reference["size_bytes"]
                or hashlib.sha256(content).hexdigest() != reference["sha256"]
            ):
                raise _unavailable()
            return content
        except OSError:
            raise _unavailable() from None


def attach_media(message, store, reference):
    if list(message.iter_attachments()):
        raise _invalid()
    content = store.read(reference)
    major, minor = reference["mime"].split("/", 1)
    message.add_attachment(
        content,
        maintype=major,
        subtype=minor,
        filename="media-" + reference["asset_id"] + "." + MIMES[reference["mime"]][1],
        cte="base64",
    )


def checked_email_bytes(message, *, limit, smtp_size=None):
    if type(limit) is not int or limit <= 0:
        raise ValueError("media_email_limit_invalid")
    if smtp_size not in (None, ""):
        if not isinstance(smtp_size, str) or not re.fullmatch(r"[0-9]{1,20}", smtp_size):
            raise DomainError(
                "media_email_size_unknown", "Не удалось проверить лимит почтового сервера.", 503
            )
        if int(smtp_size) > 0:
            limit = min(limit, int(smtp_size))
    wire = message.as_bytes(policy=policy.SMTP)
    if len(wire) > limit:
        raise DomainError("media_email_too_large", "Письмо с вложением превышает допустимый размер.", 413)
    return wire
