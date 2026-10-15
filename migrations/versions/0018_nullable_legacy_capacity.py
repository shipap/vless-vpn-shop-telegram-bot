"""Сохраняет явно не заданный исторический лимит устройств; новые периоды не меняются."""

import sqlalchemy as sa
from alembic import op

revision = "v11_0018"
down_revision = "v11_0017"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column("v5_subscriptions", "devices_limit", existing_type=sa.Integer(), nullable=True)


def downgrade():
    raise RuntimeError("Нельзя подменять исторический NULL лимита устройств выдуманным числом.")
