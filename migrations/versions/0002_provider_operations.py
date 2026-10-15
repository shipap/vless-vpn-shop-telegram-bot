"""Долговечная связь сохранённого заказа с ответом провайдера."""

from alembic import op
import sqlalchemy as sa

revision = "v5_0002"
down_revision = "v5_0001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "v5_provider_operations",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("order_id", sa.String(64), sa.ForeignKey("v5_orders.id"), nullable=False, unique=True),
        sa.Column("provider_id", sa.String(128), unique=True),
        sa.Column("terminal", sa.String(64), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("pay_url", sa.Text()),
    )


def downgrade():
    raise RuntimeError("Платёжные связи нельзя удалять автоматическим откатом.")
