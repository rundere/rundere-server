"""SPEC-13 §1.1 — the table-ownership registry (app/ownership.py).

A migration that adds a table, or a column that ties a row to a person, can't
merge until someone has decided what the export (D-241) and account deletion
(D-242) do with it: these tests fail until it's registered."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    import sqlalchemy  # noqa: F401
    DEPS = True
except ImportError:
    DEPS = False

if DEPS:
    from sqlalchemy import Column, MetaData, String, Table

    from app import ownership
    from app.models import Base

    def test_every_table_is_registered():
        assert set(Base.metadata.tables) == set(ownership.REGISTRY)

    def test_every_person_link_column_is_declared():
        missing = [
            f"{name}.{col}" for name, table in Base.metadata.tables.items()
            for col in table.columns.keys()
            if col in ownership.PERSON_COLUMNS
            and col not in ownership.REGISTRY[name].link_columns]
        assert missing == []

    def test_the_registry_has_no_problems():
        assert ownership.problems(Base.metadata) == []

    def test_a_new_table_or_person_column_is_caught():
        md = MetaData()
        Table("brand_new_things", md, Column("id", String, primary_key=True),
              Column("user_id", String))
        Table("journal_entries", md, Column("id", String, primary_key=True),
              Column("owner_id", String), Column("updated_by", String))
        found = ownership.problems(md)
        assert any("brand_new_things" in p for p in found)
        assert any("journal_entries.updated_by" in p for p in found)
        # …and a registered table that vanished is reported too.
        assert any("`users`" in p and "no longer a table" in p for p in found)

    def test_every_entry_has_known_treatments_and_a_way_to_a_person():
        exports = {ownership.EXPORT_NEVER, ownership.EXPORT_NO, ownership.EXPORT_OWN,
                   ownership.EXPORT_TRIP, ownership.EXPORT_NAMES}
        deletions = {ownership.DELETE, ownership.DELETE_WITH_PARENT, ownership.WITH_TRIP,
                     ownership.UNLINK, ownership.KEEP_ANONYMISED, ownership.CONDITIONAL,
                     ownership.UNTOUCHED}
        for e in ownership.REGISTRY.values():
            assert e.export in exports and e.deletion in deletions, e.table
            if e.group == ownership.SERVER:
                assert e.export == ownership.EXPORT_NO, e.table
            elif e.table not in ("users", "artifacts"):
                # Personal data is reached by a person column or through a parent row.
                assert e.links or e.parent, e.table
            if e.parent:
                assert e.parent.split(".")[0] in ownership.REGISTRY, e.table

    def test_credentials_and_sign_in_data_are_never_exported():
        """The eight tables SPEC-13 §1.2 marks "Never" (D-151)."""
        assert set(ownership.never_exported()) == {
            "refresh_tokens", "password_reset_tokens", "oauth_codes", "oauth_grants",
            "personal_api_tokens", "api_keys", "user_integrations", "inbox_addresses"}

    def test_the_archive_reads_nothing_from_a_never_table():
        """The exporter's imports are its reach: none of the "never" models."""
        import app.archive as archive
        never = {t for t in ownership.never_exported()}
        models = {m.__tablename__ for m in vars(archive).values()
                  if isinstance(m, type) and hasattr(m, "__tablename__")}
        assert not (models & never)

    def test_the_export_record_is_own_only_and_goes_with_the_account():
        e = ownership.entry("archive_exports")
        assert (e.export, e.deletion) == (ownership.EXPORT_NO, ownership.DELETE)
        assert e.link_columns == {"user_id"}
