"""Резерв денежного остатка, проверенная валюта и отдельный неизменяемый журнал."""

import sqlalchemy as sa
from alembic import op

revision = "v5_0007"
down_revision = "v5_0006"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "v5_accounts", sa.Column("credit_reserved", sa.Numeric(24, 6), nullable=False, server_default="0")
    )
    op.add_column("v5_accounts", sa.Column("credit_currency", sa.String(3), nullable=True))
    op.add_column(
        "v5_accounts",
        sa.Column("credit_provenance", sa.String(40), nullable=False, server_default="unverified"),
    )
    op.create_table(
        "v5_credit_ledger",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("account_id", sa.BigInteger(), sa.ForeignKey("v5_accounts.id"), nullable=False, index=True),
        sa.Column("original_account_id", sa.BigInteger(), nullable=False),
        sa.Column("operation", sa.String(200), nullable=False, unique=True),
        sa.Column("amount", sa.Numeric(24, 6), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("provenance", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    raise RuntimeError("Подтверждённые денежные записи нельзя удалять откатом.")
