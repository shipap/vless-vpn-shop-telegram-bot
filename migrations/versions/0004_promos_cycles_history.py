"""История промокодов, подтверждённых циклов и неизменённых старых покупок."""

from alembic import op
import sqlalchemy as sa

revision = "v5_0004"
down_revision = "v5_0003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "v5_promo_uses",
        sa.Column("order_id", sa.String(64), sa.ForeignKey("v5_orders.id"), primary_key=True),
        sa.Column("account_id", sa.BigInteger(), sa.ForeignKey("v5_accounts.id"), nullable=False, index=True),
        sa.Column("code", sa.String(40), nullable=False, index=True),
        sa.Column("state", sa.String(20), nullable=False),
    )
    op.create_table(
        "v5_recurring_cycles",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("agreement_id", sa.String(64), sa.ForeignKey("v5_agreements.id"), nullable=False),
        sa.Column("cycle_number", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.String(64), sa.ForeignKey("v5_orders.id"), nullable=False, unique=True),
        sa.Column("provider_payment_id", sa.String(128), nullable=False, unique=True),
        sa.Column("state", sa.String(30), nullable=False),
        sa.UniqueConstraint("agreement_id", "cycle_number"),
    )
    op.create_table(
        "v5_legacy_purchases",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("account_id", sa.BigInteger(), sa.ForeignKey("v5_accounts.id"), index=True),
        sa.Column("original_account_id", sa.BigInteger(), nullable=False),
        sa.Column("amount", sa.Numeric(24, 6), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("item_name", sa.Text(), nullable=False),
        sa.Column("raw_date", sa.String(64)),
        sa.Column("provider", sa.String(80)),
        sa.Column("invoice_id", sa.Text()),
        sa.Column("source", sa.String(80), nullable=False),
    )


def downgrade():
    raise RuntimeError("Историю финансовых операций нельзя удалять откатом.")
