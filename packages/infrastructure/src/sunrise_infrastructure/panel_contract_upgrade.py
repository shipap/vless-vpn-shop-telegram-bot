"""Явная офлайн-проверка расширения одной подтверждённой тестовой UUID-привязки.

Не вызывается из runtime: координатор сохраняет возвращённую копию и аудит
в той же транзакции после отдельного разрешения и резервной копии.
"""

import hashlib
import hmac
import json
import re
from copy import deepcopy
from datetime import datetime
from uuid import UUID

from sqlalchemy import select, text
from sunrise_core.errors import DomainError

from .db import Account, Job, Setting, Subscription, now
from .panel import registered_uuid_owner


def denied():
    return DomainError(
        "panel_contract_upgrade_denied", "Происхождение тестовой привязки требует проверки.", 409
    )


def binding_record_digest(value: dict) -> str:
    if not isinstance(value, dict):
        raise denied()
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (ValueError, TypeError):
        raise denied() from None
    return hashlib.sha256(encoded.encode()).hexdigest()


def prepare_legacy_uuid_contract_upgrade(
    session, subscription_id: str, panel, *, expected_record_digest: str, provision_job_id: str
) -> dict:
    """Возвращает contract-only копию; не изменяет ORM, базу, адаптер или панель."""
    if session.new or session.dirty or session.deleted:
        raise denied()
    with session.no_autoflush:
        return _prepare_clean(
            session,
            subscription_id,
            panel,
            expected_record_digest=expected_record_digest,
            provision_job_id=provision_job_id,
        )


def _prepare_clean(session, subscription_id, panel, *, expected_record_digest, provision_job_id):
    if (
        not isinstance(subscription_id, str)
        or not 1 <= len(subscription_id) <= 64
        or not isinstance(provision_job_id, str)
        or not 1 <= len(provision_job_id) <= 64
        or not isinstance(expected_record_digest, str)
        or not re.fullmatch(r"[a-f0-9]{64}", expected_record_digest)
        or getattr(panel, "contract", None) != "2.8_uuid"
    ):
        raise denied()
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": "sunrise:panel:" + subscription_id},
    )
    prior = session.get(Subscription, subscription_id)
    if not prior:
        raise denied()
    owner_id = prior.account_id
    account = session.scalar(
        select(Account)
        .where(Account.id == owner_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    sub = session.scalar(
        select(Subscription)
        .where(Subscription.id == subscription_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    record = session.scalar(
        select(Setting)
        .where(Setting.key == "panel_test:" + subscription_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if not record or not isinstance(record.value, dict):
        raise denied()
    value = deepcopy(record.value)
    if "contract" in value or not hmac.compare_digest(binding_record_digest(value), expected_record_digest):
        raise denied()
    candidate = value | {"contract": "2.8_uuid"}
    binding = value.get("binding")
    if (
        not account
        or sub.environment not in {"local_test", "sandbox"}
        or sub.account_id != owner_id
        or account.legacy_consent
        or str(account.telegram_id) != value.get("telegram_id")
        or not registered_uuid_owner(session, candidate, sub, account, panel)
        or sub.panel_user_id is not None
        or not isinstance(binding, dict)
        or set(binding) != {"uuid", "short_uuid", "username"}
        or sub.panel_uuid != binding.get("uuid")
        or sub.short_uuid != binding.get("short_uuid")
        or not sub.connection_url
        or sub.connection_url != value.get("connection_url")
        or not panel._safe_connection_url(sub.connection_url, automatic=True)
    ):
        raise denied()
    try:
        identity = UUID(binding["uuid"])
        valid = (
            str(identity) == binding["uuid"]
            and identity.version == 4
            and binding["username"] == "sunrise_test_" + identity.hex[:20]
            and isinstance(binding["short_uuid"], str)
            and re.fullmatch(r"[a-f0-9]{24}", binding["short_uuid"])
            and binding["uuid"] not in panel.uuids | panel.squads | panel.external_squads
        )
    except (TypeError, ValueError, AttributeError):
        valid = False
    if not valid:
        raise denied()
    jobs = list(
        session.scalars(
            select(Job)
            .where(
                Job.kind == "provision",
                Job.state == "done",
                Job.last_error.is_(None),
                Job.payload["subscription_id"].as_string() == subscription_id,
                Job.payload["panel_create"].as_boolean().is_(True),
                Job.payload["create_started"].as_boolean().is_(True),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    if len(jobs) != 1 or jobs[0].id != provision_job_id:
        raise denied()
    job = jobs[0]
    payload = job.payload
    if (
        job.account_id != sub.account_id
        or job.attempts < 1
        or payload.get("panel_create") is not True
        or payload.get("create_started") is not True
        or payload.get("environment") != sub.environment
        or payload.get("panel_binding") != binding
        or any(key.startswith("legacy_") for key in payload)
        or "connection_url" in payload
        and payload["connection_url"] != sub.connection_url
    ):
        raise denied()
    activation = session.scalar(
        select(Setting)
        .where(Setting.key == "panel_test_activation:" + panel.auto_scope)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if not activation or not isinstance(activation.value, dict):
        raise denied()
    historical = activation.value.get("historical_job_ids")
    try:
        stamp = datetime.fromisoformat(activation.value.get("activated_at"))
        valid_stamp = stamp.tzinfo is not None and stamp.utcoffset() is not None and stamp <= now()
    except (ValueError, TypeError):
        valid_stamp = False
    if (
        not valid_stamp
        or not isinstance(historical, list)
        or any(not isinstance(identifier, str) or not identifier for identifier in historical)
        or provision_job_id in historical
    ):
        raise denied()
    return candidate
