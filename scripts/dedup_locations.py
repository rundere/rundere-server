#!/usr/bin/env python3
"""AdventureLog Location duplicate DETECTOR (Phase 1 — read-only, no deletes).

Reads a pg_dump SQL backup, groups Locations that are the SAME PLACE
(same user + same name + coordinates within a tunable radius), picks a master
per group, and writes a review CSV. Nothing is mutated: this only produces the
list a human approves before the consolidation transaction is written.

Why name+coords and not name alone: name-only collapses chains (Starbucks x25,
Home Depot x12) that are legitimately distinct Swarm places. See the project
memory `adventurelog-location-dedup`.

Usage:
  python3 scripts/dedup_locations.py \
      --dump adventurelog-backup-20260806.sql \
      --radius-m 110 \
      --out scratch/location_dups_review.csv

Options:
  --radius-m N     max haversine distance (metres) between members  [110]
  --cross-user     also match identical name+coords across users    [off]
  --dump PATH      pg_dump .sql file to read                        [latest backup]
  --out PATH       review CSV path
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import sys
from collections import defaultdict


# ---- column layouts (0-based), from the CREATE TABLE statements -------------
LOC = dict(id=0, name=1, location=2, tags=3, description=4, rating=5, link=6,
           is_public=7, longitude=8, latitude=9, created_at=10, updated_at=11,
           user_id=12, category_id=13, city_id=14, country_id=15, region_id=16)
VISIT_LOCATION_ID = 6
M2M_LOCATION_ID, M2M_COLLECTION_ID = 1, 2
NULL = "\\N"


def copy_rows(dump_path: str, table: str):
    """Yield tab-split rows from a `COPY public.<table> ... FROM stdin;` block."""
    cap = False
    with open(dump_path, encoding="utf-8", errors="replace") as fh:
        for ln in fh:
            if not cap:
                if ln.startswith(f"COPY public.{table} "):
                    cap = True
                continue
            if ln.startswith("\\."):
                break
            yield ln.rstrip("\n").split("\t")


def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def fnum(v):
    if v == NULL or v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def cluster(members, radius_m):
    """Greedy single-link clustering of members within `radius_m`.
    members: list of dicts with lat/lon (already non-null). Returns list of
    clusters (each a list of members)."""
    clusters = []
    for m in members:
        placed = False
        for c in clusters:
            if any(haversine_m(m["lat"], m["lon"], o["lat"], o["lon"]) <= radius_m
                   for o in c):
                c.append(m)
                placed = True
                break
        if not placed:
            clusters.append([m])
    return clusters


def richness(m):
    """How much metadata a location carries — used to pick the master."""
    score = 0
    for k in ("category_id", "city_id", "country_id", "region_id",
              "description", "rating"):
        if m["raw"].get(k) not in (None, NULL, ""):
            score += 1
    if m["tags"]:
        score += 1
    return score


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", default=None)
    ap.add_argument("--radius-m", type=float, default=110.0)
    ap.add_argument("--cross-user", action="store_true")
    ap.add_argument("--out", default="scratch/location_dups_review.csv")
    ap.add_argument("--emit-sql", default=None,
                    help="also write the consolidation transaction to this path")
    args = ap.parse_args(argv)

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    dump = args.dump
    if dump is None:
        cands = sorted(f for f in os.listdir(here)
                       if f.startswith("adventurelog-backup-") and f.endswith(".sql"))
        if not cands:
            sys.exit("no adventurelog-backup-*.sql found; pass --dump")
        dump = os.path.join(here, cands[-1])
    if not os.path.isabs(dump):
        dump = os.path.join(here, dump)

    # ---- load reference data ------------------------------------------------
    coll_name = {}
    for r in copy_rows(dump, "adventures_collection"):
        if len(r) > 1:
            coll_name[r[0]] = r[1]

    user_name = {}
    for r in copy_rows(dump, "users_customuser"):
        if len(r) > 4:
            user_name[r[0]] = r[4]

    visits_per_loc = defaultdict(int)
    for r in copy_rows(dump, "adventures_visit"):
        if len(r) > VISIT_LOCATION_ID:
            visits_per_loc[r[VISIT_LOCATION_ID]] += 1

    colls_per_loc = defaultdict(set)
    for r in copy_rows(dump, "adventures_location_collections"):
        if len(r) > M2M_COLLECTION_ID:
            colls_per_loc[r[M2M_LOCATION_ID]].add(r[M2M_COLLECTION_ID])

    # ---- load locations -----------------------------------------------------
    locs = []
    null_coord = []
    for c in copy_rows(dump, "adventures_location"):
        if len(c) <= LOC["user_id"]:
            continue
        raw = {k: c[i] for k, i in LOC.items()}
        lat, lon = fnum(c[LOC["latitude"]]), fnum(c[LOC["longitude"]])
        m = dict(id=c[LOC["id"]], name=c[LOC["name"]], user=c[LOC["user_id"]],
                 lat=lat, lon=lon, created_at=c[LOC["created_at"]],
                 tags=c[LOC["tags"]] not in (None, NULL, "", "{}"), raw=raw)
        if lat is None or lon is None:
            null_coord.append(m)
        else:
            locs.append(m)

    total_locs = len(locs) + len(null_coord)

    # ---- bucket + cluster ---------------------------------------------------
    buckets = defaultdict(list)
    for m in locs:
        key = (m["name"],) if args.cross_user else (m["user"], m["name"])
        buckets[key].append(m)

    groups = []  # list of clusters with >1 member
    for key, members in buckets.items():
        if len(members) < 2:
            continue
        for cl in cluster(members, args.radius_m):
            if len(cl) > 1:
                groups.append(cl)

    # ---- choose master + write CSV -----------------------------------------
    os.makedirs(os.path.dirname(os.path.join(here, args.out)) or ".", exist_ok=True)
    out_path = args.out if os.path.isabs(args.out) else os.path.join(here, args.out)

    removable = 0
    max_span = 0.0
    rows_out = []
    merge_pairs = []  # (dup_id, master_id) — single source of truth for the SQL
    groups.sort(key=lambda cl: (-len(cl), cl[0]["name"]))
    for gi, cl in enumerate(groups, 1):
        # master: most collection links, then richest metadata, then oldest
        def sort_key(m):
            return (len(colls_per_loc.get(m["id"], ())), richness(m),
                    -_iso_rank(m["created_at"]))
        master = max(cl, key=sort_key)
        for m in cl:
            if m["id"] != master["id"]:
                merge_pairs.append((m["id"], master["id"]))
        # widest pairwise distance in the group (sanity signal for reviewer)
        span = max((haversine_m(a["lat"], a["lon"], b["lat"], b["lon"])
                    for a in cl for b in cl), default=0.0)
        max_span = max(max_span, span)
        removable += len(cl) - 1
        for m in sorted(cl, key=lambda x: x["id"] != master["id"]):
            cids = colls_per_loc.get(m["id"], set())
            rows_out.append({
                "group": gi,
                "group_size": len(cl),
                "role": "MASTER" if m["id"] == master["id"] else "dup",
                "name": m["name"],
                "user": user_name.get(m["user"], m["user"]),
                "location_id": m["id"],
                "latitude": f'{m["lat"]:.6f}',
                "longitude": f'{m["lon"]:.6f}',
                "group_span_m": f"{span:.0f}",
                "visits": visits_per_loc.get(m["id"], 0),
                "collection_links": len(cids),
                "collections": " | ".join(sorted(coll_name.get(x, x) for x in cids)),
                "metadata_score": richness(m),
                "created_at": m["created_at"],
            })

    fields = ["group", "group_size", "role", "name", "user", "location_id",
              "latitude", "longitude", "group_span_m", "visits",
              "collection_links", "collections", "metadata_score", "created_at"]
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows_out)

    # ---- optional: emit consolidation SQL -----------------------------------
    if args.emit_sql:
        dup_ids = {d for d, _ in merge_pairs}
        # dependency footprint over the dup set (for the run summary)
        dep_visits = sum(visits_per_loc.get(d, 0) for d in dup_ids)
        dep_links = sum(len(colls_per_loc.get(d, ())) for d in dup_ids)
        img_obj = [c for c in copy_rows(dump, "adventures_contentimage") if c and c[-1] in dup_ids]
        att_obj = [c for c in copy_rows(dump, "adventures_contentattachment") if c and c[-1] in dup_ids]
        # itinerary item: object_id at col 1, content_type_id at col 6
        itin = [c for c in copy_rows(dump, "adventures_collectionitineraryitem")
                if len(c) > 1 and c[1] in dup_ids]
        sql_path = args.emit_sql if os.path.isabs(args.emit_sql) else os.path.join(here, args.emit_sql)
        _write_sql(sql_path, merge_pairs, dump, here, args.radius_m,
                   len(groups), total_locs)
        print(f"\ndependency footprint on the {len(dup_ids)} dup locations:")
        print(f"  visits repointed        : {dep_visits}")
        print(f"  collection links merged  : {dep_links}")
        print(f"  content images repointed : {len(img_obj)}")
        print(f"  attachments repointed    : {len(att_obj)}")
        print(f"  itinerary items repointed: {len(itin)}")
        print(f"migration SQL     : {os.path.relpath(sql_path, here)}")

    # ---- summary ------------------------------------------------------------
    print(f"dump              : {os.path.relpath(dump, here)}")
    print(f"match rule        : name + coords"
          f"{'' if args.cross_user else ' + user'}, radius {args.radius_m:.0f} m")
    print(f"total locations   : {total_locs}")
    print(f"duplicate groups  : {len(groups)}")
    print(f"removable rows    : {removable}  "
          f"(locations after merge: {total_locs - removable})")
    print(f"widest group span : {max_span:.0f} m")
    print(f"null-coord locs   : {len(null_coord)} (excluded from matching)")
    print(f"review CSV        : {os.path.relpath(out_path, here)}  "
          f"({len(rows_out)} rows)")


_SQL_COLS_FILL = ["category_id", "city_id", "country_id", "region_id",
                  "description", "rating"]


def _write_sql(path, pairs, dump, here, radius_m, n_groups, total_locs):
    """Emit a single self-verifying transaction that consolidates every dup onto
    its master. Fill-only metadata union (never overwrites), repoints every known
    relation to Location — hard FKs (visit, trail, collection M2M) and Django
    generic relations (content image/attachment, itinerary item) — then deletes
    the dups. Any broken invariant RAISES, rolling the whole thing back."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    values = ",\n".join(f"  ('{d}','{m}')" for d, m in pairs)

    fill_blocks = []
    for c in _SQL_COLS_FILL:
        fill_blocks.append(f"""UPDATE adventures_location m SET {c} = pick.v
FROM (SELECT map.master_id,
             (array_agg(d.{c} ORDER BY d.created_at, d.id)
                FILTER (WHERE d.{c} IS NOT NULL))[1] AS v
      FROM location_merge_map map JOIN adventures_location d ON d.id = map.dup_id
      GROUP BY map.master_id) pick
WHERE m.id = pick.master_id AND m.{c} IS NULL AND pick.v IS NOT NULL;""")
    fill_sql = "\n\n".join(fill_blocks)

    body = f"""-- AdventureLog Location consolidation (Phases 3-5) — GENERATED, do not hand-edit.
-- Source dump : {os.path.relpath(dump, here)}
-- Match rule  : name + user + coordinates <= {radius_m:.0f} m
-- Groups      : {n_groups}   Dups removed: {len(pairs)}   Locations: {total_locs} -> {total_locs - len(pairs)}
-- Regenerate  : python3 scripts/dedup_locations.py --radius-m {radius_m:.0f} --emit-sql <this file>
--
-- Runs as ONE transaction. Every invariant is asserted; a failure RAISES and
-- rolls everything back, leaving the database untouched. Run against a RESTORED
-- COPY first, diff, then apply to production.

\\set ON_ERROR_STOP on
BEGIN;

CREATE TEMP TABLE location_merge_map (dup_id uuid PRIMARY KEY, master_id uuid NOT NULL)
  ON COMMIT DROP;
INSERT INTO location_merge_map (dup_id, master_id) VALUES
{values};

-- Location model's content_type, resolved live (portable across installs).
CREATE TEMP TABLE _loc_ct ON COMMIT DROP AS
  SELECT id AS ct FROM django_content_type
   WHERE app_label = 'adventures' AND model = 'location';

-- ---------------------------------------------------------------- pre-flight
DO $$
DECLARE n int; loc_ct int;
BEGIN
  SELECT ct INTO loc_ct FROM _loc_ct;
  IF loc_ct IS NULL THEN RAISE EXCEPTION 'pre-flight: Location content_type not found'; END IF;

  SELECT count(*) INTO n FROM location_merge_map map
   WHERE NOT EXISTS (SELECT 1 FROM adventures_location l WHERE l.id = map.dup_id);
  IF n > 0 THEN RAISE EXCEPTION 'pre-flight: % dup id(s) absent (dump drift)', n; END IF;

  SELECT count(*) INTO n FROM location_merge_map map
   WHERE NOT EXISTS (SELECT 1 FROM adventures_location l WHERE l.id = map.master_id);
  IF n > 0 THEN RAISE EXCEPTION 'pre-flight: % master id(s) absent', n; END IF;

  SELECT count(*) INTO n FROM location_merge_map a JOIN location_merge_map b
    ON a.master_id = b.dup_id;
  IF n > 0 THEN RAISE EXCEPTION 'pre-flight: % id(s) are both dup and master (chain)', n; END IF;

  SELECT count(*) INTO n FROM location_merge_map map
    JOIN adventures_location d  ON d.id  = map.dup_id
    JOIN adventures_location ma ON ma.id = map.master_id
   WHERE d.user_id <> ma.user_id;
  IF n > 0 THEN RAISE EXCEPTION 'pre-flight: % pair(s) cross a user boundary', n; END IF;
END $$;

-- ------------------------------------------- 1. metadata union (fill NULLs only)
{fill_sql}

-- tags: only when the master carries none — take the earliest dup that has some
UPDATE adventures_location m SET tags = pick.tags
FROM (SELECT DISTINCT ON (map.master_id) map.master_id, d.tags
      FROM location_merge_map map JOIN adventures_location d ON d.id = map.dup_id
      WHERE d.tags IS NOT NULL AND array_length(d.tags, 1) > 0
      ORDER BY map.master_id, d.created_at, d.id) pick
WHERE m.id = pick.master_id
  AND (m.tags IS NULL OR array_length(m.tags, 1) IS NULL);

-- ---------------------------------------------------- 2. hard FKs -> master
UPDATE adventures_visit v SET location_id = map.master_id
  FROM location_merge_map map WHERE v.location_id = map.dup_id;

UPDATE adventures_trail t SET location_id = map.master_id
  FROM location_merge_map map WHERE t.location_id = map.dup_id;

-- ------------------------------- 3. collection M2M — honor UNIQUE(loc,coll)
-- Drop the dup's link where the master already sits in that collection,
-- then repoint the survivors. Net effect: union of collection memberships.
DELETE FROM adventures_location_collections lc USING location_merge_map map
 WHERE lc.location_id = map.dup_id
   AND EXISTS (SELECT 1 FROM adventures_location_collections k
                WHERE k.location_id = map.master_id
                  AND k.collection_id = lc.collection_id);

UPDATE adventures_location_collections lc SET location_id = map.master_id
  FROM location_merge_map map WHERE lc.location_id = map.dup_id;

-- --------------------------- 4. Django generic relations (content_type = Location)
UPDATE adventures_contentimage ci SET object_id = map.master_id
  FROM location_merge_map map
 WHERE ci.object_id = map.dup_id AND ci.content_type_id = (SELECT ct FROM _loc_ct);

UPDATE adventures_contentattachment ca SET object_id = map.master_id
  FROM location_merge_map map
 WHERE ca.object_id = map.dup_id AND ca.content_type_id = (SELECT ct FROM _loc_ct);

UPDATE adventures_collectionitineraryitem it SET object_id = map.master_id
  FROM location_merge_map map
 WHERE it.object_id = map.dup_id AND it.content_type_id = (SELECT ct FROM _loc_ct);

-- ------------------------------------------------------- 5. delete the dups
DELETE FROM adventures_location l USING location_merge_map map
 WHERE l.id = map.dup_id;

-- --------------------------------------------------- 6. post-flight invariants
DO $$
DECLARE n int; loc_ct int;
BEGIN
  SELECT ct INTO loc_ct FROM _loc_ct;
  SELECT count(*) INTO n FROM adventures_visit v
    JOIN location_merge_map map ON v.location_id = map.dup_id;
  IF n > 0 THEN RAISE EXCEPTION 'post: % visit(s) still on a dup', n; END IF;

  SELECT count(*) INTO n FROM adventures_location_collections lc
    JOIN location_merge_map map ON lc.location_id = map.dup_id;
  IF n > 0 THEN RAISE EXCEPTION 'post: % collection link(s) still on a dup', n; END IF;

  SELECT count(*) INTO n FROM adventures_trail t
    JOIN location_merge_map map ON t.location_id = map.dup_id;
  IF n > 0 THEN RAISE EXCEPTION 'post: % trail(s) still on a dup', n; END IF;

  SELECT count(*) INTO n FROM adventures_contentimage ci
    JOIN location_merge_map map ON ci.object_id = map.dup_id
   WHERE ci.content_type_id = loc_ct;
  IF n > 0 THEN RAISE EXCEPTION 'post: % image(s) still on a dup', n; END IF;

  SELECT count(*) INTO n FROM adventures_contentattachment ca
    JOIN location_merge_map map ON ca.object_id = map.dup_id
   WHERE ca.content_type_id = loc_ct;
  IF n > 0 THEN RAISE EXCEPTION 'post: % attachment(s) still on a dup', n; END IF;

  SELECT count(*) INTO n FROM adventures_collectionitineraryitem it
    JOIN location_merge_map map ON it.object_id = map.dup_id
   WHERE it.content_type_id = loc_ct;
  IF n > 0 THEN RAISE EXCEPTION 'post: % itinerary item(s) still on a dup', n; END IF;

  SELECT count(*) INTO n FROM adventures_location l
    JOIN location_merge_map map ON l.id = map.dup_id;
  IF n > 0 THEN RAISE EXCEPTION 'post: % dup location(s) survived deletion', n; END IF;

  IF (SELECT count(*) FROM location_merge_map) <> {len(pairs)} THEN
    RAISE EXCEPTION 'post: merge map size drift';
  END IF;
  RAISE NOTICE 'consolidation OK: % dups merged, all references clean', {len(pairs)};
END $$;

COMMIT;
"""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)


def _iso_rank(ts):
    """Sortable rank for a timestamp string; smaller = older. Best-effort so the
    oldest record wins the master tie-break."""
    if not ts or ts == NULL:
        return float("inf")
    d = "".join(ch for ch in ts if ch.isdigit())
    return int(d[:14].ljust(14, "0")) if d else float("inf")


if __name__ == "__main__":
    main()
