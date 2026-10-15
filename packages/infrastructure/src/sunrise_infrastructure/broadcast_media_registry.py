"""Private media references require current staff and database-scoped registration."""

import hashlib
import hmac
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError
from sqlalchemy import func, select
from sunrise_core.errors import DomainError, missing

from .broadcast_media import BroadcastMediaStore
from .db import Account, Alias, Audit, Job, Setting
from .staff_permissions import StaffPermissions


class MediaReference(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    asset_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    mime: Literal["image/jpeg", "image/png", "video/mp4"]
    size_bytes: StrictInt = Field(gt=0, le=8_000_000)
    proof: str = Field(pattern=r"^[a-f0-9]{64}$")


class MediaAsset(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: StrictInt = Field(default=1, ge=1, le=1)
    owner_id: str = Field(pattern=r"^[1-9][0-9]{0,18}$")
    scope_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    media: MediaReference


class NotificationMedia(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: StrictInt = Field(default=1, ge=1, le=1)
    notification_id: str
    job_id: str
    media: MediaReference


def configured_media_store(settings):
    secret = getattr(settings, "secret_key", None)
    if not isinstance(secret, str) or len(secret.encode()) < 32:
        raise DomainError("media_unavailable", "Хранилище файлов не настроено.", 503)
    key = hmac.new(secret.encode(), b"sunrise:broadcast-media:v1", hashlib.sha256).digest()
    root = Path(__file__).resolve().parents[4] / ".private" / "broadcast-media"
    return BroadcastMediaStore(root, signing_key=key)


class BroadcastMediaRegistry:
    def __init__(self, commerce, settings, *, store=None):
        self.commerce, self.db, self.settings = commerce, commerce.db, settings
        self.store = store or configured_media_store(settings)
        self.staff = StaffPermissions(commerce.accounts or SimpleNamespace(db=self.db, settings=settings))

    def _scope(self, session):
        environment = "local_test" if getattr(self.settings, "local_test", False) else "sandbox"
        schema = session.scalar(select(func.current_schema()))
        return hashlib.sha256(f"{self.db.engine.url.database}:{schema}:{environment}".encode()).hexdigest()

    @staticmethod
    def _reference(value):
        try:
            return MediaReference.model_validate(value).model_dump()
        except ValidationError:
            raise DomainError("media_invalid", "Проверьте загруженный файл.", 400) from None

    def _owner(self, session, identifier):
        alias = session.get(Alias, int(identifier), populate_existing=True)
        return alias.canonical_id if alias else int(identifier)

    def register(self, actor, chunks, mime):
        with self.db.transaction() as session:
            account = self.staff.require_in(session, actor, "broadcasts_write", bot=True)
            reference = self.store.upload(chunks, mime=mime)
            session.add(
                Setting(
                    key="media_asset:" + reference["asset_id"],
                    value=MediaAsset(
                        owner_id=str(account.id),
                        scope_sha256=self._scope(session),
                        media=reference,
                    ).model_dump(),
                )
            )
            session.add(
                Audit(
                    actor_id=account.id,
                    action="broadcast_media_upload",
                    target=reference["asset_id"],
                    reason="Загрузка файла рассылки",
                    details={"mime": mime, "size_bytes": reference["size_bytes"]},
                )
            )
            return reference

    def reference_in(self, session, actor, value, *, lock_actor=True):
        reference = self._reference(value)
        if lock_actor:
            account = self.staff.require_in(session, actor, "broadcasts_write", bot=True)
        else:
            account = session.get(Account, self._owner(session, actor), populate_existing=True)
            if not account or "broadcasts_write" not in self.staff.capabilities_in(
                session, account, bot=True
            ):
                raise missing()
        row = session.get(Setting, "media_asset:" + reference["asset_id"])
        try:
            asset = MediaAsset.model_validate(row.value if row else None)
            if (
                self._owner(session, asset.owner_id) != account.id
                or asset.scope_sha256 != self._scope(session)
                or asset.media.model_dump() != reference
            ):
                raise ValueError()
        except (ValidationError, ValueError, TypeError):
            raise missing() from None
        self.store.read(reference)
        return reference

    def resolve(self, actor, value):
        with self.db.transaction() as session:
            reference = self.reference_in(session, actor, value)
            return {"reference": reference, "bytes": self.store.read(reference)}

    def metadata(self, actor, asset_id):
        with self.db.transaction() as session:
            self.staff.require_in(session, actor, "broadcasts_write", bot=True)
            row = session.get(Setting, "media_asset:" + asset_id)
            try:
                asset = MediaAsset.model_validate(row.value if row else None)
            except ValidationError:
                raise missing() from None
            return self.reference_in(session, actor, asset.media.model_dump())

    def bind_in(self, session, notification_id, job):
        reference = self.reference_in(session, job.account_id, job.payload["media"], lock_actor=False)
        key = "notification_media:" + notification_id
        expected = NotificationMedia(
            notification_id=notification_id, job_id=job.id, media=reference
        ).model_dump()
        row = session.get(Setting, key)
        if row and row.value != expected:
            raise missing()
        if row is None:
            session.add(Setting(key=key, value=expected))

    def notification_in(self, session, item):
        row = session.get(Setting, "notification_media:" + item.id)
        if row is None:
            job = (
                session.get(Job, item.event_key[len("broadcast:") :])
                if item.event_key.startswith("broadcast:")
                else None
            )
            if item.kind == "broadcast" and job is not None and job.payload.get("media") is not None:
                raise missing()
            return None
        try:
            marker = NotificationMedia.model_validate(row.value)
            job = session.get(Job, marker.job_id)
            if (
                marker.notification_id != item.id
                or item.kind != "broadcast"
                or job is None
                or job.kind != "broadcast"
                or job.state != "done"
                or job.payload.get("source") not in {"unified", "media_self"}
                or item.event_key != "broadcast:" + job.id
                or job.payload.get("media") != marker.media.model_dump()
                or str(item.account_id) not in job.payload.get("account_ids", [])
            ):
                raise ValueError()
            return self.reference_in(session, job.account_id, marker.media.model_dump(), lock_actor=False)
        except (ValidationError, ValueError, TypeError):
            raise missing() from None


def notification_media(db, settings, item, session):
    if session.get(Setting, "notification_media:" + item.id) is None:
        job = (
            session.get(Job, item.event_key[len("broadcast:") :])
            if item.event_key.startswith("broadcast:")
            else None
        )
        if job is None or job.payload.get("media") is None:
            return None
    return BroadcastMediaRegistry(SimpleNamespace(db=db, accounts=None), settings).notification_in(
        session, item
    )
