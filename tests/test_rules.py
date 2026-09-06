"""Task 4 regression + toggle tests.

The cases in samples.json are the contract: every case must behave exactly
as it did under tests/validate.py. A case flipping from fail to pass is a
regression, not a fix.

Schema gate: prefers validation.py (real jsonschema). On pip-less dev hosts it
falls back — loudly — to the legacy subset validator so the rule-engine port is
still fully exercised; CI/container runs use the real library.

Runnable under pytest or directly: python3 tests/test_rules.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import rules
from rules import RULES, Violation, blocking, check_batch, check_item

try:
    from validation import validate_schema
    SCHEMA_IMPL = "jsonschema"
except ImportError:                               # pip-less dev host
    from validate import validate_schema
    SCHEMA_IMPL = "legacy-subset (jsonschema unavailable — install for CI runs)"

SCHEMA = json.load(open(ROOT / "schemas" / "travel-item.schema.json"))
CASES = json.load(open(ROOT / "tests" / "samples.json"))


def _run_case(case, enablement=None):
    inst = case["item"]
    is_batch = case.get("kind") == "batch"
    root = dict(SCHEMA, **{"$ref": "#/$defs/IngestBatch"}) if is_batch else SCHEMA
    se = validate_schema(inst, root)
    if se:                       # pipeline order: rules never see invalid objects
        rv = []
    elif is_batch:
        rv = check_batch(inst, enablement)
        for it in inst.get("items", []):
            rv += check_item(it, enablement)
    else:
        rv = check_item(inst, enablement)
    return se, rv


# ------------------------------------------------------------- regression

def test_all_sample_cases_behave_exactly_as_before():
    failures = []
    for case in CASES:
        se, rv = _run_case(case)
        got_schema = "fail" if se else "pass"
        got_rules = "skipped" if se else ("fail" if blocking(rv) else "pass")
        exp = case["expect"]
        if (got_schema, got_rules) != (exp.get("schema", "pass"), exp.get("rules", "pass")):
            failures.append(f"{case['name']}: expected "
                            f"{exp.get('schema', 'pass')}/{exp.get('rules', 'pass')}, "
                            f"got {got_schema}/{got_rules} "
                            f"(schema errors: {se[:2]}; rules: {[str(v) for v in rv[:2]]})")
    assert not failures, "\n".join(failures)


def test_legacy_message_shape_preserved():
    """str(Violation) must equal the old 'RULE-XX message' strings."""
    for case in CASES:
        _, rv = _run_case(case)
        for v in rv:
            assert str(v).startswith(f"{v.rule_id} ")


# ------------------------------------------------------------- registry

def test_registry_covers_every_emitted_rule():
    emitted = set()
    for case in CASES:
        _, rv = _run_case(case)
        emitted |= {v.rule_id for v in rv}
    assert emitted <= set(RULES), f"emitted rules missing from registry: {emitted - set(RULES)}"


def test_registry_shape():
    assert len(RULES) == 57
    assert len(rules.GROUPS) == 9
    assert all(r.scope in ("item", "batch") for r in RULES.values())
    # the TripIt scar tissue is marked critical (RULES.md: "Do not weaken")
    for rid in ("RULE-01", "RULE-02", "RULE-04", "RULE-07", "RULE-16"):
        assert RULES[rid].critical, rid


def test_docs_generation_mentions_every_rule():
    doc = rules.generate_docs()
    for rid in RULES:
        assert f"`{rid}`" in doc, rid
    assert "do not hand-edit" in doc


# ------------------------------------------------------------- toggles (D-25)

def _first_case_failing(rule_id):
    for case in CASES:
        se, rv = _run_case(case)
        if not se and any(v.rule_id == rule_id for v in rv):
            return case
    raise AssertionError(f"no sample case exercises {rule_id}")


def test_rule_off_suppresses_only_that_rule():
    case = _first_case_failing("RULE-30b")
    _, base = _run_case(case)
    _, toggled = _run_case(case, enablement={"RULE-30b": "off"})
    assert {v.rule_id for v in base} - {v.rule_id for v in toggled} == {"RULE-30b"}


def test_rule_audit_records_without_blocking():
    case = _first_case_failing("RULE-30b")
    _, rv = _run_case(case, enablement={"RULE-30b": "audit"})
    audited = [v for v in rv if v.rule_id == "RULE-30b"]
    assert audited and all(not v.blocking for v in audited)
    assert all(v.rule_id != "RULE-30b" for v in blocking(rv))


def test_case_flips_to_pass_when_its_only_rule_is_disabled():
    """A case failing on exactly one rule passes once that rule is off —
    the whole point of the toggle."""
    for case in CASES:
        se, rv = _run_case(case)
        ids = {v.rule_id for v in rv}
        if not se and len(ids) == 1:
            only = ids.pop()
            _, toggled = _run_case(case, enablement={only: "off"})
            assert not blocking(toggled), f"{case['name']} still fails with {only} off"
            return
    raise AssertionError("no single-rule failing case found")


def test_unknown_rule_or_mode_raises():
    item = {"extraction": {"payload": {"item_type": "note"}}}
    for bad in ({"RULE-99": "off"}, {"RULE-01": "disabled"}):
        try:
            check_item(item, enablement=bad)
        except ValueError:
            continue
        raise AssertionError(f"enablement {bad} should have raised")


def test_default_is_enforce_everything():
    for case in CASES:
        se, rv = _run_case(case)
        assert all(v.mode == "enforce" for v in rv)


# ------------------------------------------------------------- runner

if __name__ == "__main__":
    print(f"schema gate: {SCHEMA_IMPL}\n")
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok   {name}")
            except AssertionError as ex:
                failures += 1
                print(f"  FAIL {name}: {str(ex)[:400]}")
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)
