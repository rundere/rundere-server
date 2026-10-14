"""Run·dere — the documents store (YONDER-179, SPEC-8 D-153/D-196): captured
files (PDF, .ics, .eml) per user, and an admin's per-user quota override.
The bytes live on disk under TX_DOCUMENTS_DIR, never in the database.

Revision ID: 0041
Revises: 0040
"""
import sqlalchemy as sa
from alembic import op

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "documents" not in set(insp.get_table_names()):
        op.create_table(
            "documents",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("owner_id", sa.String(36), nullable=False),
            sa.Column("sha256", sa.String(64), nullable=False),
            sa.Column("content_type", sa.String(64), nullable=False),
            sa.Column("size_bytes", sa.Integer(), nullable=False),
            sa.Column("filename", sa.String(255), nullable=False, server_default=""),
            sa.Column("source", sa.String(16), nullable=False, server_default="api"),
            sa.Column("trip_id", sa.String(36)),
            sa.Column("batch_id", sa.String(36)),
            sa.Column("scan_notes", sa.String(255)),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("purged_at", sa.DateTime(timezone=True)),
            sa.UniqueConstraint("owner_id", "sha256", name="uq_document_owner_sha"),
        )
        op.create_index("ix_documents_owner_id", "documents", ["owner_id"])
        op.create_index("ix_documents_trip_id", "documents", ["trip_id"])
        op.create_index("ix_documents_batch_id", "documents", ["batch_id"])
    cols = {c["name"] for c in insp.get_columns("users")}
    if "document_quota_mb" not in cols:
        with op.batch_alter_table("users") as b:
            b.add_column(sa.Column("document_quota_mb", sa.Integer()))


def downgrade() -> None:
    with op.batch_alter_table("users") as b:
        b.drop_column("document_quota_mb")
    op.drop_table("documents")
