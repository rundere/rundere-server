"""Phase 0 baseline: all tables from app.models (HANDOFF Task 5, D-24/D-25).

The baseline intentionally derives from the models' metadata — one source of
truth at time zero. Every LATER migration must be an explicit
autogenerate-reviewed diff, never create_all.

Revision ID: 0001
Revises:
"""
from alembic import op

from app.models import Base

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
