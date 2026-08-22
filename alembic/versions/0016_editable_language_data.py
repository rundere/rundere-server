"""Yonder 3.6 — editable language data (phrasebook + regional map).

`phrasebook_phrases` and `regional_languages` hold the language datasets as data
the owner edits via the admin UI, seeded from discover/data/*.json at startup
(same lifecycle as packing_catalog_items). One row per phrase and per
(country, region, language). `source` on a phrase marks vetted vs generated, so
LLM-generated fallbacks persist and can be promoted to vetted.

Revision ID: 0016
Revises: 0015
"""
import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def _has_table(table: str) -> bool:
    return table in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if not _has_table("phrasebook_phrases"):
        op.create_table(
            "phrasebook_phrases",
            sa.Column("id", sa.String(48), primary_key=True),
            sa.Column("lang_code", sa.String(12), nullable=False),
            sa.Column("phrase_key", sa.String(24), nullable=False),
            sa.Column("text", sa.String(200), nullable=False),
            sa.Column("pronunciation", sa.String(200), server_default=""),
            sa.Column("source", sa.String(12), nullable=False, server_default="vetted"),
            sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
            sa.UniqueConstraint("lang_code", "phrase_key", name="uq_phrasebook_lang_key"),
        )
        op.create_index("ix_phrasebook_phrases_lang_code",
                        "phrasebook_phrases", ["lang_code"])
    if not _has_table("regional_languages"):
        op.create_table(
            "regional_languages",
            sa.Column("id", sa.String(80), primary_key=True),
            sa.Column("country", sa.String(2), nullable=False),
            sa.Column("region", sa.String(64), nullable=False),
            sa.Column("lang_code", sa.String(12), nullable=False),
            sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
            sa.UniqueConstraint("country", "region", "lang_code", name="uq_regional_lang"),
        )
        op.create_index("ix_regional_languages_country",
                        "regional_languages", ["country"])


def downgrade() -> None:
    if _has_table("regional_languages"):
        op.drop_table("regional_languages")
    if _has_table("phrasebook_phrases"):
        op.drop_table("phrasebook_phrases")
