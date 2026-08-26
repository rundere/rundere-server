"""Yonder 3.9.2 — bag inventory (real backpacks/suitcases), independent from
the packing catalog (what goes in a bag) and the Packing Profile (preferences).

New `bags` table (owner-editable via admin, seeded empty — no JSON default,
unlike the packing catalog: there's no sensible universal default inventory).
`packing_items.bag` widens from VARCHAR(12) to VARCHAR(64) so it can hold
either the existing generic values (carry_on/checked/backpack) or a specific
Bag's slug id once the owner has bags to assign.

Revision ID: 0018
Revises: 0017
"""
import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def _has_table(table: str) -> bool:
    return table in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if not _has_table("bags"):
        op.create_table(
            "bags",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("label", sa.String(64), nullable=False),
            sa.Column("kind", sa.String(16), nullable=False, server_default="backpack"),
            sa.Column("capacity", sa.String(32), nullable=True),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_bags_kind", "bags", ["kind"])
    with op.batch_alter_table("packing_items") as batch:
        batch.alter_column("bag", type_=sa.String(64), existing_type=sa.String(12))


def downgrade() -> None:
    with op.batch_alter_table("packing_items") as batch:
        batch.alter_column("bag", type_=sa.String(12), existing_type=sa.String(64))
    if _has_table("bags"):
        op.drop_table("bags")
