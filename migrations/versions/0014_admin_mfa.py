"""Шифрованный аутентификатор администратора и доверие к IP на 48 часов."""

import sqlalchemy as sa
from alembic import op

revision = "v10_0014"
down_revision = "v9_0013"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "v10_admin_authenticators",
        sa.Column("account_id", sa.BigInteger(), sa.ForeignKey("v5_accounts.id"), primary_key=True),
        sa.Column("secret_ciphertext", sa.Text(), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pending_ciphertext", sa.Text(), nullable=True),
        sa.Column("pending_session_id", sa.String(64), nullable=True),
        sa.Column("pending_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_totp_step", sa.BigInteger(), nullable=True),
        sa.Column("trusted_ip_digest", sa.String(64), nullable=True),
        sa.Column("trust_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("(secret_ciphertext IS NULL) = (activated_at IS NULL)", name="ck_mfa_active"),
        sa.CheckConstraint(
            "(pending_ciphertext IS NULL AND pending_session_id IS NULL AND pending_expires_at IS NULL) "
            "OR (pending_ciphertext IS NOT NULL AND pending_session_id IS NOT NULL AND pending_expires_at IS NOT NULL)",
            name="ck_mfa_pending",
        ),
        sa.CheckConstraint("(trusted_ip_digest IS NULL) = (trust_verified_at IS NULL)", name="ck_mfa_trust"),
    )


def downgrade():
    raise RuntimeError("Активные ключи MFA нельзя удалять автоматическим откатом.")
