"""Аудируемая очередь решения по правам при возврате."""

import sqlalchemy as sa
from alembic import op

revision = "v6_0010"
down_revision = "v6_0009"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "v6_refund_reviews",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("refund_id", sa.String(64), sa.ForeignKey("v5_refunds.id"), nullable=False, unique=True),
        sa.Column("payment_id", sa.String(64), sa.ForeignKey("v5_payments.id"), nullable=False),
        sa.Column("segment_id", sa.String(64), sa.ForeignKey("v5_segments.id")),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("resolution", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_v6_refund_reviews_payment_id", "v6_refund_reviews", ["payment_id"])


def downgrade():
    raise RuntimeError("Аудит возвратов нельзя удалять откатом.")
