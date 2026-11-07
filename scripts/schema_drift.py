"""Check the Android app's saved /api/v1 schema against the server's
(YONDER-363, SPEC-4.3).

    python scripts/schema_drift.py                      # schema generated in-process
    python scripts/schema_drift.py --openapi spec.json  # or a saved schema
    python scripts/schema_drift.py --url https://trips.example.net --token ypat_…

contracts/android/openapi.json is a byte-for-byte copy of the Android app's
api/openapi.json (rundere/rundere-android); the app generates its
kotlinx.serialization models from it. Never edit it by hand. The comparison is
structural: per operation (path + method), the JSON request body and the first
2xx response body, with $refs resolved in their own spec, so a renamed schema
that sends the same data is clean.

ERROR — the app would break:
  * an operation the snapshot has is gone;
  * a response field the app has as required, non-null and without a default
    is gone, or now allows null;
  * a field changed type (integer → number counts, number → integer is fine),
    in a request or a response, including array items and nested objects;
  * a new required request field without a default (the app's writes get 422);
  * a request field the app sends is no longer accepted (silently dropped);
  * a new required query/header parameter, or one that became required.
WARN  — a nullable or defaulted response field is gone; a query parameter the
        app sends is gone; the 2xx status code changed.
INFO  — new operations, new optional fields, new enum values.

Multipart bodies are skipped, and free-form objects and mixed unions match
anything: the app reads them as raw JSON (the same rules as its gen_models.py).
Exit code 1 when there are errors. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SNAPSHOT = ROOT / "contracts" / "android" / "openapi.json"

METHODS = ("get", "put", "post", "patch", "delete")
JSON = "application/json"


class _Spec:
    """One side of the comparison: its schemas, for resolving its own $refs."""

    def __init__(self, spec: dict):
        self.spec = spec
        self.schemas = (spec.get("components") or {}).get("schemas") or {}

    def resolve(self, s: dict | None) -> tuple[dict, str | None]:
        """Follow $refs; returns the schema and the last ref name (for the
        recursion guard)."""
        name = None
        seen: set[str] = set()
        s = s or {}
        while "$ref" in s and s["$ref"] not in seen:
            seen.add(s["$ref"])
            name = s["$ref"].rsplit("/", 1)[-1]
            s = self.schemas.get(name) or {}
        return s, name

    def unwrap(self, s: dict | None) -> tuple[dict, bool, str | None]:
        """(schema, nullable, ref name) with anyOf/oneOf [X, null] unwrapped.
        A mixed union stays as it is (raw JSON to the app)."""
        s, name = self.resolve(s)
        alts = s.get("anyOf") or s.get("oneOf")
        if alts:
            non_null = [a for a in alts if self.resolve(a)[0].get("type") != "null"]
            if len(non_null) == 1 and len(non_null) < len(alts):
                inner, inner_name = self.resolve(non_null[0])
                return inner, True, inner_name or name
        return s, s.get("type") == "null", name


def _raw_json(s: dict) -> bool:
    """Free-form objects and mixed unions decode as JsonObject/JsonElement."""
    if s.get("anyOf") or s.get("oneOf"):
        return True
    if s.get("type") == "object" and not s.get("properties"):
        return True
    return not s.get("type") and not s.get("properties")    # {} = any


def _join(where: str, name: str) -> str:
    """"POST /x request: user" at the top level, "…: user.email" below it."""
    return f"{where}: {name}" if where.endswith(("request", "response")) else f"{where}.{name}"


def _type_change(old: str | None, new: str | None) -> bool:
    if not old or not new or old == new:
        return False
    return not (old == "number" and new == "integer")     # Kotlin Double reads 1


class _Checker:
    def __init__(self, snapshot: dict, live: dict):
        self.snap, self.live = _Spec(snapshot), _Spec(live)
        self.findings: list[tuple[str, str]] = []

    def add(self, level: str, msg: str) -> None:
        self.findings.append((level, msg))

    # ------------------------------------------------------------ bodies
    def compare(self, s_raw: dict | None, l_raw: dict | None, where: str,
                request: bool, guard: frozenset) -> None:
        s, _, s_name = self.snap.unwrap(s_raw)
        lv, _, l_name = self.live.unwrap(l_raw)
        if s_name and l_name:
            if (s_name, l_name) in guard:
                return                                    # recursive schema
            guard = guard | {(s_name, l_name)}
        if _raw_json(s) or _raw_json(lv):
            return
        st, lt = s.get("type"), lv.get("type")
        if _type_change(st, lt):
            self.add("ERROR", f"{where}: type changed from {st} to {lt}")
            return
        if st == "array":
            self.compare(s.get("items"), lv.get("items"), f"{where}[]", request, guard)
        elif st == "object" or s.get("properties"):
            self.compare_props(s, lv, where, request, guard)
        new_enum = set(lv.get("enum") or []) - set(s.get("enum") or [])
        if s.get("enum") and new_enum:
            self.add("INFO", f"{where}: new enum values {', '.join(sorted(map(str, new_enum)))}")

    def compare_props(self, s: dict, lv: dict, where: str, request: bool,
                      guard: frozenset) -> None:
        s_props, l_props = s.get("properties") or {}, lv.get("properties") or {}
        s_req, l_req = set(s.get("required") or []), set(lv.get("required") or [])
        for name, s_p in s_props.items():
            field = _join(where, name)
            if name not in l_props:
                if request:
                    self.add("ERROR", f"{field}: no longer accepted (it would be dropped)")
                elif self.app_non_null(s_p, name in s_req):
                    self.add("ERROR", f"{field}: required field is gone")
                else:
                    self.add("WARN", f"{field}: optional field is gone")
                continue
            if not request and self.app_non_null(s_p, name in s_req) \
                    and self.live.unwrap(l_props[name])[1]:
                self.add("ERROR", f"{field}: became nullable")
            self.compare(s_p, l_props[name], field, request, guard)
        for name, l_p in l_props.items():
            if name in s_props:
                continue
            field = _join(where, name)
            if request and name in l_req and "default" not in self.live.resolve(l_p)[0] \
                    and "default" not in l_p:
                self.add("ERROR", f"{field}: new required request field")
            else:
                self.add("INFO", f"{field}: new optional field, not adopted")

    def app_non_null(self, prop: dict, required: bool) -> bool:
        """The app's generated property is non-null with no default."""
        if not required or "default" in prop:
            return False
        inner, nullable, _ = self.snap.unwrap(prop)
        return not nullable and "default" not in inner

    # ------------------------------------------------------------ operations
    def run(self) -> list[tuple[str, str]]:
        s_paths, l_paths = self.snap.spec.get("paths") or {}, self.live.spec.get("paths") or {}
        for path, ops in s_paths.items():
            for method in METHODS:
                if method not in ops:
                    continue
                op = f"{method.upper()} {path}"
                live_op = (l_paths.get(path) or {}).get(method)
                if live_op is None:
                    self.add("ERROR", f"{op}: operation gone")
                    continue
                self.operation(op, ops[method], live_op)
        for path, ops in l_paths.items():
            for method in METHODS:
                if method in ops and method not in (s_paths.get(path) or {}):
                    self.add("INFO", f"{method.upper()} {path}: new operation")
        return self.findings

    def operation(self, op: str, s_op: dict, l_op: dict) -> None:
        s_body = (s_op.get("requestBody") or {}).get("content") or {}
        l_body = (l_op.get("requestBody") or {}).get("content") or {}
        if JSON in s_body and JSON in l_body:             # multipart is skipped
            self.compare(s_body[JSON].get("schema"), l_body[JSON].get("schema"),
                         f"{op} request", True, frozenset())
        s_ok = sorted(k for k in (s_op.get("responses") or {}) if k.startswith("2"))
        l_ok = sorted(k for k in (l_op.get("responses") or {}) if k.startswith("2"))
        if s_ok and l_ok:
            if s_ok[0] != l_ok[0]:
                self.add("WARN", f"{op}: success status changed from {s_ok[0]} to {l_ok[0]}")
            s_c = s_op["responses"][s_ok[0]].get("content") or {}
            l_c = l_op["responses"][l_ok[0]].get("content") or {}
            if JSON in s_c and JSON in l_c:
                self.compare(s_c[JSON].get("schema"), l_c[JSON].get("schema"),
                             f"{op} response", False, frozenset())
        self.parameters(op, s_op, l_op)

    def parameters(self, op: str, s_op: dict, l_op: dict) -> None:
        def params(o: dict) -> dict:
            return {(p.get("in"), p.get("name")): p for p in o.get("parameters") or []
                    if p.get("in") in ("query", "header")}
        s_par, l_par = params(s_op), params(l_op)
        for key, lp in l_par.items():
            if lp.get("required") and not (s_par.get(key) or {}).get("required"):
                what = "became required" if key in s_par else "new required parameter"
                self.add("ERROR", f"{op}: {key[0]} parameter {key[1]} {what}")
        for key in s_par.keys() - l_par.keys():
            if key[0] == "query":                         # FastAPI ignores unknown ones
                self.add("WARN", f"{op}: query parameter {key[1]} is gone")


def check(snapshot: dict, live: dict) -> list[tuple[str, str]]:
    """(level, message) findings; every message starts with "METHOD /path"."""
    return _Checker(snapshot, live).run()


def load_spec(args) -> dict:
    if args.openapi:
        return json.loads(Path(args.openapi).read_text())
    if args.url:
        req = urllib.request.Request(args.url.rstrip("/") + "/api/v1/openapi.json",
                                     headers={"Authorization": f"Bearer {args.token}"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())
    sys.path.insert(0, str(ROOT))
    from app.api_v1 import api_openapi_spec
    return api_openapi_spec()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--snapshot", default=str(DEFAULT_SNAPSHOT))
    ap.add_argument("--openapi", help="a saved /api/v1/openapi.json")
    ap.add_argument("--url", help="a live Run·dere base URL (needs --token)")
    ap.add_argument("--token", default="")
    ap.add_argument("--quiet", action="store_true", help="errors and warnings only")
    args = ap.parse_args(argv)
    findings = check(json.loads(Path(args.snapshot).read_text()), load_spec(args))
    for level, msg in findings:
        if not (args.quiet and level == "INFO"):
            print(f"{level:5} {msg}")
    errors = sum(1 for lvl, _ in findings if lvl == "ERROR")
    print(f"\n{errors} error(s), {sum(1 for lvl, _ in findings if lvl == 'WARN')} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
