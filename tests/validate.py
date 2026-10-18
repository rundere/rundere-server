#!/usr/bin/env python3
"""
Subset JSON Schema (2020-12) validator + TravelItem business rules.

The validator supports exactly the keywords used by travel-item.schema.json:
  $ref (local), type, required, properties, additionalProperties, enum, const,
  pattern, anyOf, allOf, oneOf, if/then, items, minimum, maximum, minItems,
  format (uuid, date-time, date).

On the server, replace validate_schema() with the real `jsonschema` library.
check_rules() has no equivalent and must be kept.
"""
import collections, json, re, sys, uuid
from datetime import datetime, date

# ---------------------------------------------------------------- schema core

class V:
    def __init__(self, root):
        self.root = root
        self.errors = []

    def deref(self, s):
        seen = 0
        while isinstance(s, dict) and "$ref" in s:
            ref = s["$ref"]
            assert ref.startswith("#/"), f"only local refs supported: {ref}"
            node = self.root
            for part in ref[2:].split("/"):
                node = node[part]
            s = node
            seen += 1
            if seen > 20:
                raise RuntimeError("ref cycle")
        return s

    def err(self, path, msg):
        self.errors.append(f"{path or '<root>'}: {msg}")

    def check_type(self, inst, t):
        if isinstance(t, list):
            return any(self.check_type(inst, x) for x in t)
        if t == "null":    return inst is None
        if t == "string":  return isinstance(inst, str)
        if t == "integer": return isinstance(inst, int) and not isinstance(inst, bool)
        if t == "number":  return isinstance(inst, (int, float)) and not isinstance(inst, bool)
        if t == "boolean": return isinstance(inst, bool)
        if t == "array":   return isinstance(inst, list)
        if t == "object":  return isinstance(inst, dict)
        raise ValueError(t)

    def check_format(self, inst, f, path):
        if not isinstance(inst, str):
            return
        try:
            if f == "uuid":
                uuid.UUID(inst)
            elif f == "date-time":
                datetime.fromisoformat(inst.replace("Z", "+00:00"))
            elif f == "date":
                date.fromisoformat(inst)
        except Exception:
            self.err(path, f"invalid {f}: {inst!r}")

    def ok(self, inst, schema):
        """Silent boolean sub-validation, used by anyOf/oneOf/if."""
        sub = V(self.root)
        sub.validate(inst, schema, "")
        return not sub.errors

    def validate(self, inst, schema, path=""):
        schema = self.deref(schema)
        if not isinstance(schema, dict):
            return

        if "type" in schema and not self.check_type(inst, schema["type"]):
            self.err(path, f"expected type {schema['type']}, got {type(inst).__name__}")
            return

        if "const" in schema and inst != schema["const"]:
            self.err(path, f"expected const {schema['const']!r}, got {inst!r}")
        if "enum" in schema and inst not in schema["enum"]:
            self.err(path, f"{inst!r} not in enum {schema['enum']}")
        if "pattern" in schema and isinstance(inst, str):
            if not re.search(schema["pattern"], inst):
                self.err(path, f"{inst!r} fails pattern {schema['pattern']}")
        if "format" in schema:
            self.check_format(inst, schema["format"], path)
        if isinstance(inst, (int, float)) and not isinstance(inst, bool):
            if "minimum" in schema and inst < schema["minimum"]:
                self.err(path, f"{inst} < minimum {schema['minimum']}")
            if "maximum" in schema and inst > schema["maximum"]:
                self.err(path, f"{inst} > maximum {schema['maximum']}")

        if isinstance(inst, list):
            if "minItems" in schema and len(inst) < schema["minItems"]:
                self.err(path, f"needs >= {schema['minItems']} items")
            if "items" in schema:
                for i, el in enumerate(inst):
                    self.validate(el, schema["items"], f"{path}[{i}]")

        if isinstance(inst, dict):
            props = schema.get("properties", {})
            for r in schema.get("required", []):
                if r not in inst:
                    self.err(path, f"missing required property {r!r}")
            addl = schema.get("additionalProperties", True)
            for k, v in inst.items():
                if k in props:
                    self.validate(v, props[k], f"{path}.{k}")
                elif addl is False:
                    self.err(path, f"additional property {k!r} not allowed")
                elif isinstance(addl, dict):
                    self.validate(v, addl, f"{path}.{k}")

        for kw in ("allOf",):
            for sub in schema.get(kw, []):
                self.validate(inst, sub, path)
        if "anyOf" in schema:
            branch_errs = []
            for s in schema["anyOf"]:
                sub = V(self.root)
                sub.validate(inst, s, path)
                if not sub.errors:
                    branch_errs = None
                    break
                branch_errs.append(sub.errors)
            if branch_errs is not None:
                # Report the closest branch rather than an opaque "matched nothing".
                # Ignore trivial `{"type":"null"}` branches — they always fail with
                # one useless error and would otherwise win on error count.
                substantive = [b for b in branch_errs
                               if not (len(b) == 1 and "expected type null" in b[0])]
                best = min(substantive or branch_errs, key=len)
                self.errors.extend(best)
        if "oneOf" in schema:
            n = sum(1 for s in schema["oneOf"] if self.ok(inst, s))
            if n != 1:
                self.err(path, f"oneOf matched {n} branches, expected 1")
        if "if" in schema:
            if self.ok(inst, schema["if"]):
                if "then" in schema:
                    self.validate(inst, schema["then"], path)
            elif "else" in schema:
                self.validate(inst, schema["else"], path)


def validate_schema(instance, schema):
    v = V(schema)
    v.validate(instance, schema, "")
    return v.errors


# ------------------------------------------------------- business rules (A1b)
# These cannot be expressed in JSON Schema. They are the encoded scar tissue
# from the TripIt -> AirTrail import and the AdventureLog collection model.

TWO_SLASH_TZ = re.compile(r"^[^/]+/[^/]+/[^/]+$")


def _dt_key(d):
    """Sortable key for a DateTimeLocal, ignoring tz. None if not comparable."""
    if not d or d.get("precision") != "datetime":
        return None
    return d["local"]


def check_rules(item):
    e = []
    p = (item.get("extraction") or {}).get("payload") or {}
    t = p.get("item_type")

    def movements(pairs):
        for label, a, b in pairs:
            if not a or not b:
                continue
            ka, kb = _dt_key(a.get("time") or a), _dt_key(b.get("time") or b)
            if ka and kb and kb < ka:
                e.append(f"RULE-01 {label}: arrival/end {kb} precedes departure/start {ka} "
                         f"(AirTrail aborts the ENTIRE batch on this)")
            if ka and kb and ka == kb:
                e.append(f"RULE-02 {label}: identical start/end {ka} — the exact TripIt "
                         f"midnight-stub failure mode")

    if t == "flight":
        f = p.get("flight") or {}
        movements([("flight", f.get("departure"), f.get("arrival"))])
        for side in ("departure", "arrival"):
            m = f.get(side) or {}
            pl = m.get("place") or {}
            if not pl.get("iata") and not pl.get("icao"):
                e.append(f"RULE-03 flight.{side}.place: no IATA/ICAO — AirTrail resolves "
                         f"airports by ICAO; this will land in unknownAirports")
            tm = m.get("time") or {}
            if tm.get("precision") != "datetime":
                e.append(f"RULE-04 flight.{side}.time: precision={tm.get('precision')} — "
                         f"must be needs_review, never defaulted")

    if t == "ground_transport":
        g = p.get("ground_transport") or {}
        movements([("ground", g.get("departure"), g.get("arrival"))])

    if t == "lodging":
        l = p.get("lodging") or {}
        ci, co = l.get("check_in"), l.get("check_out")
        if ci and co:
            a, b = ci.get("local"), co.get("local")
            if a and b and b < a:
                e.append(f"RULE-05 lodging: check_out {b} precedes check_in {a}")
            if a and b and a == b:
                e.append("RULE-06 lodging: zero-night stay — verify")

    if t == "event":
        ev = p.get("event") or {}
        movements([("event", {"time": ev.get("start")}, {"time": ev.get("end")} if ev.get("end") else None)])

    # timezone hygiene, all types
    def walk_tz(node, path=""):
        if isinstance(node, dict):
            tz = node.get("tz")
            if isinstance(tz, str) and TWO_SLASH_TZ.match(tz):
                e.append(f"RULE-07 {path}.tz: {tz!r} has two slashes — AirTrail's regex "
                         f"rejects it; map to a backward-compatible alias")
            for k, v in node.items():
                walk_tz(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk_tz(v, f"{path}[{i}]")
    walk_tz(p, "payload")

    # AdventureLog collection-exclusivity
    res = item.get("resolution") or {}
    targets = res.get("targets") or []
    exclusive = {"adventurelog.transportation", "adventurelog.lodging", "adventurelog.note"}
    if exclusive & set(targets) and not res.get("collection_id"):
        if res.get("outcome") == "resolved":
            e.append("RULE-08 AdventureLog transportation/lodging/note are collection-exclusive; "
                     "outcome=resolved with no collection_id is unwritable")

    # only flights may mint a collection, and only when none already exists
    if res.get("collection_action") == "created":
        if t != "flight":
            e.append(f"RULE-09 collection_action=created on item_type={t}"
                     f"{'/' + str((p.get('ground_transport') or {}).get('mode')) if t == 'ground_transport' else ''}"
                     f"; ONLY flights mint collections — everything else attaches or defers")
        if res.get("candidate_collections"):
            e.append(f"RULE-30 collection_action=created but "
                     f"{len(res['candidate_collections'])} candidate collection(s) matched; "
                     f"mint only if none exists")
        if res.get("match_attempted") is not True:
            e.append("RULE-30b collection_action=created without match_attempted=true — "
                     "cannot claim nothing matched without running the ladder")
    if res.get("anchor_eligible") is True and t != "flight":
        e.append(f"RULE-09b anchor_eligible=true on item_type={t}; only flights anchor trips")

    # ------------------------------------------------- capture-time metadata
    src = item.get("source") or {}
    cc = src.get("capture_context") or {}
    ext = (item.get("extraction") or {}).get("extractor")
    req = p.get("capture_required_fields") or []

    if ext == "maps_url":
        if not cc.get("travel_date"):
            e.append("RULE-31 maps_url capture with no capture_context.travel_date — a Google "
                     "Maps directions URL carries no date; it is unusable without one")
        if not cc.get("collection_id") and res.get("outcome") == "resolved":
            e.append("RULE-32 maps_url resolved with no user-selected collection; the drive "
                     "plan attaches to a collection the human made in advance")

    for f in req:
        if not cc.get(f):
            if not (item.get("review") or {}).get("required"):
                e.append(f"RULE-33 capture_required_fields lists {f!r} but capture_context "
                         f"does not supply it and review.required is not set")

    # dates that came from the human must say so
    def walk_dt(node, path=""):
        if isinstance(node, dict):
            if "local" in node and "precision" in node:
                o = node.get("origin")
                if ext == "maps_url" and o not in ("capture_context", "collection_default"):
                    e.append(f"RULE-34 {path}: maps_url date origin={o!r}; must be "
                             f"capture_context — nothing in the URL supplies it")
                if o == "inferred":
                    e.append(f"RULE-34b {path}: date origin=inferred is never committable")
                if o == "capture_context" and not (cc.get("travel_date") or cc.get("end_date")):
                    e.append(f"RULE-34c {path}: origin=capture_context but none was supplied")
            for k, v in node.items():
                walk_dt(v, f"{path}/{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk_dt(v, f"{path}[{i}]")
    walk_dt(p, "payload")

    # user_selected bypasses the ladder by design — but must actually be selected
    if res.get("collection_action") == "user_selected":
        if not cc.get("collection_id"):
            e.append("RULE-35 collection_action=user_selected but capture_context supplies "
                     "no collection_id")
        elif res.get("collection_id") != cc.get("collection_id"):
            e.append(f"RULE-35b resolver assigned {res.get('collection_id')} but the human "
                     f"selected {cc.get('collection_id')} — never override an explicit choice")
    if cc.get("collection_id") and res.get("collection_action") in ("created",):
        e.append("RULE-35c human selected a collection but the resolver minted a new one")

    # guest-only exclusion
    if res.get("self_on_booking") is False and "airtrail.flight" in targets:
        e.append("RULE-10 self_on_booking=false but airtrail.flight targeted; "
                 "guest-only flights are not tracked in AirTrail")

    # inferred passenger must notify
    for i, tr in enumerate(p.get("travelers") or []):
        if tr.get("match_source") == "inferred_absent" and not (item.get("review") or {}).get("required"):
            e.append(f"RULE-11 travelers[{i}] role inferred from an absent passenger list "
                     f"but review.required is not set (policy: assume self AND notify)")

    # cancellation must reference something
    if p.get("booking_status") == "cancelled" and not (res.get("supersedes") or res.get("duplicate_of")):
        if res.get("outcome") == "resolved":
            e.append("RULE-12 cancellation resolved without supersedes/duplicate_of — "
                     "nothing was actually cancelled")

    # ---------------------------------------------------- update decisions
    rev = item.get("revision") or {}
    act = rev.get("action")
    al = (item.get("commit") or {}).get("adventurelog") or []
    at = (item.get("commit") or {}).get("airtrail") or {}

    if act == "update":
        has_prior_record = bool(at.get("record_id")) or any(r.get("record_id") for r in al)
        if not rev.get("prior_item_id"):
            e.append("RULE-13 action=update with no prior_item_id — this is a create")
        if item.get("commit") and not has_prior_record:
            e.append("RULE-13b action=update but no prior target record_id exists to update; "
                     "downgrade to create or the write silently duplicates")
        if not rev.get("changed_fields"):
            e.append("RULE-18 action=update with empty changed_fields — should be no_change")

    if act == "no_change" and rev.get("content_hash") != rev.get("prior_content_hash"):
        e.append("RULE-18b action=no_change but content_hash differs from prior — "
                 "a real change is about to be dropped")

    if rev.get("stale") and act in ("update", "supersede"):
        e.append(f"RULE-14 document_timestamp is older than the stored revision but "
                 f"action={act}; out-of-order delivery must yield conflict, not a write")

    locks = set(rev.get("field_locks") or [])
    clash = locks & set(rev.get("changed_fields") or [])
    if clash and act != "conflict":
        e.append(f"RULE-15 update would overwrite human-edited field(s) {sorted(clash)}; "
                 f"action must be conflict")

    if act == "conflict" and not (item.get("review") or {}).get("required"):
        e.append("RULE-15b action=conflict but review.required is not set")

    # ------------------------------------------ AdventureLog per-record graph
    seen = collections.Counter(r.get("key") for r in al)
    for k, n in seen.items():
        if n > 1:
            e.append(f"RULE-19 duplicate AdventureLog target key {k!r} ({n}x) — keys must be "
                     f"unique within an item")
    by_key = {r.get("key"): r for r in al}
    for r in al:
        if r.get("target") in ("transportation", "lodging", "note") and \
           not any(x.get("target") == "collection" for x in al) and not res.get("collection_id"):
            e.append(f"RULE-08b {r.get('key')}: collection-exclusive target with no collection "
                     f"record or collection_id")
        for dep in r.get("depends_on") or []:
            if dep not in by_key:
                e.append(f"RULE-20 {r.get('key')} depends_on {dep!r} which is not in the target set")
            elif by_key[dep].get("state") != "ok" and r.get("state") == "ok":
                e.append(f"RULE-20b {r.get('key')} is ok but its dependency {dep} is "
                         f"{by_key[dep].get('state')}")

    # status rollup must not be rosier than the records
    RANK = {"failed": 0, "needs_review": 1, "deferred": 2, "pending": 3,
            "running": 3, "skipped": 4, "ok": 5}
    rollup = (item.get("status") or {}).get("adventurelog") or {}
    worst = {}
    for r in al:
        _t = r.get("target")
        st = r.get("state")
        if _t and st and RANK.get(st, 9) < RANK.get(worst.get(_t, "ok"), 9):
            worst[_t] = st
    for _t, st in worst.items():
        if _t in rollup and RANK.get(rollup[_t], 9) > RANK.get(st, 9):
            e.append(f"RULE-21 status.adventurelog.{_t}={rollup[_t]} is rosier than its worst "
                     f"record ({st}) — partial failure would be invisible")

    # -------------------------------------------------- home suppression (1)
    def iter_places(node, path=""):
        if isinstance(node, dict):
            if "raw" in node and not {"local", "precision"} & set(node):
                yield path, node
            for k, v in node.items():
                yield from iter_places(v, f"{path}/{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                yield from iter_places(v, f"{path}[{i}]")

    def loc_key(pl):
        return f"location:{pl.get('iata') or pl.get('icao') or (pl.get('name') or pl.get('raw'))}"

    written_locs = {r.get("key") for r in al if r.get("target") == "location"
                    and r.get("state") in ("ok", "pending", "running")}
    home_places = []
    for path, pl in iter_places(p):
        home = bool(pl.get("is_home"))
        if home:
            home_places.append((path, pl))
            if not pl.get("home_ref"):
                e.append(f"RULE-24 {path}: is_home=true with no home_ref")
            if pl.get("suppressed") is not True:
                e.append(f"RULE-23 {path}: home place {pl.get('raw')!r} is not suppressed — "
                         f"home is never written to AdventureLog as a Location")
            k = loc_key(pl)
            if k in written_locs:
                e.append(f"RULE-23b {path}: AdventureLog location record {k!r} exists for a "
                         f"home place — suppress the write, not just the flag")
        if pl.get("suppressed") and not pl.get("suppression_reason"):
            e.append(f"RULE-24b {path}: suppressed with no suppression_reason")
        dk = pl.get("distance_from_home_km")
        if dk is not None and home and dk > 0 and pl.get("suppression_reason") == "home_radius":
            pass  # radius-based suppression, fine

    # -------------------------------------------------------- road trips (2)
    if t == "ground_transport":
        g = p.get("ground_transport") or {}
        gmode = g.get("mode")
        wps = g.get("waypoints") or []
        keys = [_dt_key((w.get("time") or {})) for w in wps]
        seq = [k for k in keys if k]
        if seq != sorted(seq):
            e.append("RULE-26 waypoints are not in chronological order")
        dep_t = _dt_key(((g.get("departure") or {}).get("time") or {}))
        arr_t = _dt_key(((g.get("arrival") or {}).get("time") or {}))
        for i, k in enumerate(seq):
            if dep_t and k < dep_t:
                e.append(f"RULE-26b waypoint[{i}] {k} precedes departure {dep_t}")
            if arr_t and k > arr_t:
                e.append(f"RULE-26c waypoint[{i}] {k} follows arrival {arr_t}")
        rt = g.get("route") or {}
        if gmode in ("car", "car_rental", "rideshare"):
            if rt.get("distance_km") is None and res.get("anchor_eligible"):
                e.append("RULE-27 road trip is anchor_eligible but has no route.distance_km — "
                         "home-radius test cannot be evaluated")
            if rt.get("is_estimate") is False:
                e.append("RULE-27b route.is_estimate=false; directions data is planning-time "
                         "only. Dawarich owns actual GPS traces")

    # entirely-within-home movement is not a trip
    if res.get("within_home_radius") is True:
        if res.get("outcome") not in ("ignored", "needs_review"):
            e.append(f"RULE-28 within_home_radius=true but outcome={res.get('outcome')}; "
                     f"movement that never left home is not a trip")
        if res.get("targets"):
            e.append("RULE-28b within_home_radius=true but write targets are set")

    if res.get("anchor_eligible") is False and res.get("collection_action") == "created":
        e.append("RULE-29 collection minted by an item marked anchor_eligible=false")
    if res.get("anchor_eligible") is True and res.get("within_home_radius") is True:
        e.append("RULE-29b anchor_eligible and within_home_radius are mutually exclusive")

    return e


def check_batch_rules(batch):
    """Batch-level integrity. A silently dropped leg errors nowhere else."""
    e = []
    items = batch.get("items") or []
    declared = batch.get("declared_segment_count")
    if declared is not None and declared != len(items):
        e.append(f"RULE-16 declared_segment_count={declared} but batch carries {len(items)} "
                 f"items — a segment was dropped without erroring")
    sha = (batch.get("artifact") or {}).get("sha256")
    for i, it in enumerate(items):
        if (it.get("artifact") or {}).get("sha256") != sha:
            e.append(f"RULE-16b items[{i}] artifact sha256 differs from the batch artifact")
        if it.get("batch_id") != batch.get("batch_id"):
            e.append(f"RULE-16c items[{i}].batch_id does not match the batch")
        if it.get("segment_count") not in (None, len(items)):
            e.append(f"RULE-16d items[{i}].segment_count={it.get('segment_count')} != {len(items)}")
    idx = [it.get("segment_index") for it in items if it.get("segment_index") is not None]
    if idx and sorted(idx) != list(range(len(items))):
        e.append(f"RULE-17 segment_index values {sorted(idx)} are not a contiguous 0..n-1 range")
    groups = {it.get("booking_group_id") for it in items if it.get("booking_group_id")}
    types = {it.get("item_type") for it in items}
    if len(groups) > 1 and types == {"flight"}:
        e.append(f"RULE-17b multiple booking_group_id values {groups} across flight segments "
                 f"from one document — legs of one PNR must share a group")
    keys = [it.get("canonical_key") for it in items if it.get("canonical_key")]
    dupes = [k for k, n in collections.Counter(keys).items() if n > 1]
    if dupes:
        e.append(f"RULE-17c duplicate canonical_key within one batch: {dupes} — "
                 f"the same leg was extracted twice")
    if batch.get("batch_status") == "complete":
        bad = [it.get("id") for it in items
               if any(v in ("failed", "needs_review", "deferred")
                      for k, v in (it.get("status") or {}).items() if isinstance(v, str))]
        if bad:
            e.append(f"RULE-22 batch_status=complete but {len(bad)} item(s) are not done — "
                     f"must be partial")
    return e


# ------------------------------------------------------------------- harness

def main():
    import pathlib
    here = pathlib.Path(__file__).parent
    # Schemas live in ../schemas/ in the repo; fall back to cwd for ad-hoc runs.
    sp = here.parent / "schemas" / "travel-item.schema.json"
    if not sp.exists():
        sp = here / "travel-item.schema.json"
    schema = json.load(open(sp))
    cases = json.load(open(here / "samples.json"))
    failures = 0
    for c in cases:
        name, expect = c["name"], c["expect"]
        inst = c["item"]
        is_batch = c.get("kind") == "batch"
        root = dict(schema, **{"$ref": "#/$defs/IngestBatch"}) if is_batch else schema
        se = validate_schema(inst, root)
        # Pipeline order: business rules never run on a schema-invalid object.
        if se:
            re_ = []
        elif is_batch:
            re_ = check_batch_rules(inst)
            for it in inst.get("items", []):
                re_ += check_rules(it)
        else:
            re_ = check_rules(inst)
        got_schema = "fail" if se else "pass"
        got_rules = "skipped" if se else ("fail" if re_ else "pass")
        exp_schema = expect.get("schema", "pass")
        exp_rules = expect.get("rules", "pass")
        ok = (got_schema == exp_schema) and (got_rules == exp_rules)
        mark = "PASS" if ok else "FAIL"
        if not ok:
            failures += 1
        print(f"[{mark}] {name}")
        print(f"        schema: expected {exp_schema}, got {got_schema}"
              f"   rules: expected {exp_rules}, got {got_rules}")
        if se:
            for x in se[:6]:
                print(f"          S| {x}")
        if re_:
            for x in re_[:6]:
                print(f"          R| {x}")
    print(f"\n{len(cases) - failures}/{len(cases)} cases behaved as expected")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
