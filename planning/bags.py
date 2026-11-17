"""Bags for a trip, and what fits (SPEC-5 §6.10, D-232).

Pure, like the engine: lines and bags in; fill, assignment and a suggestion
out. Everything here is an estimate in litres. A bag's capacity is whatever the
person typed ("40L", "35–40 litres"); anything not in litres is unknown and is
reported as such, never guessed.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# No bag packs solid: corners, the frame and the zip take their share.
USABLE = 0.85

_UNITS = ("liters", "litres", "liter", "litre", "ltr", "lt", "l")


def _numbers(text: str) -> list[tuple[float, str]]:
    """Each number in `text` with the word after it, in one pass. Hand-rolled
    rather than a regex: a regex scan restarts at every digit, which is
    polynomial on a long run of them (CodeQL py/polynomial-redos)."""
    out, i, n = [], 0, len(text)
    while i < n:
        if not text[i].isdigit():
            i += 1
            continue
        j = i
        while j < n and text[j].isdigit():
            j += 1
        if j + 1 < n and text[j] in ".," and text[j + 1].isdigit():
            j += 1
            while j < n and text[j].isdigit():
                j += 1
        value = float(text[i:j].replace(",", "."))
        k = j
        while k < n and text[k] == " ":
            k += 1
        w = k
        while w < n and text[w].isalpha():
            w += 1
        out.append((value, text[k:w].lower()))
        i = j
    return out


def capacity_l(text: str | None) -> float | None:
    """Litres from a bag's capacity text. "35–40L" is the larger number. A bare
    number is read as litres (nobody types "22" meaning inches without saying
    so); inches, a word or a blank is None."""
    if not text:
        return None
    text = text[:64]                # the column holds 32; never scan more
    nums = _numbers(text)
    found = [v for v, unit in nums if unit in _UNITS]
    bare = text.strip()
    if not found and len(nums) == 1 and all(c.isdigit() or c in ".," for c in bare):
        found = [nums[0][0]]
    best = max(found, default=0)
    return best if 1 <= best <= 300 else None


def volume_for(type_key: str | None, category: str, items: dict,
               defaults: dict[str, float], own: float | None = None) -> float:
    """One unit's packed size: the person's own figure, else the catalogue's
    estimate for its type, else the category default."""
    if own is not None and own > 0:
        return float(own)
    v = (items.get(type_key or "") or {}).get("volume_l")
    if v:
        return float(v)
    return float(defaults.get(category, 0.5))


@dataclass
class Line:
    """What the fit needs from a packing line."""
    id: str
    category: str
    quantity: int
    volume: float               # one unit, litres
    bag: str | None             # a Bag id, or a generic carry_on/checked/backpack


@dataclass
class BagInfo:
    id: str
    label: str
    kind: str                   # backpack | suitcase | personal_item
    capacity: float | None      # litres, None = unknown


def total_l(lines: list[Line]) -> float:
    """The list's packed volume. One pair of shoes is worn on the way, so the
    bulkiest footwear line counts one fewer."""
    tot = sum(ln.volume * max(0, ln.quantity) for ln in lines)
    shoes = [ln for ln in lines if ln.category == "footwear" and ln.quantity > 0]
    if shoes:
        tot -= max(ln.volume for ln in shoes)
    return round(max(0.0, tot), 1)


def usable(b: BagInfo) -> float:
    return (b.capacity or 0) * USABLE


def assign(guess: str | None, picked: list[BagInfo]) -> str | None:
    """Which picked bag a line goes in from the engine's generic guess:
    carry-on things in the smallest picked bag, the rest in the largest
    (unknown capacity sorts as smallest). One bag picked takes everything.
    None when nothing is picked (the line keeps its guess)."""
    if not picked:
        return None
    if len(picked) == 1:
        return picked[0].id
    order = sorted(picked, key=lambda b: (b.capacity or 0, b.kind != "personal_item"))
    return order[0].id if guess == "carry_on" else order[-1].id


@dataclass
class Fill:
    bag: BagInfo
    used: float
    over: bool


@dataclass
class FitReport:
    fills: list[Fill] = field(default_factory=list)
    elsewhere: float = 0.0       # litres in lines not in a picked bag
    total: float = 0.0


def fit(lines: list[Line], picked: list[BagInfo]) -> FitReport:
    rep = FitReport(total=total_l(lines))
    ids = {b.id for b in picked}
    for b in picked:
        used = total_l([ln for ln in lines if ln.bag == b.id])
        rep.fills.append(Fill(bag=b, used=used,
                              over=b.capacity is not None and used > usable(b)))
    rep.elsewhere = total_l([ln for ln in lines if ln.bag not in ids]) if picked else 0.0
    return rep


def suggest(total: float, bags: list[BagInfo], *, one_bag: bool = False) -> list[BagInfo] | None:
    """The smallest of the person's bags that holds `total`, else the smallest
    pair (two of any kind; with one_bag, one bag plus a personal item). None
    when nothing with a known capacity fits, or no bag has one."""
    known = [b for b in bags if b.capacity]
    singles = sorted(known, key=usable)
    for b in singles:
        if usable(b) >= total:
            return [b]
    pairs = []
    for i, a in enumerate(known):
        for b in known[i + 1:]:
            if one_bag and [a.kind, b.kind].count("personal_item") != 1:
                continue
            if usable(a) + usable(b) >= total:
                pairs.append((usable(a) + usable(b), [a, b]))
    if pairs:
        pair = min(pairs, key=lambda p: p[0])[1]
        return sorted(pair, key=lambda b: -(b.capacity or 0))
    return None
