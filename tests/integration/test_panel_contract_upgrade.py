"""Explicit offline contract expansion never trusts merely matching UUIDs."""

from copy import deepcopy
from datetime import timedelta

import pytest
from sqlalchemy import event, select
from sunrise_core.errors import DomainError
from sunrise_infrastructure.db import Account, Alias, Job, LegacyIssue, Setting, Subscription, now

from tests.integration.test_panel_28_workflows import (
    panel28_workflow,  # noqa: F401 -- shared isolated fixture
)


def legacy_context(context):
    db, panel, _, _, _, state, sid, _ = context
    with db.transaction() as tx:
        record = tx.get(Setting, "panel_test:" + sid)
        record.value = {key: value for key, value in record.value.items() if key != "contract"}
        job = tx.scalar(
            select(Job).where(Job.kind == "provision", Job.payload["subscription_id"].as_string() == sid)
        )
        return db, panel, state, sid, job.id, deepcopy(record.value)


def prepare(context):
    from sunrise_infrastructure.panel_contract_upgrade import (
        binding_record_digest,
        prepare_legacy_uuid_contract_upgrade,
    )

    db, panel, state, sid, job_id, record = context
    before = len(state["calls"])
    with db.transaction() as tx:
        result = prepare_legacy_uuid_contract_upgrade(
            tx, sid, panel, expected_record_digest=binding_record_digest(record), provision_job_id=job_id
        )
        assert tx.get(Setting, "panel_test:" + sid).value == record
    assert len(state["calls"]) == before
    return result


def test_panel_contract_upgrade_proves_creation_without_writing_or_http(panel28_workflow):  # noqa: F811
    context = legacy_context(panel28_workflow)
    assert prepare(context) == context[-1] | {"contract": "2.8_uuid"}


def test_panel_contract_upgrade_never_flushes_caller_pending_changes(panel28_workflow):  # noqa: F811
    from sunrise_infrastructure.panel_contract_upgrade import (
        binding_record_digest,
        prepare_legacy_uuid_contract_upgrade,
    )

    db, panel, _, sid, job_id, record = legacy_context(panel28_workflow)
    with db.transaction() as tx:
        tx.get(Subscription, sid).name = "Изменение вызывающего кода"

        def prevent_flush(*_args):
            raise RuntimeError("offline helper must not flush caller writes")

        event.listen(tx, "before_flush", prevent_flush)
        try:
            with pytest.raises(DomainError) as refusal:
                prepare_legacy_uuid_contract_upgrade(
                    tx,
                    sid,
                    panel,
                    expected_record_digest=binding_record_digest(record),
                    provision_job_id=job_id,
                )
            assert refusal.value.code == "panel_contract_upgrade_denied"
            assert tx.get(Subscription, sid).name == "Изменение вызывающего кода"
        finally:
            event.remove(tx, "before_flush", prevent_flush)
            tx.rollback()


def test_panel_contract_upgrade_preserves_canonical_merge_origin(panel28_workflow):  # noqa: F811
    db, _, commerce, _, _, _, sid, accounts = panel28_workflow
    with db.transaction() as tx:
        tx.add(Account(id=302, email="canonical@example.test", email_verified=True))
    proof = accounts.request_link(302, "70000000000000001", now())["proof"]
    accounts.confirm_telegram_link(301, proof)
    context = legacy_context(panel28_workflow)
    candidate = prepare(context)
    assert candidate["account_id"] == "301"
    with db.transaction() as tx:
        assert tx.get(Alias, 301).canonical_id == 302
        assert tx.get(Subscription, sid).account_id == 302


@pytest.mark.parametrize("contract", [None, "", "3.4_id", "2.8_uuid"])
def test_panel_contract_upgrade_refuses_any_present_contract_without_writes(panel28_workflow, contract):  # noqa: F811
    context = legacy_context(panel28_workflow)
    db, _, _, sid, _, record = context
    record["contract"] = contract
    with db.transaction() as tx:
        tx.get(Setting, "panel_test:" + sid).value = deepcopy(record)
    with pytest.raises(DomainError, match=".*") as denied:
        prepare(context)
    assert denied.value.code == "panel_contract_upgrade_denied"


@pytest.mark.parametrize(
    "fault",
    [
        "imported",
        "unapproved_owner",
        "current_owner",
        "alias",
        "url",
        "uuid",
        "email_only",
        "job_missing",
        "job_owner",
        "job_binding",
        "job_error",
        "job_attempts",
        "job_legacy",
        "job_renewal",
        "activation_missing",
        "activation_malformed",
        "activation_date",
        "activation_historical",
        "record_digest",
        "production",
        "activation_future",
    ],
)
def test_panel_contract_upgrade_denies_incomplete_or_changed_provenance(panel28_workflow, fault):  # noqa: F811
    context = legacy_context(panel28_workflow)
    db, panel, _, sid, job_id, record = context
    with db.transaction() as tx:
        sub, account = tx.get(Subscription, sid), tx.get(Account, 301)
        job = tx.get(Job, job_id)
        activation = tx.get(Setting, "panel_test_activation:" + panel.auto_scope)
        if fault == "imported":
            tx.add(LegacyIssue(source_table="subscriptions", source_id=sid, reason="synthetic-import"))
        elif fault == "unapproved_owner":
            record["telegram_id"] = "999"
        elif fault == "current_owner":
            account.telegram_id = 999
        elif fault == "alias":
            record["account_id"] = "999"
        elif fault == "url":
            sub.connection_url += "changed"
        elif fault == "uuid":
            record["binding"]["uuid"] = "not-uuid"
        elif fault == "email_only":
            account.telegram_id = None
        elif fault == "job_missing":
            tx.delete(job)
        elif fault == "job_owner":
            tx.add(Account(id=999))
            job.account_id = 999
        elif fault == "job_binding":
            job.payload = job.payload | {"panel_binding": {}}
        elif fault == "job_error":
            job.last_error = "superseded"
        elif fault == "job_attempts":
            job.attempts = 0
        elif fault == "job_legacy":
            job.payload = job.payload | {"legacy_source": "synthetic"}
        elif fault == "job_renewal":
            job.payload = job.payload | {"panel_create": False}
        elif fault == "activation_missing":
            tx.delete(activation)
        elif fault == "activation_malformed":
            activation.value = activation.value | {"historical_job_ids": None}
        elif fault == "activation_date":
            activation.value = activation.value | {"activated_at": "2026-01-01"}
        elif fault == "activation_historical":
            activation.value = activation.value | {"historical_job_ids": [job_id]}
        elif fault == "production":
            sub.environment = "production"
            record["environment"] = "production"
            job.payload = job.payload | {"environment": "production"}
        elif fault == "activation_future":
            activation.value = activation.value | {"activated_at": (now() + timedelta(days=1)).isoformat()}
        if fault == "record_digest":
            tx.get(Setting, "panel_test:" + sid).value = record | {"changed": True}
        else:
            tx.get(Setting, "panel_test:" + sid).value = deepcopy(record)
    with pytest.raises(DomainError) as denied:
        prepare(context)
    assert denied.value.code == "panel_contract_upgrade_denied"
