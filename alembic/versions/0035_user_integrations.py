"""Run·dere 4.8 — per-user AirTrail / AdventureLog connections (SPEC-8 D-150,
D-151; YONDER-192).

One row per user and kind. The credential is AES-256-GCM ciphertext (never the
token itself); user_id is a soft link (house rule 3). The deployment owner's
rows are seeded at boot from the deprecated TX_* variables (app/integrations.py),
not here: encrypting needs the secrets key, which migrations don't read.

Revision ID: 0035
Revises: 0034
"""
import sqlalchemy as sa
from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "user_integrations" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "user_integrations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("base_url", sa.String(512), nullable=False),
        sa.Column("public_url", sa.String(512)),
        sa.Column("credential_ct", sa.Text),
        sa.Column("credential_hint", sa.String(8)),
        sa.Column("remote_user_id", sa.String(128)),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("last_check_at", sa.DateTime(timezone=True)),
        sa.Column("last_check_ok", sa.Boolean),
        sa.Column("last_error", sa.String(300)),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("user_id", "kind", name="uq_user_integration_kind"),
    )
    op.create_index("ix_user_integrations_user_id", "user_integrations", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_user_integrations_user_id", table_name="user_integrations")
    op.drop_table("user_integrations")
