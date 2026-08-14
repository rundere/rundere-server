"""Plug / voltage resolver — the purest static tier (SPEC-3-discovery.md §3, D-64).

No search, no LLM, no network: plug type and mains voltage are national standards
read from a curated JSON file. Also emits the two things a traveller actually
needs to know — "do my plugs fit?" and "is the voltage different?" — judged
against the deployment's home standard (North America: Type A/B, 120 V).
"""
from __future__ import annotations

import json
from pathlib import Path

from discover.places import DerivedPlace
from discover.resolvers.base import Finding, ResolverContext

_DATA = Path(__file__).resolve().parent.parent / "data" / "plugs.json"

# Home electrical standard the advice is relative to. North America, matching the
# deployment's home profile; a future config lever if a deployment differs.
HOME_PLUG_TYPES = {"A", "B"}
HOME_VOLTAGE_V = 120

_PLUG_NAMES = {
    "A": "Type A (North-American 2-pin)", "B": "Type B (North-American 3-pin)",
    "C": "Type C (European 2-pin)", "D": "Type D (Indian)",
    "E": "Type E (French)", "F": "Type F (Schuko)", "G": "Type G (UK 3-pin)",
    "H": "Type H (Israeli)", "I": "Type I (Australian)", "J": "Type J (Swiss)",
    "K": "Type K (Danish)", "L": "Type L (Italian)", "M": "Type M (South-African)",
    "N": "Type N (Brazilian)", "O": "Type O (Thai)",
}

_SOURCE = [{"title": "IEC World Plugs (static reference)"}]


def _load() -> dict:
    with open(_DATA, encoding="utf-8") as fh:
        return json.load(fh)


_TABLE = _load()


def _voltage_value(v: str) -> int | None:
    """The lowest volt number in a "230V" / "127V/220V" string, for the
    dual-voltage comparison."""
    nums = []
    cur = ""
    for ch in v:
        if ch.isdigit():
            cur += ch
        elif cur:
            nums.append(int(cur)); cur = ""
    if cur:
        nums.append(int(cur))
    return min(nums) if nums else None


class PlugsResolver:
    topic = "plugs"
    scope = "country"

    def resolve(self, place: DerivedPlace, ctx: ResolverContext) -> Finding | None:
        country = place.country
        if not country:
            return None
        entry = _TABLE.get(country)
        if entry is None:
            return Finding(
                topic=self.topic, scope=self.scope, place_key=country,
                resolver="plugs.static", confidence="flagged", sources=_SOURCE,
                body_md=(f"Plug and voltage data for **{country}** isn't in "
                         f"Yonder's local dataset yet. Add it to "
                         f"`discover/data/plugs.json` (SPEC-3 D-64)."))

        types = entry.get("types", [])
        voltage = entry.get("voltage", "—")
        freq = entry.get("frequency", "—")
        type_list = ", ".join(_PLUG_NAMES.get(t, f"Type {t}") for t in types)

        lines = [f"**{place.label}** uses **{type_list}** at **{voltage} / {freq}**."]

        if types and not (set(types) & HOME_PLUG_TYPES):
            lines.append("⚠️ Bring a plug adapter — your Type A/B plugs won't fit.")
        elif set(types) & HOME_PLUG_TYPES:
            lines.append("✅ Your Type A/B plugs fit (at least some outlets).")

        dest_v = _voltage_value(voltage)
        if dest_v is not None and dest_v >= 200 and HOME_VOLTAGE_V < 130:
            lines.append("⚠️ Voltage is higher than home (120 V). Check devices are "
                         "dual-voltage (100–240 V), or bring a converter for "
                         "single-voltage items (hair dryers, some chargers).")

        return Finding(
            topic=self.topic, scope=self.scope, place_key=country,
            resolver="plugs.static", confidence="high", sources=_SOURCE,
            data={"types": types, "voltage": voltage, "frequency": freq},
            body_md="\n\n".join(lines))
