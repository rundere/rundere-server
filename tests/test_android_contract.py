"""YONDER-363 — the Android app's /api/v1 contract (contracts/android/).

contracts/android/openapi.json is a byte-for-byte copy of the Android app's
saved schema (api/openapi.json in rundere/rundere-android, private). The app
generates its kotlinx.serialization models from it, so scripts/schema_drift.py
compares that snapshot structurally with the live schema and fails CI when a
server change would break the app's decoding or drop its writes.

The fixture pairs below use invented schema and field names, one per rule in
the table in contracts/android/README.md.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import schema_drift  # noqa: E402

JSON = "application/json"


# ---------------------------------------------------------------- fixture builders
def spec(paths: dict, schemas: dict | None = None) -> dict:
    return {"openapi": "3.1.0", "info": {"title": "t", "version": "0"},
            "paths": paths, "components": {"schemas": schemas or {}}}


def op(resp: dict | None = None, req: dict | None = None,
       params: list | None = None, status: str = "200",
       req_type: str = JSON) -> dict:
    o: dict = {"responses": {status: {"description": "ok"}}}
    if resp is not None:
        o["responses"][status]["content"] = {JSON: {"schema": resp}}
    if req is not None:
        o["requestBody"] = {"required": True, "content": {req_type: {"schema": req}}}
    if params is not None:
        o["parameters"] = params
    return o


def obj(props: dict, required: list | None = None) -> dict:
    o = {"type": "object", "properties": props}
    if required:
        o["required"] = required
    return o


def nullable(t: dict) -> dict:
    return {"anyOf": [t, {"type": "null"}]}


def ref(name: str) -> dict:
    return {"$ref": f"#/components/schemas/{name}"}


def param(name: str, required: bool = False, where: str = "query") -> dict:
    return {"name": name, "in": where, "required": required,
            "schema": {"type": "string"}}


S = {"type": "string"}
I = {"type": "integer"}
N = {"type": "number"}
B = {"type": "boolean"}


def levels(snapshot: dict, live: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {"ERROR": [], "WARN": [], "INFO": []}
    for level, msg in schema_drift.check(snapshot, live):
        out[level].append(msg)
    return out


def has(msgs: list[str], *needles: str) -> bool:
    return any(all(n in m for n in needles) for m in msgs)


def one_get(resp: dict, schemas: dict | None = None, **kw) -> dict:
    return spec({"/api/v1/widgets": {"get": op(resp=resp, **kw)}}, schemas)


def one_post(req: dict, resp: dict | None = None, schemas: dict | None = None,
             **kw) -> dict:
    return spec({"/api/v1/widgets": {"post": op(resp=resp or obj({}), req=req, **kw)}},
                schemas)


# ---------------------------------------------------------------- the real snapshot
def test_real_snapshot_has_no_errors_against_this_server():
    snapshot = json.loads(schema_drift.DEFAULT_SNAPSHOT.read_text())
    from app.api_v1 import api_openapi_spec
    errors = [m for lvl, m in schema_drift.check(snapshot, api_openapi_spec())
              if lvl == "ERROR"]
    assert errors == [], "\n".join(errors)


def test_identical_specs_are_clean():
    s = one_get(obj({"name": S, "count": I}, ["name", "count"]))
    assert levels(s, s) == {"ERROR": [], "WARN": [], "INFO": []}


# ---------------------------------------------------------------- ERROR rules
def test_error_operation_gone():
    snap = spec({"/api/v1/widgets": {"get": op(resp=obj({})),
                                     "delete": op()}})
    live = spec({"/api/v1/widgets": {"get": op(resp=obj({}))}})
    r = levels(snap, live)
    assert has(r["ERROR"], "DELETE", "/api/v1/widgets")


def test_error_required_response_field_gone():
    snap = one_get(obj({"name": S, "colour": S}, ["name", "colour"]))
    live = one_get(obj({"name": S}, ["name"]))
    assert has(levels(snap, live)["ERROR"], "colour")


def test_error_response_field_became_nullable():
    snap = one_get(obj({"colour": S}, ["colour"]))
    live = one_get(obj({"colour": nullable(S)}, ["colour"]))
    assert has(levels(snap, live)["ERROR"], "colour")


def test_error_type_swapped_in_response():
    snap = one_get(obj({"size": S}, ["size"]))
    live = one_get(obj({"size": B}, ["size"]))
    assert has(levels(snap, live)["ERROR"], "size")


def test_error_type_swapped_in_request():
    snap = one_post(obj({"size": S}))
    live = one_post(obj({"size": I}))
    assert has(levels(snap, live)["ERROR"], "size")


def test_number_to_integer_is_fine():
    snap = one_get(obj({"weight": N}, ["weight"]))
    live = one_get(obj({"weight": I}, ["weight"]))
    assert levels(snap, live)["ERROR"] == []


def test_error_integer_to_number():
    snap = one_get(obj({"weight": I}, ["weight"]))
    live = one_get(obj({"weight": N}, ["weight"]))
    assert has(levels(snap, live)["ERROR"], "weight")


def test_error_array_items_changed_type():
    snap = one_get(obj({"tags": {"type": "array", "items": S}}, ["tags"]))
    live = one_get(obj({"tags": {"type": "array", "items": I}}, ["tags"]))
    assert has(levels(snap, live)["ERROR"], "tags")


def test_error_nested_ref_shape_incompatible():
    snap = one_get(obj({"part": ref("Part")}, ["part"]),
                   {"Part": obj({"code": S}, ["code"])})
    live = one_get(obj({"part": ref("Part")}, ["part"]),
                   {"Part": obj({"code": I}, ["code"])})
    assert has(levels(snap, live)["ERROR"], "code")


def test_error_new_required_request_field():
    snap = one_post(obj({"name": S}, ["name"]))
    live = one_post(obj({"name": S, "colour": S}, ["name", "colour"]))
    assert has(levels(snap, live)["ERROR"], "colour")


def test_new_required_request_field_with_default_is_not_an_error():
    snap = one_post(obj({"name": S}, ["name"]))
    live = one_post(obj({"name": S, "colour": {**S, "default": "red"}},
                        ["name", "colour"]))
    assert levels(snap, live)["ERROR"] == []


def test_error_request_field_no_longer_accepted():
    snap = one_post(obj({"name": S, "colour": S}))
    live = one_post(obj({"name": S}))
    assert has(levels(snap, live)["ERROR"], "colour")


def test_error_new_required_parameter():
    snap = one_get(obj({}), params=[param("page")])
    live = one_get(obj({}), params=[param("page"), param("shelf", required=True)])
    assert has(levels(snap, live)["ERROR"], "shelf")


def test_error_parameter_became_required():
    snap = one_get(obj({}), params=[param("page")])
    live = one_get(obj({}), params=[param("page", required=True)])
    assert has(levels(snap, live)["ERROR"], "page")


def test_error_new_required_header():
    snap = one_get(obj({}))
    live = one_get(obj({}), params=[param("X-Widget-Key", required=True, where="header")])
    assert has(levels(snap, live)["ERROR"], "X-Widget-Key")


# ---------------------------------------------------------------- WARN rules
def test_warn_nullable_response_field_gone():
    snap = one_get(obj({"name": S, "note": nullable(S)}, ["name"]))
    live = one_get(obj({"name": S}, ["name"]))
    r = levels(snap, live)
    assert r["ERROR"] == [] and has(r["WARN"], "note")


def test_warn_defaulted_response_field_gone():
    snap = one_get(obj({"name": S, "count": {**I, "default": 0}}, ["name"]))
    live = one_get(obj({"name": S}, ["name"]))
    r = levels(snap, live)
    assert r["ERROR"] == [] and has(r["WARN"], "count")


def test_warn_query_parameter_gone():
    snap = one_get(obj({}), params=[param("page")])
    live = one_get(obj({}))
    r = levels(snap, live)
    assert r["ERROR"] == [] and has(r["WARN"], "page")


def test_warn_success_status_changed():
    snap = spec({"/api/v1/widgets": {"post": op(resp=obj({}), req=obj({}), status="200")}})
    live = spec({"/api/v1/widgets": {"post": op(resp=obj({}), req=obj({}), status="201")}})
    r = levels(snap, live)
    assert r["ERROR"] == [] and has(r["WARN"], "201")


# ---------------------------------------------------------------- INFO rules
def test_info_new_operation():
    snap = spec({"/api/v1/widgets": {"get": op(resp=obj({}))}})
    live = spec({"/api/v1/widgets": {"get": op(resp=obj({}))},
                 "/api/v1/gadgets": {"get": op(resp=obj({}))}})
    r = levels(snap, live)
    assert r["ERROR"] == [] and r["WARN"] == [] and has(r["INFO"], "/api/v1/gadgets")


def test_info_new_optional_response_field():
    snap = one_get(obj({"name": S}, ["name"]))
    live = one_get(obj({"name": S, "shine": nullable(S)}, ["name"]))
    r = levels(snap, live)
    assert r["ERROR"] == [] and r["WARN"] == [] and has(r["INFO"], "shine")


def test_info_new_optional_request_field():
    snap = one_post(obj({"name": S}, ["name"]))
    live = one_post(obj({"name": S, "shine": S}, ["name"]))
    r = levels(snap, live)
    assert r["ERROR"] == [] and r["WARN"] == [] and has(r["INFO"], "shine")


def test_info_new_enum_value():
    snap = one_get(obj({"kind": {"type": "string", "enum": ["round", "square"]}}, ["kind"]))
    live = one_get(obj({"kind": {"type": "string", "enum": ["round", "square", "oval"]}},
                       ["kind"]))
    r = levels(snap, live)
    assert r["ERROR"] == [] and r["WARN"] == [] and has(r["INFO"], "oval")


# ---------------------------------------------------------------- structure, not names
def test_renamed_schema_with_the_same_shape_is_not_an_error():
    snap = one_get(ref("WidgetOut"), {"WidgetOut": obj({"name": S}, ["name"])})
    live = one_get(ref("WidgetView"), {"WidgetView": obj({"name": S}, ["name"])})
    r = levels(snap, live)
    assert r["ERROR"] == [] and r["WARN"] == []


def test_inline_and_ref_compare_the_same():
    snap = one_get(obj({"name": S}, ["name"]))
    live = one_get(ref("WidgetOut"), {"WidgetOut": obj({"name": S}, ["name"])})
    assert levels(snap, live)["ERROR"] == []


def test_recursive_refs_terminate():
    node = obj({"name": S, "children": {"type": "array", "items": ref("Node")}}, ["name"])
    s = one_get(ref("Node"), {"Node": node})
    assert levels(s, s)["ERROR"] == []
    live_node = obj({"name": I, "children": {"type": "array", "items": ref("Node")}},
                    ["name"])
    assert has(levels(s, one_get(ref("Node"), {"Node": live_node}))["ERROR"], "name")


def test_multipart_bodies_are_skipped():
    snap = one_post(obj({"file": S, "note": S}), req_type="multipart/form-data")
    live = one_post(obj({"file": S}), req_type="multipart/form-data")
    assert levels(snap, live)["ERROR"] == []


def test_free_form_object_is_compatible_with_anything():
    snap = one_get(obj({"extra": {"type": "object", "additionalProperties": True}},
                       ["extra"]))
    live = one_get(obj({"extra": obj({"a": S}, ["a"])}, ["extra"]))
    assert levels(snap, live)["ERROR"] == []


def test_mixed_union_is_compatible_with_anything():
    snap = one_get(obj({"value": {"anyOf": [S, I]}}, ["value"]))
    live = one_get(obj({"value": B}, ["value"]))
    assert levels(snap, live)["ERROR"] == []


def test_nullable_ref_still_compares_its_shape():
    snap = one_get(obj({"part": nullable(ref("Part"))}),
                   {"Part": obj({"code": S}, ["code"])})
    live = one_get(obj({"part": nullable(ref("Part"))}),
                   {"Part": obj({"code": I}, ["code"])})
    assert has(levels(snap, live)["ERROR"], "code")


# ---------------------------------------------------------------- CLI
def test_cli_exit_codes(tmp_path):
    good = one_get(obj({"name": S}, ["name"]))
    bad = one_get(obj({"other": S}, ["other"]))
    snap = tmp_path / "snap.json"
    live_ok = tmp_path / "ok.json"
    live_bad = tmp_path / "bad.json"
    snap.write_text(json.dumps(good))
    live_ok.write_text(json.dumps(good))
    live_bad.write_text(json.dumps(bad))
    assert schema_drift.main(["--snapshot", str(snap), "--openapi", str(live_ok),
                              "--quiet"]) == 0
    assert schema_drift.main(["--snapshot", str(snap), "--openapi", str(live_bad)]) == 1


def test_default_snapshot_is_the_android_copy():
    assert schema_drift.DEFAULT_SNAPSHOT == ROOT / "contracts" / "android" / "openapi.json"
