"""Сохранение сменённых соглашений и неуспешных попыток циклов."""

from alembic import op
import sqlalchemy as sa

revision = "v5_0005"
down_revision = "v5_0004"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("v5_agreements_subscription_id_key", "v5_agreements", type_="unique")
    op.add_column(
        "v5_agreements",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index(
        "v5_agreements_active_subscription",
        "v5_agreements",
        ["subscription_id"],
        unique=True,
        postgresql_where=sa.text("state <> 'stopped'"),
    )
    op.create_table(
        "v5_recurring_attempts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "agreement_id", sa.String(64), sa.ForeignKey("v5_agreements.id"), nullable=False, index=True
        ),
        sa.Column("cycle_number", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    raise RuntimeError("Историю согласий нельзя удалять откатом.")
