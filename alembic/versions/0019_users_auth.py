"""Yonder 4.0 Phase 1a — auth core (SPEC-4 Workstream B, D-111/D-112).

Yonder's first user model. `users` are the identity that API/MCP/web
authenticate as (dual-source: local password / oidc / proxy); `refresh_tokens`
back the mobile rotating-refresh flow (only the sha256 is stored). No ownership
columns yet — per-user data isolation is Phase 2.

Revision ID: 0019
Revises: 0018
"""
import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def _has_table(table: str) -> bool:
    return table in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if not _has_table("users"):
        op.create_table(
            "users",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("username", sa.String(64), nullable=False),
            sa.Column("email", sa.String(255), nullable=True),
            sa.Column("password_hash", sa.String(255), nullable=True),
            sa.Column("auth_source", sa.String(16), nullable=False, server_default="local"),
            sa.Column("external_subject", sa.String(255), nullable=True),
            sa.Column("is_admin", sa.Boolean(), nullable=False, server_default="0"),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default="1"),
            sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("traveler_id", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_users_username", "users", ["username"], unique=True)
        op.create_index("ix_users_external_subject", "users", ["external_subject"])
        op.create_index("ix_users_traveler_id", "users", ["traveler_id"])

    if not _has_table("refresh_tokens"):
        op.create_table(
            "refresh_tokens",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("token_hash", sa.String(64), nullable=False),
            sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("user_agent", sa.String(256), nullable=True),
        )
        op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])
        op.create_index("ix_refresh_tokens_token_hash", "refresh_tokens",
                        ["token_hash"], unique=True)


def downgrade() -> None:
    if _has_table("refresh_tokens"):
        op.drop_table("refresh_tokens")
    if _has_table("users"):
        op.drop_table("users")
