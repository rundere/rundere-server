"""Traveler identity matching (D-22 / RULE-10 / RULE-11). Pure functions.

AirTrail models a seat as either a registered USER (the account holder) or a
guestName string. Only flights the account holder was actually on may be
written there — a flight booked for family alone is not the owner's flight
history — and that exclusion is enforced HERE, at resolution, not at commit.

Airlines print names in shapes no two agree on: "OKAFOR/DANIEL MR",
"DANIEL OKAFOR", "Okafor, Daniel", "R OKAFOR". Matching therefore compares a
normalised token SET, so word order, punctuation, honorifics and middle names
do not decide whether someone is you.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# Titles airlines append or prepend; never part of a person's name.
_HONORIFICS = {"mr", "mrs", "ms", "miss", "dr", "prof", "sir", "madam",
               "mstr", "master", "rev"}


@dataclass(frozen=True)
class Identity:
    """One person the deployment knows about."""
    id: str
    display_name: str
    kind: str                       # self | guest
    aliases: tuple[str, ...] = ()
    airtrail_user_id: str | None = None

    def name_forms(self) -> list[str]:
        return [self.display_name, *self.aliases]


def normalize_name(raw: str) -> str:
    """Casefold, strip diacritics and punctuation, drop honorifics."""
    s = unicodedata.normalize("NFKD", raw or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^0-9a-zA-Z]+", " ", s.casefold())
    return " ".join(t for t in s.split() if t not in _HONORIFICS)


def name_tokens(raw: str) -> frozenset[str]:
    """Token set, ignoring single-letter initials — "R OKAFOR" should not match
    every other person whose name contains an R."""
    return frozenset(t for t in normalize_name(raw).split() if len(t) > 1)


def match_identity(raw_name: str,
                   identities: list[Identity]) -> tuple[Identity | None, str, float]:
    """(identity, match_source, confidence).

    Exact on the full normalised string first; then token-set containment,
    which is what makes "OKAFOR/DANIEL MR" and "Daniel Okafor" the same person
    without letting a shared surname alone match a different family member.
    """
    want_norm = normalize_name(raw_name)
    want_tokens = name_tokens(raw_name)
    if not want_norm:
        return None, "", 0.0

    for ident in identities:
        for form in ident.name_forms():
            if normalize_name(form) == want_norm:
                return ident, "alias_exact", 1.0

    best: tuple[Identity | None, float] = (None, 0.0)
    for ident in identities:
        for form in ident.name_forms():
            have = name_tokens(form)
            if not have or not want_tokens:
                continue
            overlap = len(have & want_tokens)
            # Require every token of the shorter name to appear in the longer
            # one: "Daniel Okafor" vs "Daniel Kumar Okafor" matches; "Daniel Okafor"
            # vs "Amara Okafor" does not, because "amara" is not "daniel".
            if overlap and overlap == min(len(have), len(want_tokens)) and overlap >= 2:
                score = overlap / max(len(have), len(want_tokens))
                if score > best[1]:
                    best = (ident, score)
    if best[0] is not None:
        return best[0], "alias_fuzzy", round(best[1], 2)
    return None, "", 0.0


@dataclass
class TravelerResolution:
    travelers: list[dict] = field(default_factory=list)
    self_on_booking: bool = False
    inferred: bool = False          # no passenger list at all -> assumed self


def resolve_travelers(payload: dict,
                      identities: list[Identity]) -> TravelerResolution:
    """Assign role/match_source/match_confidence to each traveler.

    An empty passenger list means the document never named anyone. Policy
    (D-22) is assume self AND notify — RULE-11 then enforces that
    review.required was actually set, so the policy is checked rather than
    trusted.
    """
    raw = payload.get("travelers") or []
    selves = [i for i in identities if i.kind == "self"]

    if not raw:
        return TravelerResolution(
            travelers=[{"raw_name": (selves[0].display_name if selves else "self"),
                        "role": "self", "match_source": "inferred_absent",
                        "match_confidence": 0.0}],
            self_on_booking=True, inferred=True)

    out: list[dict] = []
    found_self = False
    for t in raw:
        entry = dict(t)
        ident, source, confidence = match_identity(t.get("raw_name", ""), identities)
        if ident is None:
            entry.update({"role": "unknown", "match_source": None,
                          "match_confidence": 0.0})
        else:
            entry.update({"role": ident.kind, "match_source": source,
                          "match_confidence": confidence})
            if ident.kind == "self":
                found_self = True
        out.append(entry)
    return TravelerResolution(travelers=out, self_on_booking=found_self)
