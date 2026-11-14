"""Run·dere — the table-ownership registry (SPEC-13 §1, D-241 and D-242).

Every table in `Base.metadata` is listed here once, with:

- its **group** (the sections of SPEC-13 §1.2 to §1.8);
- its **person links**: every column that ties a row to a person, and whether
  it holds a user id or a traveller id;
- its **parent**, for a table reached only through another row;
- what the **export** does with it (D-241, app/archive.py);
- what **deleting an account** does with it (D-242): `app/account_purge.py`
  reads the treatment from here, and refuses to import when a table that an
  account deletion touches has no step.

tests/test_ownership.py fails when a table isn't registered, and when a column
named like a person link (`PERSON_COLUMNS`) isn't declared, so a migration that
adds a table can't merge until someone has decided what export and deletion do
with it (SPEC-13 §1.1).

Rules the map follows (SPEC-13 §1.1): a row with a NULL owner belongs to
nobody, so it is in no archive and no account deletion touches it; rows with a
NULL `traveler_id` in the profile tables are the server's defaults.
"""
from __future__ import annotations

from dataclasses import dataclass

# Column names that tie a row to a person. A column with one of these names
# that a table's entry doesn't declare fails the registry test.
PERSON_COLUMNS = frozenset({"owner_id", "user_id", "author_id", "actor_id", "created_by",
                            "updated_by", "fetched_by", "requested_by", "traveler_id"})

USER, TRAVELLER = "user", "traveller"       # what a person-link column holds

# Groups (SPEC-13 §1.2 to §1.8).
ACCOUNT, PROFILE, OWN, TRIP, TRIP_PERSON, CAPTURE, SERVER = (
    "account", "traveller", "own", "trip", "trip_person", "capture", "server")

# Export treatments (D-241).
EXPORT_NEVER = "never"        # credentials and sign-in data: nothing, ever (D-151)
EXPORT_NO = "no"              # not in format 1 (SPEC-13 §2.6)
EXPORT_OWN = "own"            # the person's own rows, by named fields
EXPORT_TRIP = "trip"          # with its trip: owned in full, shared narrowed (§2.4)
EXPORT_NAMES = "names"        # other people appear as {ref, name, is_me} only

# Deletion treatments (D-242, consumed by wave 9b).
DELETE = "delete"             # the person's rows are deleted
DELETE_WITH_PARENT = "delete_with_parent"   # deleted with the row it hangs off
WITH_TRIP = "with_trip"       # follows its trip (deleted or handed over, §3.4)
UNLINK = "unlink"             # the row stays; the person link is cleared
KEEP_ANONYMISED = "keep_anonymised"         # the row stays without the person
CONDITIONAL = "conditional"   # see the note: depends on what else points at it
UNTOUCHED = "untouched"       # server-wide data


@dataclass(frozen=True)
class Link:
    """One column that ties a row to a person."""
    column: str
    holds: str = USER                    # USER (users.id) or TRAVELLER (traveler_identities.id)


@dataclass(frozen=True)
class Entry:
    table: str
    group: str
    export: str
    deletion: str
    links: tuple[Link, ...] = ()
    parent: str | None = None            # "table.column" a row is reached through
    export_note: str = ""
    deletion_note: str = ""

    @property
    def link_columns(self) -> frozenset[str]:
        return frozenset(link.column for link in self.links)


def _u(*cols: str) -> tuple[Link, ...]:
    return tuple(Link(c, USER) for c in cols)


def _t(*cols: str) -> tuple[Link, ...]:
    return tuple(Link(c, TRAVELLER) for c in cols)


_ENTRIES: tuple[Entry, ...] = (
    # ---------------------------------------------------------------- §1.2 account and sign-in
    Entry("users", ACCOUNT, EXPORT_OWN, DELETE, _t("traveler_id"),
          export_note="username, email, sign-in kind, created date, settings; never the "
                      "hash, the SSO subject or token_version",
          deletion_note="deleted last of all; the row is found by its id"),
    Entry("refresh_tokens", ACCOUNT, EXPORT_NEVER, DELETE, _u("user_id")),
    Entry("password_reset_tokens", ACCOUNT, EXPORT_NEVER, DELETE, _u("user_id")),
    Entry("oauth_codes", ACCOUNT, EXPORT_NEVER, DELETE, _u("user_id")),
    Entry("oauth_grants", ACCOUNT, EXPORT_NEVER, DELETE, _u("user_id")),
    Entry("personal_api_tokens", ACCOUNT, EXPORT_NEVER, DELETE, _u("user_id")),
    Entry("api_keys", ACCOUNT, EXPORT_NEVER, CONDITIONAL, _u("user_id"),
          deletion_note="deleted; a key that a batch of another owner still points at is "
                        "revoked and unlinked instead"),
    Entry("user_integrations", ACCOUNT, EXPORT_NEVER, DELETE, _u("user_id"),
          export_note="nothing from this table (D-151)",
          deletion_note="ciphertext included (D-151)"),
    Entry("inbox_addresses", ACCOUNT, EXPORT_NEVER, DELETE, _u("user_id"),
          export_note="the token is a capability"),
    Entry("account_events", ACCOUNT, EXPORT_OWN, KEEP_ANONYMISED, _u("user_id", "actor_id"),
          export_note="the person's own log: action, time, and who did it as "
                      "you / an admin / the server",
          deletion_note="user_id and detail cleared; where they were the actor, "
                        "actor_id cleared"),
    Entry("ai_calls", ACCOUNT, EXPORT_NO, KEEP_ANONYMISED, _u("user_id"),
          export_note="no content, gone after 90 days",
          deletion_note="user_id cleared; the row still ages out at 90 days"),
    Entry("archive_exports", ACCOUNT, EXPORT_NO, DELETE, _u("user_id"),
          export_note="build bookkeeping for the archive itself (SPEC-13 §2.5)",
          deletion_note="with the person's export folder"),
    Entry("account_deletions", ACCOUNT, EXPORT_NO, KEEP_ANONYMISED,
          _u("user_id", "requested_by"),
          export_note="the request to delete the account; the person sees it on the "
                      "locked page and in their account log",
          deletion_note="the leaver's own record stays without them (user_id and "
                        "choices cleared) so the mailbox step can be retried; in other "
                        "people's records, an admin who asked or an heir who was named "
                        "is cleared"),

    # ---------------------------------------------------------------- §1.3 traveller and profiles
    Entry("traveler_identities", PROFILE, EXPORT_OWN, CONDITIONAL,
          parent="users.traveler_id",
          export_note="the person's own row; companions appear in trip files as names only",
          deletion_note="kept as a guest name when a surviving trip still lists it, with "
                        "every other field cleared; otherwise deleted (SPEC-13 §3.5)"),
    Entry("home_profiles", PROFILE, EXPORT_OWN, DELETE, _t("traveler_id")),
    Entry("packing_profiles", PROFILE, EXPORT_OWN, DELETE, _t("traveler_id")),
    Entry("discovery_profiles", PROFILE, EXPORT_OWN, DELETE, _t("traveler_id")),

    # ---------------------------------------------------------------- §1.4 own-only content
    Entry("journal_entries", OWN, EXPORT_OWN, DELETE, _u("owner_id"),
          deletion_note="including entries attached to other people's trips"),
    Entry("saved_places", OWN, EXPORT_OWN, DELETE, _u("owner_id"),
          export_note="all, dismissed ones marked"),
    Entry("saved_place_filings", OWN, EXPORT_OWN, DELETE, _u("owner_id"),
          export_note="with the place",
          deletion_note="the timeline item it made on someone else's trip stays"),
    Entry("place_lists", OWN, EXPORT_OWN, DELETE, _u("owner_id")),
    Entry("place_list_members", OWN, EXPORT_OWN, DELETE, _u("owner_id"),
          export_note="with the list"),
    Entry("inventory_items", OWN, EXPORT_OWN, DELETE, _u("user_id")),
    Entry("packing_templates", OWN, EXPORT_OWN, DELETE, _u("user_id")),
    Entry("packing_template_items", OWN, EXPORT_OWN, DELETE_WITH_PARENT,
          parent="packing_templates.id", export_note="with the template"),
    Entry("bags", OWN, EXPORT_OWN, DELETE, _u("user_id")),
    Entry("documents", OWN, EXPORT_OWN, DELETE, _u("owner_id"),
          export_note="the files still stored, plus the list of ones already removed "
                      "by retention",
          deletion_note="rows deleted and the person's whole folder removed (D-153, D-196)"),

    # ---------------------------------------------------------------- §1.5 trips
    Entry("trips", TRIP, EXPORT_TRIP, CONDITIONAL, _u("owner_id"),
          export_note="owned: in full; shared: a header only",
          deletion_note="owned: deleted or handed over (SPEC-13 §3.4); others': untouched"),
    Entry("trip_members", TRIP, EXPORT_NO, DELETE, _u("user_id"),
          export_note="sharing lists name other accounts (SPEC-13 §2.6)",
          deletion_note="the person's own shares; shares of a deleted trip go with the trip"),
    Entry("trip_travelers", TRIP, EXPORT_NAMES, WITH_TRIP, _t("traveler_id"),
          deletion_note="on a surviving trip the row stays, now a guest"),
    Entry("trip_activities", TRIP, EXPORT_TRIP, WITH_TRIP, parent="trips.id",
          export_note="owned: all; shared: only items logged from the person's own "
                      "captures (no author column)"),
    Entry("trip_activity_travelers", TRIP, EXPORT_NAMES, WITH_TRIP, _t("traveler_id"),
          parent="trip_activities.id"),
    Entry("trip_expenses", TRIP, EXPORT_TRIP, WITH_TRIP, _u("owner_id"),
          parent="trips.id",
          export_note="owned: the ones they can see (D-183); shared: the ones they "
                      "entered. payer_id is a traveller, exported as a ref",
          deletion_note="on a surviving trip the row stays and owner_id is cleared"),
    Entry("trip_expense_shares", TRIP, EXPORT_NAMES, DELETE_WITH_PARENT, _t("traveler_id"),
          parent="trip_expenses.id", export_note="with the expense"),
    Entry("trip_expense_comments", TRIP, EXPORT_OWN, DELETE, _u("author_id"),
          parent="trip_expenses.id", export_note="their own comments",
          deletion_note="their comments are deleted everywhere"),
    Entry("trip_budget_items", TRIP, EXPORT_TRIP, WITH_TRIP, _u("owner_id"),
          parent="trips.id",
          export_note="owned: the ones they can see (D-183); shared: the ones they entered",
          deletion_note="on a surviving trip: deleted when it was planned only for the "
                        "leaver, otherwise kept with owner_id cleared"),
    Entry("trip_budget_travelers", TRIP, EXPORT_NAMES, DELETE_WITH_PARENT,
          _t("traveler_id"), parent="trip_budget_items.id"),
    Entry("trip_rates", TRIP, EXPORT_TRIP, WITH_TRIP, _u("fetched_by"), parent="trips.id",
          export_note="owned: the rates; never who fetched them",
          deletion_note="on a surviving trip fetched_by is cleared"),

    # ---------------------------------------------------------------- §1.6 per-person trip data
    Entry("discovery_runs", TRIP_PERSON, EXPORT_NO, DELETE, _u("user_id"),
          export_note="run bookkeeping"),
    Entry("discovery_findings", TRIP_PERSON, EXPORT_OWN, DELETE, _u("user_id"),
          export_note="their own, inside the trip file"),
    Entry("discovery_items", TRIP_PERSON, EXPORT_OWN, DELETE, _u("user_id"),
          export_note="with the finding"),
    Entry("discovery_exclusions", TRIP_PERSON, EXPORT_OWN, DELETE, _u("user_id"),
          export_note="with the trip file"),
    Entry("packing_plans", TRIP_PERSON, EXPORT_OWN, DELETE, _u("user_id"),
          export_note="their own, inside the trip file"),
    Entry("packing_items", TRIP_PERSON, EXPORT_OWN, DELETE, _u("user_id"),
          export_note="with the plan"),

    # ---------------------------------------------------------------- §1.7 capture pipeline
    Entry("ingest_batches", CAPTURE, EXPORT_NO, DELETE, _u("user_id"),
          export_note="raw captures are short-lived by design",
          deletion_note="deleted outright, tombstones included (SPEC-13 §3.5)"),
    Entry("travel_items", CAPTURE, EXPORT_NO, DELETE, _u("owner_id"),
          export_note="not in format 1 (SPEC-13 §2.6)",
          deletion_note="including ones filed to other people's trips; the timeline "
                        "items logged from them stay"),
    Entry("item_keys", CAPTURE, EXPORT_NO, DELETE_WITH_PARENT, parent="travel_items.id",
          deletion_note="by an explicit delete, not a cascade"),
    Entry("commit_attempts", CAPTURE, EXPORT_NO, DELETE_WITH_PARENT,
          parent="travel_items.id", deletion_note="by an explicit delete, not a cascade"),
    Entry("artifacts", CAPTURE, EXPORT_NO, CONDITIONAL,
          deletion_note="deleted when no remaining batch or item points at it"),
    Entry("inbox_messages", CAPTURE, EXPORT_NO, DELETE, _u("user_id")),

    # ---------------------------------------------------------------- §1.8 server-wide
    Entry("source_rules", SERVER, EXPORT_NO, UNTOUCHED),
    Entry("rule_settings", SERVER, EXPORT_NO, UNTOUCHED),
    Entry("packing_catalog_items", SERVER, EXPORT_NO, UNTOUCHED),
    Entry("phrasebook_phrases", SERVER, EXPORT_NO, UNTOUCHED),
    Entry("regional_languages", SERVER, EXPORT_NO, UNTOUCHED),
    Entry("server_settings", SERVER, EXPORT_NO, UNLINK, _u("updated_by")),
    Entry("notification_targets", SERVER, EXPORT_NO, UNLINK, _u("created_by")),
    Entry("alert_states", SERVER, EXPORT_NO, CONDITIONAL,
          deletion_note="the person's `integration:<user id>:<kind>` rows are deleted"),
    Entry("oauth_clients", SERVER, EXPORT_NO, UNLINK, _u("created_by"),
          deletion_note="unused self-registered clients are YONDER-309's job"),
)

REGISTRY: dict[str, Entry] = {e.table: e for e in _ENTRIES}
assert len(REGISTRY) == len(_ENTRIES), "a table is registered twice"


def entry(table: str) -> Entry:
    return REGISTRY[table]


def by_group(group: str) -> list[Entry]:
    return [e for e in _ENTRIES if e.group == group]


def never_exported() -> list[str]:
    """The tables an archive takes nothing from, ever (D-151, SPEC-13 §2.1)."""
    return [e.table for e in _ENTRIES if e.export == EXPORT_NEVER]


def problems(metadata) -> list[str]:
    """What the registry is missing for this schema: an unregistered table, a
    registered table that no longer exists, an undeclared person-link column,
    or a declared column the table doesn't have. Empty = complete."""
    out: list[str] = []
    tables = metadata.tables
    for name in sorted(set(tables) - set(REGISTRY)):
        out.append(f"table `{name}` isn't in app/ownership.py: decide what export and "
                   "account deletion do with it (SPEC-13 §1.1)")
    for name in sorted(set(REGISTRY) - set(tables)):
        out.append(f"app/ownership.py lists `{name}`, which is no longer a table")
    for name, e in REGISTRY.items():
        if name not in tables:
            continue
        columns = set(tables[name].columns.keys())
        for col in sorted((columns & PERSON_COLUMNS) - e.link_columns):
            out.append(f"`{name}.{col}` looks like a person link but isn't declared")
        for col in sorted(e.link_columns - columns):
            out.append(f"`{name}.{col}` is declared but isn't a column")
    return out
