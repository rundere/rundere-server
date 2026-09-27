"""YONDER-107 — Yonder as an OAuth 2.1 authorization server (+ personal API tokens).

`oauth_clients` (DCR / CLI-registered clients), `oauth_codes` (single-use,
hashed authorization codes), `oauth_grants` (one per user consent; rotating
hashed refresh token) and `personal_api_tokens` (hashed `ypat_…` tokens made on
the Account page). Soft links, no DB FKs (house convention).

Revision ID: 0028
Revises: 0027
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None

_JSON = sa.JSON().with_variant(JSONB(), "postgresql")


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def _ts(name: str, nullable: bool = True) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    if not _has_table("oauth_clients"):
        op.create_table(
            "oauth_clients",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("name", sa.String(128), nullable=False),
            sa.Column("redirect_uris", _JSON, nullable=False),
            sa.Column("secret_hash", sa.String(64), nullable=True),
            sa.Column("source", sa.String(8), nullable=False, server_default="dcr"),
            sa.Column("created_by", sa.String(36), nullable=True),
            _ts("created_at", nullable=False),
        )
    if not _has_table("oauth_codes"):
        op.create_table(
            "oauth_codes",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("code_hash", sa.String(64), nullable=False, unique=True, index=True),
            sa.Column("client_id", sa.String(64), nullable=False, index=True),
            sa.Column("user_id", sa.String(36), nullable=False, index=True),
            sa.Column("redirect_uri", sa.Text(), nullable=False),
            sa.Column("code_challenge", sa.String(128), nullable=False),
            sa.Column("scope", sa.String(255), nullable=True),
            sa.Column("resource", sa.String(512), nullable=True),
            _ts("expires_at", nullable=False),
            _ts("used_at"),
            sa.Column("grant_id", sa.String(36), nullable=True),
        )
    if not _has_table("oauth_grants"):
        op.create_table(
            "oauth_grants",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("client_id", sa.String(64), nullable=False, index=True),
            sa.Column("user_id", sa.String(36), nullable=False, index=True),
            sa.Column("scope", sa.String(255), nullable=True),
            sa.Column("resource", sa.String(512), nullable=True),
            sa.Column("refresh_hash", sa.String(64), nullable=True),
            sa.Column("prev_refresh_hashes", _JSON, nullable=True),
            sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"),
            _ts("created_at", nullable=False),
            _ts("last_used_at"),
            _ts("expires_at", nullable=False),
            _ts("revoked_at"),
        )
    if not _has_table("personal_api_tokens"):
        op.create_table(
            "personal_api_tokens",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36), nullable=False, index=True),
            sa.Column("name", sa.String(128), nullable=False),
            sa.Column("token_hash", sa.String(64), nullable=False, unique=True, index=True),
            sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"),
            _ts("created_at", nullable=False),
            _ts("last_used_at"),
            _ts("expires_at"),
            _ts("revoked_at"),
        )


def downgrade() -> None:
    for name in ("personal_api_tokens", "oauth_grants", "oauth_codes", "oauth_clients"):
        if _has_table(name):
            op.drop_table(name)
