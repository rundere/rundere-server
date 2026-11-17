"""Run·dere — the AI call log (SPEC-12 §6, D-228, YONDER-354): one row per
call to the AI endpoint, with timings and the outcome, never the prompt or the
reply. Kept 90 days.

Revision ID: 0048
Revises: 0047
"""
import sqlalchemy as sa
from alembic import op

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    have = set(sa.inspect(op.get_bind()).get_table_names())
    if "ai_calls" not in have:
        op.create_table(
            "ai_calls",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("feature", sa.String(24), nullable=False),
            sa.Column("function", sa.String(64), nullable=False),
            sa.Column("provider", sa.String(16), nullable=False),
            sa.Column("model", sa.String(120), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("duration_ms", sa.Integer, nullable=False),
            sa.Column("ok", sa.Boolean, nullable=False),
            sa.Column("error", sa.String(64)),
            sa.Column("prompt_tokens", sa.Integer),
            sa.Column("out_tokens", sa.Integer),
            sa.Column("user_id", sa.String(36)),
        )
        op.create_index("ix_ai_calls_feature", "ai_calls", ["feature"])
        op.create_index("ix_ai_calls_started_at", "ai_calls", ["started_at"])
        op.create_index("ix_ai_calls_user_id", "ai_calls", ["user_id"])


def downgrade() -> None:
    op.drop_table("ai_calls")
