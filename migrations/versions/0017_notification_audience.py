"""Сохраняет ограничения служебных уведомлений; существующие данные не удаляются."""

import sqlalchemy as sa
from alembic import op

revision = "v11_0017"
down_revision = "v11_0016"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "v11_notifications", sa.Column("audience", sa.String(20), server_default="account", nullable=False)
    )
    op.execute("UPDATE v11_notifications SET audience = 'owner' WHERE kind = 'identity_review'")
    op.execute(
        "UPDATE v11_notifications SET audience = 'staff' WHERE audience = 'account' AND (kind = 'sale' OR split_part(split_part(target, '#', 1), '?', 1) = '/admin')"
    )


def downgrade():
    raise RuntimeError("Нельзя удалять ограничения доступа к истории служебных уведомлений.")
