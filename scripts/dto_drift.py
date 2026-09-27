"""Check the iOS app's Codable DTOs against the server's /api/v1 contract
(YONDER-58, SPEC-4.2 §3.1).

    python scripts/dto_drift.py                      # schema generated in-process
    python scripts/dto_drift.py --openapi spec.json  # or a saved schema
    python scripts/dto_drift.py --url https://yonder.example.net --token ypat_…

Parses `struct X: Codable` blocks in ios/Yonder/Models/DTOs.swift and compares
each property with the matching OpenAPI schema:

ERROR — the app would fail to decode, or its writes would be ignored:
  * a non-optional Swift field the server doesn't send, or sends as nullable;
  * a non-optional Swift field whose JSON type differs (String vs Int…);
  * a request DTO field the server doesn't accept (it would be silently dropped).
WARN  — a Swift field the server no longer has (optional, so harmless but dead),
        or a type mismatch on an optional field.
INFO  — server fields the app hasn't adopted yet (e.g. new optional data).

Response models always serialize every field (FastAPI), so "present" means
"declared"; "nullable" comes from the schema's anyOf/null. Exit code 1 when
there are errors. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SWIFT = ROOT / "ios" / "Yonder" / "Models" / "DTOs.swift"

# Swift struct → OpenAPI schema, where the names differ. Everything else is
# matched by the same name; structs listed in SKIP have no schema.
SCHEMA_FOR = {
    "UserPublic": "UserOut",
    "TokenResponse": "TokenOut",
    "NextTrip": "NextTripOut",
    "Dashboard": "DashboardOut",
    "ReviewList": "ReviewListOut",
}
SKIP = {"APIErrorBody",          # FastAPI's error envelope, not a declared model
        "ActivityTypeField",     # ActivityTypeOut.fields is list[dict] on the server
        "ActivityGeo", "Point", "Route",  # ActivityOut.geo is a dict on the server
        "CommitPreview", "Summary", "Plan", "Result"}  # commit dry-run: an undeclared dict
REQUEST_SUFFIXES = ("In", "Patch", "Body", "Create")
# Swift types that decode JSON null themselves (JSONValue has a `.null` case).
NULL_TOLERANT = {"JSONValue"}

_STRUCT = re.compile(r"^\s*struct\s+(\w+)\s*:\s*([^{]*)\{", re.M)
_PROP = re.compile(r"^\s*(?:let|var)\s+(\w+)\s*:\s*([^=\n/]+)", re.M)


@dataclass
class SwiftField:
    name: str
    type: str
    optional: bool


def parse_swift(text: str) -> dict[str, list[SwiftField]]:
    """struct name → fields, for Codable structs (brace-matched bodies)."""
    out: dict[str, list[SwiftField]] = {}
    for m in _STRUCT.finditer(text):
        if "Codable" not in m.group(2) and "Decodable" not in m.group(2):
            continue
        depth, i = 1, m.end()
        while i < len(text) and depth:
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            i += 1
        body = text[m.end():i - 1]
        body = re.sub(r"//[^\n]*", "", body)
        fields = []
        for p in _PROP.finditer(body):
            t = p.group(2).strip()
            fields.append(SwiftField(p.group(1), t.rstrip("?").strip(), t.endswith("?")))
        out[m.group(1)] = fields
    return out


def _resolve(schema: dict, schemas: dict) -> dict:
    while isinstance(schema, dict) and "$ref" in schema:
        schema = schemas.get(schema["$ref"].rsplit("/", 1)[-1], {})
    return schema or {}


def _variants(prop: dict, schemas: dict) -> tuple[set[str], bool]:
    """(JSON types, nullable) of a property schema."""
    prop = _resolve(prop, schemas)
    if "anyOf" in prop or "oneOf" in prop:
        types, nullable = set(), False
        for sub in prop.get("anyOf") or prop.get("oneOf"):
            t, n = _variants(sub, schemas)
            types |= t
            nullable |= n
        return types, nullable
    t = prop.get("type")
    if t is None:
        # A schema with no type (Pydantic `Any`) or a $ref'd object.
        return ({"object"} if "properties" in prop else {"any"}), "properties" not in prop
    if t == "null":
        return set(), True
    return {t}, False


_SWIFT_JSON = {"String": {"string"}, "Int": {"integer"}, "Double": {"number", "integer"},
               "Float": {"number", "integer"}, "Bool": {"boolean"}}


def _swift_json_types(t: str) -> set[str] | None:
    """The JSON types a Swift type decodes from, or None = don't check
    (custom structs, JSONValue)."""
    t = t.strip()
    if t in _SWIFT_JSON:
        return _SWIFT_JSON[t]
    if t.startswith("[") and ":" in t:
        return {"object"}
    if t.startswith("["):
        return {"array"}
    return None


def check(structs: dict[str, list[SwiftField]], spec: dict) -> list[tuple[str, str]]:
    """→ [(level, message)] with level ERROR | WARN | INFO."""
    schemas = spec.get("components", {}).get("schemas", {})
    findings: list[tuple[str, str]] = []
    for struct, fields in sorted(structs.items()):
        if struct in SKIP:
            continue
        name = SCHEMA_FOR.get(struct, struct)
        schema = schemas.get(f"{name}-Output") or schemas.get(name)
        if schema is None:
            findings.append(("ERROR", f"{struct}: no schema {name!r} in the API contract"))
            continue
        props = schema.get("properties", {})
        is_request = struct.endswith(REQUEST_SUFFIXES)
        for f in fields:
            where = f"{struct}.{f.name}"
            if f.name not in props:
                if is_request:
                    findings.append(("ERROR", f"{where}: the server doesn't accept this field "
                                              "(it would be dropped)"))
                elif f.optional:
                    findings.append(("WARN", f"{where}: not in {name} any more (optional, dead)"))
                else:
                    findings.append(("ERROR", f"{where}: required by the app but {name} "
                                              "doesn't send it"))
                continue
            if is_request:
                continue
            types, nullable = _variants(props[f.name], schemas)
            if nullable and not f.optional and f.type not in NULL_TOLERANT:
                findings.append(("ERROR", f"{where}: the server may send null; make it "
                                          f"`{f.type}?`"))
            want = _swift_json_types(f.type)
            if want is not None and types and "any" not in types and not (types & want):
                level = "WARN" if f.optional else "ERROR"
                findings.append((level, f"{where}: Swift {f.type} vs JSON {sorted(types)}"))
        if not is_request:
            known = {f.name for f in fields}
            new = sorted(set(props) - known)
            if new:
                findings.append(("INFO", f"{struct}: not adopted yet: {', '.join(new)}"))
    return findings


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
    ap.add_argument("--swift", default=str(DEFAULT_SWIFT))
    ap.add_argument("--openapi", help="a saved /api/v1/openapi.json")
    ap.add_argument("--url", help="a live Yonder base URL (needs --token)")
    ap.add_argument("--token", default="")
    ap.add_argument("--quiet", action="store_true", help="errors and warnings only")
    args = ap.parse_args(argv)
    findings = check(parse_swift(Path(args.swift).read_text()), load_spec(args))
    for level, msg in findings:
        if not (args.quiet and level == "INFO"):
            print(f"{level:5} {msg}")
    errors = sum(1 for lvl, _ in findings if lvl == "ERROR")
    print(f"\n{errors} error(s), {sum(1 for lvl, _ in findings if lvl == 'WARN')} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
