"""Schema validation gate — the real `jsonschema` library (Task 4).

Replaces validate.py's hand-rolled subset validator, which existed only because
the original drafting sandbox had no network. Return shape is kept identical
(list of "path: message" strings, empty = valid) so callers and the regression
harness are drop-in.

Hard dependency by design: if `jsonschema` is missing this module fails to
import. A silent fallback to the subset validator could mask a broken prod
image. The test harness (tests/test_rules.py) does its own explicit,
loudly-labelled fallback for pip-less dev hosts.
"""
from __future__ import annotations

import jsonschema

_FORMAT_CHECKER = jsonschema.FormatChecker()
# The subset validator enforced uuid/date-time/date formats; jsonschema treats
# format as annotation-only unless a checker is supplied. Keep enforcement.


def validate_schema(instance, schema) -> list[str]:
    validator = jsonschema.Draft202012Validator(schema, format_checker=_FORMAT_CHECKER)
    errors = []
    for err in sorted(validator.iter_errors(instance),
                      key=lambda e: list(e.absolute_path)):
        path = "".join(f"[{p}]" if isinstance(p, int) else f".{p}"
                       for p in err.absolute_path)
        errors.append(f"{path or '<root>'}: {err.message}")
    return errors
