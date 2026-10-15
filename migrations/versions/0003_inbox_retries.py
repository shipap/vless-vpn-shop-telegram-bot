"""Повторная сверка входящих платежей без блокирования очереди."""

from alembic import op
import sqlalchemy as sa

revision = "v5_0003"
down_revision = "v5_0002"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("v5_payment_inbox", sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"))
    op.add_column(
        "v5_payment_inbox",
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.add_column("v5_payment_inbox", sa.Column("last_error", sa.String(100)))


def downgrade():
    raise RuntimeError("Историю платёжных попыток нельзя удалять автоматическим откатом.")
