"""Run·dere — admin notifications (YONDER-253, SPEC-8 D-197): where alerts go
(targets, credentials encrypted at rest) and each watched service's health.

Revision ID: 0042
Revises: 0041
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

_JSON = sa.JSON().with_variant(JSONB(), "postgresql")     # same as models.JsonCol

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    have = set(sa.inspect(op.get_bind()).get_table_names())
    if "notification_targets" not in have:
        op.create_table(
            "notification_targets",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("kind", sa.String(16), nullable=False),
            sa.Column("name", sa.String(80), nullable=False, server_default=""),
            sa.Column("secret_ct", sa.Text()),
            sa.Column("secret_hint", sa.String(8)),
            sa.Column("config", _JSON),
            sa.Column("categories", _JSON),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_by", sa.String(36)),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("last_sent_at", sa.DateTime(timezone=True)),
            sa.Column("last_error", sa.String(300)),
        )
    if "alert_states" not in have:
        op.create_table(
            "alert_states",
            sa.Column("key", sa.String(120), primary_key=True),
            sa.Column("category", sa.String(24), nullable=False),
            sa.Column("label", sa.String(120), nullable=False, server_default=""),
            sa.Column("failing", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("fail_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("since", sa.DateTime(timezone=True)),
            sa.Column("alerted", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("last_alert_at", sa.DateTime(timezone=True)),
            sa.Column("detail", sa.String(300)),
            sa.Column("checked_at", sa.DateTime(timezone=True)),
        )


def downgrade() -> None:
    op.drop_table("alert_states")
    op.drop_table("notification_targets")
