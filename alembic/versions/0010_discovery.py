"""Yonder 3.0 — Discovery: durable research runs + findings.

Two tables backing the in-house trip research pipeline (SPEC-3-discovery.md):

  discovery_runs      one quality-paced research pass over a Trip (D-63) — a
                      DURABLE job row (unlike the in-memory review job registry)
                      so a long run survives a restart.
  discovery_findings  one researched topic for one place, held on Yonder for
                      human review/edit/approve before an idempotent push to an
                      AdventureLog Note. Idempotency on (trip_id, topic,
                      place_key) is enforced in APP code — no DB unique
                      constraint — same pattern as 0007/0008/0009, identical on
                      SQLite (dev) and Postgres (prod).

Revision ID: 0010
Revises: 0009
"""
import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def _has_table(table: str) -> bool:
    return table in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if not _has_table("discovery_runs"):
        op.create_table(
            "discovery_runs",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("trip_id", sa.String(36), nullable=False),
            sa.Column("status", sa.String(12), nullable=False, server_default="running"),
            sa.Column("phase", sa.String(128)),
            sa.Column("done", sa.Integer, nullable=False, server_default="0"),
            sa.Column("total", sa.Integer, nullable=False, server_default="0"),
            sa.Column("params", sa.JSON),
            sa.Column("error", sa.Text),
            sa.Column("started_at", sa.DateTime(timezone=True)),
            sa.Column("finished_at", sa.DateTime(timezone=True)),
        )
        op.create_index("ix_discovery_runs_trip_id", "discovery_runs", ["trip_id"])
        op.create_index("ix_discovery_runs_status", "discovery_runs", ["status"])

    if not _has_table("discovery_findings"):
        op.create_table(
            "discovery_findings",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("run_id", sa.String(36)),
            sa.Column("trip_id", sa.String(36), nullable=False),
            sa.Column("topic", sa.String(24), nullable=False),
            sa.Column("scope", sa.String(8), nullable=False),
            sa.Column("place_key", sa.String(128), nullable=False),
            sa.Column("status", sa.String(12), nullable=False, server_default="draft"),
            sa.Column("body_md", sa.Text),
            sa.Column("data", sa.JSON),
            sa.Column("sources", sa.JSON),
            sa.Column("confidence", sa.String(8)),
            sa.Column("resolver", sa.String(32)),
            sa.Column("steer_note", sa.Text),
            sa.Column("ttl_seconds", sa.Integer),
            sa.Column("generated_at", sa.DateTime(timezone=True)),
            sa.Column("pushed_at", sa.DateTime(timezone=True)),
            sa.Column("adventurelog_note_id", sa.String(64)),
        )
        op.create_index("ix_discovery_findings_run_id", "discovery_findings", ["run_id"])
        op.create_index("ix_discovery_findings_trip_id", "discovery_findings", ["trip_id"])
        op.create_index("ix_discovery_findings_topic", "discovery_findings", ["topic"])
        # The idempotency handle (trip_id, topic, place_key) — a plain composite
        # index for fast upsert lookups; uniqueness is enforced in app code.
        op.create_index("ix_discovery_findings_idem", "discovery_findings",
                        ["trip_id", "topic", "place_key"])


def downgrade() -> None:
    if _has_table("discovery_findings"):
        op.drop_table("discovery_findings")
    if _has_table("discovery_runs"):
        op.drop_table("discovery_runs")
