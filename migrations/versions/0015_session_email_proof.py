"""Отдельное доказательство свежего email OTP; старым сессиям не приписывается."""

import sqlalchemy as sa
from alembic import op

revision = "v10_0015"
down_revision = "v10_0014"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("v5_sessions", sa.Column("email_confirmed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    raise RuntimeError("Удаление доказательства email отключит защиту настройки MFA.")
