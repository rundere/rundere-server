"""Rasterise a theme's fixed-colour scene SVG to a transparent PNG (D-245).

iOS has no public way to draw an SVG it fetched at run time, so each scene is
also published as PNG at 2x and 3x of the SVG's 1440x240 size. A theme newer than
the app build can then show its scenery without a new app release (D-240).

This reads only what `scripts/build_tokens.py` allows in a scene (SPEC-8 §15):
`rect` (with `rx`), `circle`, `ellipse` and `path` (M L H V C S Q T Z, absolute),
flat `fill` / `stroke` colours, `stroke-width`, round caps and `opacity`. Anything
else raises, so a theme can't ship a scene this renderer would draw wrongly.
Pillow only (already a dependency); no system library, so CI and a laptop agree.
"""
from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image, ImageDraw, PngImagePlugin

SCALES = (2, 3)
SUPERSAMPLE = 2          # drawn at this multiple of the output, then reduced (anti-aliasing)
SOURCE_KEY = "rd-source"  # PNG text chunk holding the SVG's sha256 (the staleness check)
_HEX = re.compile(r"#([0-9a-fA-F]{6})")
_TOKEN = re.compile(r"[MLHVCSQTZ]|-?\d*\.?\d+(?:e-?\d+)?")
_CURVE_STEPS = 24


class SceneError(ValueError):
    pass


def source_hash(svg: str) -> str:
    return hashlib.sha256(svg.encode()).hexdigest()


def _colour(value: str | None, opacity: float) -> tuple[int, int, int, int] | None:
    if value in (None, "none"):
        return None
    m = _HEX.fullmatch(value)
    if not m:
        raise SceneError(f"unsupported colour {value!r}; scenes use #RRGGBB")
    h = m.group(1)
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), round(255 * opacity))


def _bezier(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Flatten a quadratic or cubic Bezier (control points, start first)."""
    out = []
    for i in range(1, _CURVE_STEPS + 1):
        t = i / _CURVE_STEPS
        pts = points
        while len(pts) > 1:
            pts = [((1 - t) * a[0] + t * b[0], (1 - t) * a[1] + t * b[1])
                   for a, b in zip(pts, pts[1:])]
        out.append(pts[0])
    return out


def flatten(d: str) -> list[list[tuple[float, float]]]:
    """A path's subpaths as polylines."""
    stray = re.search(r"[A-Za-z]", re.sub(r"[MLHVCSQTZ]|(?<=\d)e", "", d))
    if stray:
        raise SceneError(f"unsupported path command {stray.group(0)!r}")
    tokens = _TOKEN.findall(d)
    subs: list[list[tuple[float, float]]] = []
    cur: list[tuple[float, float]] = []
    i, cmd = 0, ""
    pos = (0.0, 0.0)
    ctrl: tuple[float, float] | None = None     # last control point, for S and T
    last = ""

    def num() -> float:
        nonlocal i
        v = float(tokens[i])
        i += 1
        return v

    while i < len(tokens):
        if tokens[i].isalpha():
            cmd = tokens[i]
            i += 1
            if cmd == "Z":
                if cur:
                    subs.append(cur)
                cur, ctrl, last = [], None, "Z"
                continue
        if cmd == "M":
            if cur:
                subs.append(cur)
            pos = (num(), num())
            cur = [pos]
            cmd = "L"                           # extra pairs after M are lines
            ctrl = None
        elif cmd == "L":
            pos = (num(), num()); cur.append(pos); ctrl = None
        elif cmd == "H":
            pos = (num(), pos[1]); cur.append(pos); ctrl = None
        elif cmd == "V":
            pos = (pos[0], num()); cur.append(pos); ctrl = None
        elif cmd == "C":
            p1, p2, p3 = (num(), num()), (num(), num()), (num(), num())
            cur += _bezier([pos, p1, p2, p3]); pos, ctrl = p3, p2
        elif cmd == "S":
            p1 = (2 * pos[0] - ctrl[0], 2 * pos[1] - ctrl[1]) if ctrl and last in "CS" else pos
            p2, p3 = (num(), num()), (num(), num())
            cur += _bezier([pos, p1, p2, p3]); pos, ctrl = p3, p2
        elif cmd == "Q":
            p1, p2 = (num(), num()), (num(), num())
            cur += _bezier([pos, p1, p2]); pos, ctrl = p2, p1
        elif cmd == "T":
            p1 = (2 * pos[0] - ctrl[0], 2 * pos[1] - ctrl[1]) if ctrl and last in "QT" else pos
            p2 = (num(), num())
            cur += _bezier([pos, p1, p2]); pos, ctrl = p2, p1
        else:
            raise SceneError(f"unsupported path command {cmd!r}")
        last = cmd
    if cur:
        subs.append(cur)
    return subs


def render(svg: str, scale: int) -> Image.Image:
    """The scene at `scale` times its SVG size, RGBA on a transparent ground."""
    root = ET.fromstring(svg)
    width, height = int(float(root.get("width"))), int(float(root.get("height")))
    k = scale * SUPERSAMPLE
    big = Image.new("RGBA", (width * k, height * k), (0, 0, 0, 0))

    def px(pts):
        return [(x * k, y * k) for x, y in pts]

    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag in ("svg", "title"):
            continue
        opacity = float(el.get("opacity", "1"))
        fill = _colour(el.get("fill", "#000000"), opacity)
        stroke = _colour(el.get("stroke"), opacity)
        layer = Image.new("RGBA", big.size, (0, 0, 0, 0))   # one layer per shape: opacity
        draw = ImageDraw.Draw(layer)                         # composites, never stacks
        if tag == "rect":
            x, y = float(el.get("x", 0)), float(el.get("y", 0))
            w, h = float(el.get("width")), float(el.get("height"))
            r = float(el.get("rx", 0))
            box = [x * k, y * k, (x + w) * k - 1, (y + h) * k - 1]
            if r:
                draw.rounded_rectangle(box, radius=r * k, fill=fill)
            else:
                draw.rectangle(box, fill=fill)
        elif tag in ("circle", "ellipse"):
            cx, cy = float(el.get("cx")), float(el.get("cy"))
            rx = float(el.get("r", el.get("rx", 0)))
            ry = float(el.get("r", el.get("ry", 0)))
            draw.ellipse([(cx - rx) * k, (cy - ry) * k, (cx + rx) * k, (cy + ry) * k], fill=fill)
        elif tag == "path":
            for sub in flatten(el.get("d")):
                if fill and len(sub) > 2:
                    draw.polygon(px(sub), fill=fill)
                if stroke and len(sub) > 1:
                    sw = float(el.get("stroke-width", 1)) * k
                    draw.line(px(sub), fill=stroke, width=round(sw), joint="curve")
                    if el.get("stroke-linecap") == "round":
                        for x, y in px([sub[0], sub[-1]]):
                            draw.ellipse([x - sw / 2, y - sw / 2, x + sw / 2, y + sw / 2], fill=stroke)
        else:
            raise SceneError(f"unsupported scene element <{tag}>")
        big = Image.alpha_composite(big, layer)
    return big.resize((width * scale, height * scale), Image.LANCZOS)


def png_bytes(svg: str, scale: int) -> bytes:
    """PNG for `svg`, tagged with the SVG's hash so `build_tokens.py --check`
    can tell when a scene changed without comparing pixels (which differ between
    Pillow builds)."""
    import io
    meta = PngImagePlugin.PngInfo()
    meta.add_text(SOURCE_KEY, source_hash(svg))
    buf = io.BytesIO()
    render(svg, scale).save(buf, "PNG", optimize=True, pnginfo=meta)
    return buf.getvalue()


def is_current(path: Path, svg: str, scale: int) -> bool:
    """True when `path` is a PNG of this SVG at this scale."""
    try:
        with Image.open(path) as im:
            w, h = int(float(ET.fromstring(svg).get("width"))), int(float(ET.fromstring(svg).get("height")))
            return im.size == (w * scale, h * scale) and im.text.get(SOURCE_KEY) == source_hash(svg)
    except (OSError, ValueError, AttributeError):
        return False
