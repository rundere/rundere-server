"""Run·dere 4.10 — first-party OAuth clients (YONDER-235, SPEC-8 D-194): the
built-in iOS app client's tokens count as first-party and skip consent. The
client row itself is upserted at startup (oauth.ensure_app_client).

Revision ID: 0039
Revises: 0038
"""
import sqlalchemy as sa
from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("oauth_clients")}
    if "first_party" not in cols:
        with op.batch_alter_table("oauth_clients") as b:
            b.add_column(sa.Column("first_party", sa.Boolean(), nullable=False,
                                   server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table("oauth_clients") as b:
        b.drop_column("first_party")
