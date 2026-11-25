"""Run·dere — asking an admin to link your account to a traveller (D-250).

- `users.requested_traveler_id`: the existing traveller a new account says it
  is, waiting for an admin to link it (or dismiss the request). A plain indexed
  column, no foreign key (house rule 3). NULL = no request.
- `users.link_requested_at`: when they asked.

Revision ID: 0056
Revises: 0055
"""
import sqlalchemy as sa
from alembic import op

revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("users")}
    if "requested_traveler_id" not in cols:
        op.add_column("users", sa.Column("requested_traveler_id", sa.Integer))
        op.create_index("ix_users_requested_traveler_id", "users", ["requested_traveler_id"])
    if "link_requested_at" not in cols:
        op.add_column("users", sa.Column("link_requested_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_index("ix_users_requested_traveler_id", table_name="users")
    op.drop_column("users", "link_requested_at")
    op.drop_column("users", "requested_traveler_id")
