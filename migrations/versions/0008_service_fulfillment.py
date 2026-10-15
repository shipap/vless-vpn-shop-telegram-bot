"""Фактическая выдача услуги отдельно от поступления внешних денег."""

import sqlalchemy as sa
from alembic import op

revision = "v5_0008"
down_revision = "v5_0007"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("v5_orders", sa.Column("fulfilled_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    raise RuntimeError("Подтверждённое время выдачи услуги нельзя удалять откатом.")
