"""The cuisine vocabulary for Discovery profiles (YONDER-328).

A closed list of OpenStreetMap `cuisine=*` values (the same open-data world as
Photon), grouped for the profile's modal, in discover/data/cuisines.json. A
person marks each one Like, Neutral (the default) or Don't like:

- Don't like subtracts: the food resolver is told never to recommend it (D-87).
- Like adds targeted food searches, as drink interests do. The person entered
  it, so it isn't an inference (D-93).

`match()` turns free text (the pre-5.0 "cuisines to avoid" box) into a key.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_PATH = Path(__file__).parent / "data" / "cuisines.json"


@lru_cache(maxsize=1)
def groups() -> list[dict]:
    """[{key, label, cuisines: [{key, label, aliases?}]}], in the modal's order."""
    return json.loads(_PATH.read_text(encoding="utf-8"))["groups"]


@lru_cache(maxsize=1)
def labels() -> dict[str, str]:
    return {c["key"]: c["label"] for g in groups() for c in g["cuisines"]}


def label(key: str) -> str:
    """A key's label; an unknown value (hand-written data) is shown as itself."""
    return labels().get(key, key)


def _norm(text: str) -> str:
    t = " ".join(text.lower().replace("_", " ").split())
    for tail in (" food", " cuisine", " restaurants", " restaurant"):
        if t.endswith(tail) and len(t) > len(tail):
            t = t[: -len(tail)]
    return t


@lru_cache(maxsize=1)
def _lookup() -> dict[str, str]:
    out: dict[str, str] = {}
    for g in groups():
        for c in g["cuisines"]:
            for name in (c["key"], c["label"], *c.get("aliases", [])):
                out[_norm(name)] = c["key"]
    return out


def match(text: str) -> str | None:
    """The key free text names, or None. "Sushi", "fast food", "BBQ" and
    "Mexican food" all match; "spicy things" doesn't."""
    return _lookup().get(_norm(text or ""))


def keys_only(values) -> list[str]:
    """`values` filtered to known keys, in vocabulary order, without repeats."""
    wanted = set(values or [])
    return [k for k in labels() if k in wanted]
