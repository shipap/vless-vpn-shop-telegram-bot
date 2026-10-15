"""Неизменяемые движения комиссий/зачислений и исходный плательщик."""

from alembic import op
import sqlalchemy as sa

revision = "v5_0006"
down_revision = "v5_0005"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("v5_payments", sa.Column("original_account_id", sa.BigInteger(), nullable=True))
    # Existing canonical ownership cannot prove the payer before any historical merge.
    op.create_table(
        "v5_settlements",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("payment_id", sa.String(64), sa.ForeignKey("v5_payments.id"), nullable=False, index=True),
        sa.Column("operation_id", sa.String(200), nullable=False, unique=True),
        sa.Column("fee", sa.Numeric(24, 8)),
        sa.Column("fee_currency", sa.String(5)),
        sa.Column("net_usdt", sa.Numeric(24, 8)),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("time_source", sa.String(32), nullable=False),
    )


def downgrade():
    raise RuntimeError("Подтверждённые финансовые факты нельзя удалять откатом.")
