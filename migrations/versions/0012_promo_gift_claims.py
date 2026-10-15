"""Идемпотентные выдачи подарочных дней без денежных платежей."""

import sqlalchemy as sa
from alembic import op

revision = "v6_0012"
down_revision = "v6_0011"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "v6_promo_claims",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("account_id", sa.BigInteger(), sa.ForeignKey("v5_accounts.id"), nullable=False),
        sa.Column("code", sa.String(40), nullable=False),
        sa.Column("segment_id", sa.String(64), sa.ForeignKey("v5_segments.id"), nullable=False, unique=True),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("account_id", "code"),
    )
    op.create_index("ix_v6_promo_claims_account_id", "v6_promo_claims", ["account_id"])
    op.create_index("ix_v6_promo_claims_code", "v6_promo_claims", ["code"])


def downgrade():
    raise RuntimeError("Историю подарочных прав нельзя удалять откатом.")
