"""Drop the UNIQUE constraint on travel_items.canonical_key (keep the index).

The baseline made canonical_key unique, which contradicts the revision model:
D-13 chains SEPARATE rows via revision.prior_item_id, and D-15 keeps every
version forever, so a recapture (no_change), an update, and a cancellation
superseding its confirmation all legitimately carry the SAME canonical_key.
Caught by a resolve-stage test: superseding a hotel booking hit
"UNIQUE constraint failed: travel_items.canonical_key".

Identity uniqueness lives in item_keys.key (unchanged, still unique), which
maps each key to exactly one owning item. RULE-17c separately forbids a
duplicate key WITHIN one batch, which is the case that really is an error.

Revision ID: 0003
Revises: 0002
"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

_PG_CONSTRAINT = "travel_items_canonical_key_key"
_INDEX = "ix_travel_items_canonical_key"


def upgrade() -> None:
    bind = op.get_bind()
    # A fresh database built by the 0001 baseline already has the non-unique
    # index (the model no longer declares unique), so there is nothing to undo.
    import sqlalchemy as sa
    insp = sa.inspect(bind)
    if "travel_items" in insp.get_table_names():
        for ix in insp.get_indexes("travel_items"):
            if ix["name"] == _INDEX and not ix.get("unique"):
                return
    if bind.dialect.name == "postgresql":
        # `unique=True` created a UNIQUE CONSTRAINT (with its own index).
        op.execute(f"ALTER TABLE travel_items DROP CONSTRAINT IF EXISTS {_PG_CONSTRAINT}")
        op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
        op.create_index(_INDEX, "travel_items", ["canonical_key"], unique=False)
    else:
        # SQLite cannot drop a constraint in place; batch mode rebuilds the table.
        with op.batch_alter_table("travel_items") as batch:
            try:
                batch.drop_index(_INDEX)
            except Exception:
                pass
            batch.alter_column("canonical_key", existing_type=sa.String(512))
        op.create_index(_INDEX, "travel_items", ["canonical_key"], unique=False)


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="travel_items")
    op.create_index(_INDEX, "travel_items", ["canonical_key"], unique=True)
