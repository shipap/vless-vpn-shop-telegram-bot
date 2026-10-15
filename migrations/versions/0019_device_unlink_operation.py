"""Снимок подтверждённой отвязки без выдумывания исторического типа операции."""

import sqlalchemy as sa
from alembic import op

revision = "v11_0019"
down_revision = "v11_0018"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("v9_device_unlinks", sa.Column("unlink_mode", sa.String(16), nullable=True))
    op.add_column("v9_device_unlinks", sa.Column("unlink_operation", sa.String(200), nullable=True))
    op.add_column("v9_device_unlinks", sa.Column("device_name", sa.String(200), nullable=True))
    # Только исходный уникальный durable Job доказывает режим и группу операции.
    op.execute(
        sa.text("""
        UPDATE v9_device_unlinks AS event
        SET unlink_mode = job.payload->>'unlink_mode',
            unlink_operation = job.payload->>'unlink_operation'
        FROM v5_jobs AS job
        WHERE job.key = 'device-revoke:' || event.device_id || ':' || CAST(event.generation AS TEXT)
          AND job.kind = 'device_revoke'
          AND job.account_id = event.account_id
          AND job.payload->>'subscription_id' = event.subscription_id
          AND job.payload->>'device_id' = event.device_id
          AND job.payload->>'generation' = CAST(event.generation AS TEXT)
          AND job.payload->>'unlink_mode' IN ('single', 'bulk')
          AND length(job.payload->>'unlink_operation') BETWEEN 1 AND 200
    """)
    )
    op.create_check_constraint(
        "ck_device_unlink_provenance",
        "v9_device_unlinks",
        "unlink_mode IS NULL OR (unlink_mode IN ('single', 'bulk') AND unlink_operation IS NOT NULL)",
    )


def downgrade():
    raise RuntimeError("Нельзя удалять подтверждённое происхождение отвязок и снимки устройств.")
