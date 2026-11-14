"""Run·dere — deleting an account (SPEC-13 §3, D-242).

- `users.deletion_due_at`: the pending marker. Set, the account is locked and
  is deleted on that date.
- `account_deletions`: the request, its due date and the per-trip choices. It
  outlives the account (without the person) so the mailbox step can be retried.
  `user_id` is a plain indexed column, no foreign key (house rule 3).
- `account_events.user_id` becomes nullable: the audit rows of a deleted account
  stay as counts, without the person.

Revision ID: 0055
Revises: 0054
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

_JSON = sa.JSON().with_variant(JSONB(), "postgresql")

revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "deletion_due_at" not in {c["name"] for c in insp.get_columns("users")}:
        op.add_column("users", sa.Column("deletion_due_at", sa.DateTime(timezone=True)))
    if "account_deletions" not in insp.get_table_names():
        op.create_table(
            "account_deletions",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36)),
            sa.Column("requested_by", sa.String(36)),
            sa.Column("by_admin", sa.Boolean, nullable=False, server_default=sa.false()),
            sa.Column("status", sa.String(12), nullable=False, server_default="pending"),
            sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("finished_at", sa.DateTime(timezone=True)),
            sa.Column("choices", _JSON),
            sa.Column("mail_ids", _JSON),
        )
        op.create_index("ix_account_deletions_user_id", "account_deletions", ["user_id"])
        op.create_index("ix_account_deletions_status", "account_deletions", ["status"])
    with op.batch_alter_table("account_events") as batch:
        batch.alter_column("user_id", existing_type=sa.String(36), nullable=True)


def downgrade() -> None:
    # Audit rows of deleted accounts have no user to go back to.
    op.execute("DELETE FROM account_events WHERE user_id IS NULL")
    with op.batch_alter_table("account_events") as batch:
        batch.alter_column("user_id", existing_type=sa.String(36), nullable=False)
    op.drop_index("ix_account_deletions_status", table_name="account_deletions")
    op.drop_index("ix_account_deletions_user_id", table_name="account_deletions")
    op.drop_table("account_deletions")
    op.drop_column("users", "deletion_due_at")
