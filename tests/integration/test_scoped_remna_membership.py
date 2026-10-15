"""The private relay resolver uses current PostgreSQL ownership without writes."""

# ruff: noqa: F811, S105 -- shared pytest fixtures and a synthetic in-memory HMAC key.
from sqlalchemy import text
from sunrise_api.remna_membership import admitted_resource_in
from sunrise_api.webhook_router import remna_resource_digest
from sunrise_infrastructure.db import Setting

from tests.contract.test_scoped_payment_policy import synthetic_scope  # noqa: F401
from tests.integration.test_scoped_panel_runtime_events import (  # noqa: F401
    NODE,
    paid_panel,
    scoped_panel_database,
)


def test_private_probe_reads_current_owned_user_and_node_without_writes(paid_panel):
    db, settings, commerce, _, _, _, _, _ = paid_panel
    secret = "synthetic-private-probe-key"
    with db.transaction() as session:
        session.execute(text("SET TRANSACTION READ ONLY"))
        assert admitted_resource_in(
            session, commerce, settings, "user", remna_resource_digest(secret, "user", "432"), secret
        )
        assert not admitted_resource_in(
            session, commerce, settings, "user", remna_resource_digest(secret, "user", "99999"), secret
        )
        assert admitted_resource_in(
            session, commerce, settings, "node", remna_resource_digest(secret, "node", NODE), secret
        )
        assert not admitted_resource_in(
            session, commerce, settings, "node", remna_resource_digest(secret, "node", "unrelated"), secret
        )
        assert session.execute(text("SHOW transaction_read_only")).scalar_one() == "on"


def test_private_probe_never_reads_binding_outside_current_account_scope(paid_panel):
    db, settings, commerce, aid, _, _, _, _ = paid_panel
    secret = "synthetic-private-probe-key"
    with db.transaction() as session:
        marker = session.get(Setting, "scoped_test_account:" + str(aid))
        marker.value = marker.value | {"scope_id": "unrelated-scope"}
    with db.transaction() as session:
        session.execute(text("SET TRANSACTION READ ONLY"))
        assert not admitted_resource_in(
            session, commerce, settings, "user", remna_resource_digest(secret, "user", "432"), secret
        )
