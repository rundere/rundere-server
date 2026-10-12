"""Run·dere 4.8 — trip expenses, budget and settle-up (SPEC-10, YONDER-172, D-173–D-185).

Actual expenses + their shares, planned budget items, and the trip's expense
currency (no foreign keys, house convention).
Amounts and rates are decimal strings, so no float rounding creeps into balances.

Revision ID: 0033
Revises: 0032
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

_JSON = sa.JSON().with_variant(JSONB(), "postgresql")

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    tables = insp.get_table_names()
    if "expense_currency" not in {c["name"] for c in insp.get_columns("trips")}:
        op.add_column("trips", sa.Column("expense_currency", sa.String(3)))
    if "trip_expenses" not in tables:
        op.create_table(
            "trip_expenses",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("trip_id", sa.String(36), nullable=False),
            sa.Column("owner_id", sa.String(36)),
            sa.Column("kind", sa.String(12), nullable=False, server_default="expense"),
            sa.Column("title", sa.String(200), nullable=False, server_default=""),
            sa.Column("amount", sa.String(32), nullable=False),
            sa.Column("currency", sa.String(3), nullable=False),
            sa.Column("rate", sa.String(32)),
            sa.Column("rate_source", sa.String(12)),
            sa.Column("spent_on", sa.String(10)),
            sa.Column("payer_id", sa.Integer, nullable=False),
            sa.Column("split", sa.String(8), nullable=False, server_default="equal"),
            sa.Column("category", sa.String(16), nullable=False, server_default="other"),
            sa.Column("activity_id", sa.String(36)),
            sa.Column("budget_item_id", sa.String(36)),
            sa.Column("notes", sa.Text),
            sa.Column("source", sa.String(16), nullable=False, server_default="api"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_trip_expenses_trip_id", "trip_expenses", ["trip_id"])
        op.create_index("ix_trip_expenses_owner_id", "trip_expenses", ["owner_id"])
        op.create_index("ix_trip_expenses_spent_on", "trip_expenses", ["spent_on"])
        op.create_index("ix_trip_expenses_activity_id", "trip_expenses", ["activity_id"])
        op.create_index("ix_trip_expenses_budget_item_id", "trip_expenses", ["budget_item_id"])
    if "trip_budget_items" not in tables:
        op.create_table(
            "trip_budget_items",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("trip_id", sa.String(36), nullable=False),
            sa.Column("owner_id", sa.String(36)),
            sa.Column("title", sa.String(200), nullable=False, server_default=""),
            sa.Column("category", sa.String(16), nullable=False, server_default="other"),
            sa.Column("amount", sa.String(32), nullable=False),
            sa.Column("currency", sa.String(3), nullable=False),
            sa.Column("rate", sa.String(32)),
            sa.Column("rate_source", sa.String(12)),
            sa.Column("status", sa.String(8), nullable=False, server_default="kept"),
            sa.Column("start_date", sa.String(10)),
            sa.Column("end_date", sa.String(10)),
            sa.Column("place_key", sa.String(128)),
            sa.Column("activity_id", sa.String(36)),
            sa.Column("notes", sa.Text),
            sa.Column("sources", _JSON),
            sa.Column("source", sa.String(16), nullable=False, server_default="api"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_trip_budget_items_trip_id", "trip_budget_items", ["trip_id"])
        op.create_index("ix_trip_budget_items_owner_id", "trip_budget_items", ["owner_id"])
        op.create_index("ix_trip_budget_items_activity_id", "trip_budget_items", ["activity_id"])
    if "trip_budget_travelers" not in tables:
        op.create_table(
            "trip_budget_travelers",
            sa.Column("budget_item_id", sa.String(36), primary_key=True),
            sa.Column("traveler_id", sa.Integer, primary_key=True),
        )
        op.create_index("ix_trip_budget_travelers_traveler_id", "trip_budget_travelers",
                        ["traveler_id"])
    if "trip_expense_comments" not in tables:
        op.create_table(
            "trip_expense_comments",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("expense_id", sa.String(36), nullable=False),
            sa.Column("trip_id", sa.String(36), nullable=False),
            sa.Column("author_id", sa.String(36)),
            sa.Column("body", sa.Text, nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_trip_expense_comments_expense_id", "trip_expense_comments",
                        ["expense_id"])
        op.create_index("ix_trip_expense_comments_trip_id", "trip_expense_comments", ["trip_id"])
        op.create_index("ix_trip_expense_comments_author_id", "trip_expense_comments",
                        ["author_id"])
    if "trip_expense_shares" not in tables:
        op.create_table(
            "trip_expense_shares",
            sa.Column("expense_id", sa.String(36), primary_key=True),
            sa.Column("traveler_id", sa.Integer, primary_key=True),
            sa.Column("amount", sa.String(32), nullable=False),
        )
        op.create_index("ix_trip_expense_shares_traveler_id", "trip_expense_shares",
                        ["traveler_id"])


def downgrade() -> None:
    op.drop_table("trip_expense_comments")
    op.drop_table("trip_budget_travelers")
    op.drop_table("trip_budget_items")
    op.drop_table("trip_expense_shares")
    op.drop_table("trip_expenses")
    op.drop_column("trips", "expense_currency")
