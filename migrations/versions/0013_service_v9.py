"""Имя Telegram и история успешных отвязок; существующие права сохраняются."""

import sqlalchemy as sa
from alembic import op

revision = "v9_0013"
down_revision = "v6_0012"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("v5_accounts", sa.Column("telegram_username", sa.String(32), nullable=True))
    op.create_index("ix_v5_accounts_telegram_username", "v5_accounts", ["telegram_username"])
    op.add_column(
        "v5_accounts", sa.Column("unlink_warnings", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "v5_accounts", sa.Column("unlink_warnings_total", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "v5_accounts", sa.Column("unlink_blocked", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.create_table(
        "v9_device_unlinks",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("account_id", sa.BigInteger(), sa.ForeignKey("v5_accounts.id"), nullable=False),
        sa.Column("original_account_id", sa.BigInteger(), nullable=False),
        sa.Column("subscription_id", sa.String(64), sa.ForeignKey("v5_subscriptions.id"), nullable=False),
        sa.Column("device_id", sa.String(64), sa.ForeignKey("v5_devices.id"), nullable=False, unique=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_v9_device_unlinks_account_id", "v9_device_unlinks", ["account_id"])


def downgrade():
    raise RuntimeError("Историю отвязок и решений владельца нельзя удалять откатом.")
