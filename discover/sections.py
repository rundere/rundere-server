"""Discover sections (Run·dere 4.5, Discovery 2.0 — YONDER-166).

Storage keeps one finding per topic per place; the page groups topics into
sections and shows ONE section at a time (a left rail on desktop, chips on
mobile). Topic keys never change, so agents, the API and old data are
unaffected — this is purely how the tab is organised.
"""
from __future__ import annotations

# (key, label, topics in display order, what the section is for)
SECTIONS: list[tuple[str, str, tuple[str, ...], str]] = [
    ("weather", "Weather", ("weather",),
     "Expected conditions in each city for your dates."),
    ("food", "Food & Drink", ("food",),
     "Where to eat and drink in each city, and what to order."),
    ("events", "Events & Activities", ("events",),
     "What's on while you're there, and things to do any day."),
    ("language", "Language", ("language", "language_regional", "language_local"),
     "The national language, regional variants, and how locals actually talk."),
    ("safety", "Safety & Transport", ("safety",),
     "Advisories, scams to know, and getting around."),
    ("research", "Research", ("research",),
     "Research AI agents saved for this trip."),
    ("practical", "Practical", ("plugs", "currency", "visa", "packing"),
     "Plugs & voltage, currency, and entry requirements — check visas with the "
     "embassy or government site; nothing here researches them for you."),
]
SECTION_KEYS = [s[0] for s in SECTIONS]
DEFAULT_SECTION = "weather"

SUBTOPIC_LABELS = {
    "language": "National language", "language_regional": "Regional language",
    "language_local": "Local phrases & register",
    "plugs": "Plugs & voltage", "currency": "Currency", "visa": "Visa & entry",
    "packing": "Packing notes",
}


def section_of(topic: str) -> str:
    for key, _label, topics, _about in SECTIONS:
        if topic in topics:
            return key
    return "research"


def build(findings_by_topic: dict[str, list]) -> list[dict]:
    """Sections for the rail + panels. `findings_by_topic` maps topic → list of
    finding views (already sorted). Every section is returned, empty or not,
    so the rail never shifts; each carries its count and how many are new from
    an agent or still drafts."""
    out = []
    for key, label, topics, about in SECTIONS:
        groups = []
        for t in topics:
            fs = findings_by_topic.get(t) or []
            if fs:
                groups.append({"topic": t, "label": SUBTOPIC_LABELS.get(t, label),
                               "findings": fs})
        allf = [f for g in groups for f in g["findings"]]
        out.append({
            "key": key, "label": label, "about": about, "groups": groups,
            "count": len(allf),
            "from_agent": sum(1 for f in allf if f.get("external") and f.get("status") == "draft"),
            "drafts": sum(1 for f in allf if f.get("status") == "draft"),
        })
    return out
