"""Отмена подтверждает отсутствие начатой внешней операции, а не её успех."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sunrise_core.errors import DomainError
from sunrise_infrastructure import customer_operations as operations
from sunrise_infrastructure.db import Audit, Job, Setting


@pytest.fixture
def cancellation():
    sub = SimpleNamespace(id="synthetic-sub", account_id=1, environment="live", version=2)
    binding = {"scope_id": "synthetic", "user_id": "8"}
    job = Job(
        id="synthetic-job",
        key="credential-rotate:synthetic-sub:original",
        kind="credential_rotate",
        account_id=1,
        attempts=0,
        state="blocked",
        last_error="panel_target_denied",
        payload={
            "subscription_id": sub.id,
            "environment": "live",
            "scope_id": "synthetic",
            "version": 2,
            "revoke_only_passwords": False,
            "panel_binding": binding,
        },
    )
    original = Setting(
        key="customer_op:1:original",
        value={
            "request": {
                "kind": "credential_rotate",
                "subscription_id": sub.id,
                "revoke_only_passwords": False,
            },
            "result": {"status": "rotate_pending", "job_id": job.id},
        },
    )
    rows = {job.id: job, original.key: original}
    added = []
    session = SimpleNamespace(
        get=lambda model, key, **kw: rows.get(key),
        scalar=lambda query: job,
        add=lambda row: (
            added.append(row),
            rows.update({row.key: row}) if isinstance(row, Setting) else None,
        ),
    )

    @contextmanager
    def transaction():
        yield session

    account = SimpleNamespace(id=1)
    commerce = SimpleNamespace(
        db=SimpleNamespace(transaction=transaction), _account=lambda *args: account, _sub=lambda *args: sub
    )
    service = operations.CustomerOperations(commerce)
    service._guards = lambda *args: None
    service._before_queue = lambda *args: None
    service._binding = lambda *args: binding
    return service, session, job, sub, binding, original, added


def test_cancel_never_started_rotation_is_audited_and_both_keys_replay_truthfully(cancellation):
    service, session, job, sub, _, original, added = cancellation
    result = service.cancel_rotation(1, sub.id, job.id, "cancel-key", confirmed=True)
    assert result == {"status": "rotate_canceled", "job_id": job.id}
    assert job.state == "canceled" and job.attempts == 0
    assert job.last_error == "customer_canceled_before_start"
    assert original.value["result"] == result
    assert service.cancel_rotation(1, sub.id, job.id, "cancel-key", confirmed=True) == result
    assert len([row for row in added if isinstance(row, Audit)]) == 1
    assert (
        service._prior(
            session,
            SimpleNamespace(id=1),
            "cancel-key",
            {"kind": "credential_rotate_cancel", "subscription_id": sub.id, "job_id": job.id},
        )[1]
        == result
    )


def test_original_blocked_rotation_key_reports_review_instead_of_pending(cancellation):
    service, session, job, _, _, original, _ = cancellation
    session.get = lambda model, key, **kw: job if model is Job else original
    assert service._prior(session, SimpleNamespace(id=1), "original-key", original.value["request"])[1] == {
        "status": "review_required",
        "job_id": job.id,
    }


@pytest.mark.parametrize(
    "changed",
    [
        "attempts",
        "lease",
        "rotate_started",
        "before_short_uuid",
        "before_sub_revoked_at",
        "before_expires_at",
        "next_short_uuid",
        "owner",
        "version",
        "environment",
        "scope_id",
        "binding",
        "state",
        "error",
        "kind",
        "confirmed",
    ],
)
def test_cancel_refuses_uncertain_or_changed_rotation_without_changes(cancellation, changed):
    service, _, job, sub, _, original, added = cancellation
    if changed in {
        "lease",
        "rotate_started",
        "before_short_uuid",
        "before_sub_revoked_at",
        "before_expires_at",
        "next_short_uuid",
    }:
        job.payload = job.payload | {changed: False}
    elif changed in {"version", "environment", "binding", "scope_id"}:
        key = "panel_binding" if changed == "binding" else changed
        job.payload = job.payload | {key: "changed"}
    elif changed == "owner":
        job.account_id = 99
    elif changed != "confirmed":
        setattr(
            job, "last_error" if changed == "error" else changed, 1 if changed == "attempts" else "changed"
        )
    before = original.value.copy()
    with pytest.raises(DomainError):
        service.cancel_rotation(1, sub.id, job.id, "cancel-key", confirmed=changed != "confirmed")
    assert job.state != "canceled" and original.value == before and added == []
