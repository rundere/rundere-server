# TravelerIdentity spine + trip travellers

Implementation: `app/models.py` (data model), `alembic/versions/0014_traveler_spine.py`
(migration), `app/trips/routes.py` + `app/trips/templates/_travelers_section.html`
(the "Who's traveling" UI).
Tests: `tests/test_trips.py` (travellers + guest sections).

Run·dere 3.2, phases 1–2 of `SPEC-3.2-discovery-llm.md`. This lands the person
data model the LLM discovery tier will read, plus the trip-level UI to populate
it. No resolver consumes it yet — that is phase 3 onward.

## The model: one person record everything hangs off

`TravelerIdentity` (introduced in 3.0 for the flight-review identity matcher) is
promoted to Run·dere's **central person record**. Every per-person table links to
it by a nullable `traveler_id`; trip membership is a structured association, not
a free-text hint.

```
TravelerIdentity (the person)
  ├── HomeProfile[]        home locations over time      (traveler_id added)
  ├── PackingProfile       one per person                (traveler_id retyped)
  ├── DiscoveryProfile     one per person — new table
  ├── TripTraveler[]       which trips this person is on — new table
  └── nationality, generic identity facts
```

### Columns and tables (migration 0014, head was 0013)

- `TravelerIdentity.nationality` — ISO-2 (`String(2)`), used by the future visa
  resolver. NULL skips visa research with a visible prompt.
- `TravelerIdentity.generic` — boolean, default false. Marks a placeholder guest
  (see below).
- `HomeProfile.traveler_id` — `Integer`, indexed, nullable. NULL keeps a home as
  the deployment-wide default (backward compatible: the `self` traveller inherits
  all NULL homes until a UI assigns them).
- `PackingProfile.traveler_id` — **retyped `String(36)` → `Integer`**. It was
  always NULL to date, so the conversion carries no data; this ends a pre-existing
  mismatch against the `Integer` PK.
- `DiscoveryProfile` — new table (`id`, `traveler_id`, `values` JSON,
  timestamps), peer of `PackingProfile`. Preferences live in the `values` blob so
  adding a field is a data change, never a migration.
- `TripTraveler` — new association table: `(trip_id, traveler_id)` PK + `role`
  (`self`|`companion`).

### traveler_id is a soft link, not a hard FK

`traveler_id` columns are plain `Integer` columns with **no** `ForeignKey`
constraint — the established house convention (as `PackingProfile` already used).
NULL means "deployment-wide default" while Run·dere is single-user. The type is
`Integer` to match `traveler_identities.id`; the earlier `String(36)` on
`PackingProfile` was a soft-link that could never actually match the integer PK.

### Migration is doubly guarded

`0014` reaches head on both paths:

- **Fresh DB** — the `0001` baseline runs `create_all` from the *current* models,
  so a new database already has every column and table. Each step guards with
  `_has_column` / `_has_table` and no-ops.
- **Existing (production) DB** — the columns/tables are genuinely absent and get
  added. The `PackingProfile.traveler_id` retype only fires when the column is
  still a character type (`_column_type(...)` contains `CHAR`), and splits by
  dialect: Postgres `ALTER ... USING traveler_id::integer`, SQLite `batch_alter_table`
  (which rebuilds the table). `downgrade()` reverses every step, including the
  type, so the round-trip is clean.

## Trip travellers UI — "Who's traveling"

The trip detail page gains a roster section (`_travelers_section.html`, included
after the trip header). It reads `TripTraveler` and renders the linked
`TravelerIdentity` rows, self first.

- **Auto-link self.** Creating a trip flushes the row, then links the `self`
  identity as `role="self"` (`_auto_link_self`). No-op if there is no self
  identity yet.
- **Add a saved person.** A dropdown of identities not already on the trip
  (`POST /trips/{id}/travelers`, idempotent, guards a bad id).
- **Add a guest by name.** A text field mints a minimal identity and links it
  (`POST /trips/{id}/travelers/new`) — see below.
- **Remove.** `POST /trips/{id}/travelers/{traveler_id}/remove`.
- **Manage.** Links to the SQLAdmin travellers list (`/admin/traveler-identity/list`).

All mutations swap just `#travelers-section` via HTMX — no page reload.
`delete_trip` explicitly sweeps `trip_travelers` (SQLite does not enforce
`ON DELETE CASCADE` without `PRAGMA foreign_keys`, same caveat as activities).

## Generic guests (D-100)

The quick-add-by-name path exists so you can record "someone is coming" without
building a whole person. To keep that from polluting the identity table with
consideration-encoding junk ("female guest", "old guest"), a quick-added guest is
explicitly a **placeholder**:

- Minted as `TravelerIdentity(kind="guest", generic=True)` — no profile, no
  nationality.
- **Non-duplicate.** `display_name` is `UNIQUE`; quick-add reuses an existing row
  by name rather than minting a second. Reusing a *real* (non-generic) identity
  never flips its flag.
- **Ignored by discovery and planning.** Resolvers read the roster through
  `discovery_travelers(session, trip_id)`, which returns every roster member
  **except** generic guests. A guest name therefore cannot leak into a
  recommendation, a nationality lookup, or packing math. This helper is the
  contract phase 3+ resolvers consume.
- The roster shows a `guest` badge on generic rows.

To make a companion actually count for discovery, give them a real profile — not
a guest name.

## Discovery Profile editor

Implementation: `app/trips/routes.py` (`edit_discovery_profile` /
`save_discovery_profile`, `/trips/settings/discovery-profile`),
`app/trips/templates/discovery_profile.html`, nav link in
`app/review/templates/_appheader.html`.
Tests: `tests/test_discovery_profile.py`.

A structured form (checkboxes / radios / a select — never raw JSON), peer of the
Packing Profile page and linked from the same Administer nav group. It reads and
writes `DiscoveryProfile.values`, single-user for now (the first row is the
deployment-wide default, same convention as the packing profile).

**Constraint-first (D-87).** The blob is a filter, not a wish list:

- **Subtract / bound** — dietary restrictions (checkboxes; results must
  accommodate), cuisines to avoid (comma-separated free text), allergies (free
  text, flagged as a warning not a filter), budget ceiling (radio, `$/$$/$$$/$$$$`
  or no limit — D-88, stored as an int 0–4).
- **Add** — drink interests and activity interests (opt-in checkboxes; unchecked
  is simply not searched). Activity interests double as the curation lens for
  suggested events.
- **Practical** — mobility (a select).

The `values` shape matches SPEC-3.2 §2.2 (`food` / `drink` / `activity` /
`practical`). A blank form saves all defaults — empty lists, budget 0, mobility
`no_limits` — meaning "show me everything." On save, closed-vocabulary fields
(drink/activity interests, mobility, budget) are filtered to known keys so the
blob stays clean; dietary restrictions are not filtered because the model allows
custom strings (D-93). No resolver reads the profile yet — that is phase 6+.
