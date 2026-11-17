"""Run·dere — what's new after an upgrade (SPEC-8 §14, D-234).

`users.seen_version` is the last version whose release notes a person closed.
Existing accounts get NULL, which shows them only the running version's notes;
new accounts are created at the running version by the model.

Revision ID: 0052
Revises: 0051
"""
import sqlalchemy as sa
from alembic import op

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "users" in sa.inspect(bind).get_table_names() and \
            "seen_version" not in {c["name"] for c in sa.inspect(bind).get_columns("users")}:
        with op.batch_alter_table("users") as b:
            b.add_column(sa.Column("seen_version", sa.String(32)))


def downgrade() -> None:
    with op.batch_alter_table("users") as b:
        b.drop_column("seen_version")
