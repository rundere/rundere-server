"""Run·dere — the archive export's build record (SPEC-13 §2.5, D-241).

New: `archive_exports`, one row per build of a person's Run·dere archive. The
zip itself is on disk under TX_EXPORT_DIR; this row says whose it is, where the
build stands, when the file expires and its checksum. `user_id` is a plain
indexed column, no foreign key (house rule 3).

Revision ID: 0054
Revises: 0053

Renumbered from 0053 when 5.0 waves met on `next`: 0053 is the travel
theme columns (0053_ui_theme.py, merged first).
"""
import sqlalchemy as sa
from alembic import op

revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "archive_exports" in sa.inspect(bind).get_table_names():
        return
    op.create_table(
        "archive_exports",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(12), nullable=False, server_default="building"),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("size_bytes", sa.Integer),
        sa.Column("sha256", sa.String(64)),
        sa.Column("format_version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("error", sa.String(64)),
    )
    op.create_index("ix_archive_exports_user_id", "archive_exports", ["user_id"])
    op.create_index("ix_archive_exports_status", "archive_exports", ["status"])


def downgrade() -> None:
    op.drop_index("ix_archive_exports_status", table_name="archive_exports")
    op.drop_index("ix_archive_exports_user_id", table_name="archive_exports")
    op.drop_table("archive_exports")
