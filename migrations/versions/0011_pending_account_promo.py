"""Один ожидающий применения промокод канонического аккаунта."""

import sqlalchemy as sa
from alembic import op

revision = "v6_0011"
down_revision = "v6_0010"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("v5_accounts", sa.Column("pending_promo", sa.JSON(), nullable=True))


def downgrade():
    raise RuntimeError("Ожидающие применения промокоды нельзя удалять откатом.")
