"""Совместимое добавление версий согласий и настроек витрины."""

import sqlalchemy as sa
from alembic import op

revision = "v6_0009"
down_revision = "v5_0008"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "v5_accounts", sa.Column("legacy_consent", sa.JSON(), nullable=False, server_default=sa.text("'{}'"))
    )
    op.add_column(
        "v5_products", sa.Column("presentation", sa.JSON(), nullable=False, server_default=sa.text("'{}'"))
    )
    op.create_table(
        "v6_legal_consents",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("account_id", sa.BigInteger(), sa.ForeignKey("v5_accounts.id"), nullable=False),
        sa.Column("original_account_id", sa.BigInteger(), nullable=False),
        sa.Column("document_type", sa.String(20), nullable=False),
        sa.Column("document_version", sa.String(100), nullable=False),
        sa.Column("document_url", sa.Text(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("context", sa.String(40), nullable=False),
        sa.CheckConstraint("document_type IN ('offer', 'privacy') AND source IN ('website', 'telegram')"),
    )
    op.create_index("ix_v6_legal_consents_account_id", "v6_legal_consents", ["account_id"])


def downgrade():
    raise RuntimeError("Историю согласий нельзя удалять откатом.")
