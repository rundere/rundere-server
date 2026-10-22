"""Static trip maps for the timeline PDF (and anything else that wants a PNG).

Draws located points and lines (app/trips/geo.py) onto a Web-Mercator map:
road trips along their route line, flights as great-circle arcs, places as
numbered pins. The background is raster map tiles from TX_MAP_TILE_URL — a
self-hosted TileServer GL, a hosted style, or OpenStreetMap's own tiles (the
default; light, cached, attributed use only) — or, when that is empty or
unreachable, a plain schematic background with the same lines and pins.

Rendered at 2x for print, with Pillow (already present via WeasyPrint). Never
raises into the PDF: any failure returns None and the PDF simply has no map.
"""
from __future__ import annotations

import io
import logging
import math
import os
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("yonder.trips.maps")

TILE = 256
SCALE = 2                    # render at 2x; the PDF shows it at 1x size

# Colours per kind of stop — the PDF legend uses the same table.
KIND_COLOURS = {
    "drive": "#2f6fb0", "flight": "#6a4fb3", "lodging": "#8e44ad",
    "dining": "#d35400", "tour": "#1e8449", "event": "#1e8449",
    "train": "#b03a2e", "carRental": "#2f6fb0", "cruise": "#117a8b",
    "taxi": "#b7950b",
}
DEFAULT_COLOUR = "#4d5359"


@dataclass
class Marker:
    lat: float
    lon: float
    label: str = ""
    colour: str = DEFAULT_COLOUR
    caption: str = ""        # place name written beside the pin (route maps)


@dataclass
class Line:
    points: list[tuple[float, float]]
    colour: str = "#2f6fb0"
    dashed: bool = False


@dataclass
class MapImage:
    data: bytes
    mime: str
    width: int                # CSS px (the PNG itself is SCALE x this)
    height: int
    attribution: str = ""


@dataclass
class _Canvas:
    zoom: int                 # tile zoom actually fetched
    k: float                  # extra magnification (fractional zoom = zoom + log2 k)
    left: float               # image top-left, in magnified world px (1x)
    top: float
    width: int
    height: int
    tiles_ok: bool = field(default=False)

    def world(self, lat: float, lon: float) -> tuple[float, float]:
        x, y = _world(lat, lon, self.zoom)
        return x * self.k, y * self.k

    def xy(self, lat: float, lon: float) -> tuple[float, float]:
        x, y = self.world(lat, lon)
        return (x - self.left) * SCALE, (y - self.top) * SCALE


def _world(lat: float, lon: float, zoom: float) -> tuple[float, float]:
    """Web-Mercator world pixel coordinates at `zoom` (1x, 256px tiles)."""
    lat = max(-85.0511, min(85.0511, lat))
    n = TILE * (2 ** zoom)
    x = (lon + 180.0) / 360.0 * n
    s = math.sin(math.radians(lat))
    y = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * n
    return x, y


def _fit(coords: list[tuple[float, float]], width: int, height: int,
         pad: int = 40, max_zoom: float = 15.0) -> _Canvas:
    """Frame every coordinate as large as it fits, at a fractional zoom:
    tiles come from the whole zoom below and are magnified by the remainder."""
    xs, ys = zip(*(_world(la, lo, 0) for la, lo in coords))
    dx, dy = max(xs) - min(xs), max(ys) - min(ys)
    if dx == 0 and dy == 0:
        zf = 12.0                                   # one place: street-level-ish
    else:
        zf = min(math.log2((width - 2 * pad) / dx) if dx > 0 else max_zoom,
                 math.log2((height - 2 * pad) / dy) if dy > 0 else max_zoom)
    zf = max(1.0, min(max_zoom, zf))
    zoom = int(math.floor(zf))
    canvas = _Canvas(zoom, 2 ** (zf - zoom), 0, 0, width, height)
    wx, wy = zip(*(canvas.world(la, lo) for la, lo in coords))
    canvas.left = (min(wx) + max(wx)) / 2 - width / 2
    canvas.top = (min(wy) + max(wy)) / 2 - height / 2
    return canvas


def great_circle(a: tuple[float, float], b: tuple[float, float], n: int = 48
                 ) -> list[tuple[float, float]]:
    """Points along the great circle from a to b (lat, lon) — a flight's arc."""
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    d = 2 * math.asin(math.sqrt(math.sin((la2 - la1) / 2) ** 2 + math.cos(la1)
                                * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2))
    if d == 0:
        return [a, b]
    out = []
    for i in range(n + 1):
        f = i / n
        A, B = math.sin((1 - f) * d) / math.sin(d), math.sin(f * d) / math.sin(d)
        x = A * math.cos(la1) * math.cos(lo1) + B * math.cos(la2) * math.cos(lo2)
        y = A * math.cos(la1) * math.sin(lo1) + B * math.cos(la2) * math.sin(lo2)
        z = A * math.sin(la1) + B * math.sin(la2)
        out.append((math.degrees(math.atan2(z, math.hypot(x, y))),
                    math.degrees(math.atan2(y, x))))
    return out


# ------------------------------------------------------------------ tiles

_MEMORY: dict[str, bytes] = {}
_TILE_TTL = 30 * 24 * 3600          # OSM asks for >= 7 days of caching


def _cache_dir() -> Path | None:
    from app.config import settings
    # Default: inside the artifacts volume, so the cache survives a redeploy
    # (OSM asks for >= 7 days of caching) with no extra mount or variable.
    raw = settings.map_tile_cache_dir or str(Path(settings.artifacts_dir) / "tile-cache")
    try:
        p = Path(raw)
        p.mkdir(parents=True, exist_ok=True)
        return p if os.access(p, os.W_OK) else None
    except OSError:
        return None


def _tile(template: str, z: int, x: int, y: int) -> bytes | None:
    """One tile's bytes, from memory, the disk cache, or the tile server."""
    url = (template.replace("{z}", str(z)).replace("{x}", str(x))
           .replace("{y}", str(y)).replace("{r}", "@2x"))    # {r}: high-res tiles
    if url in _MEMORY:
        return _MEMORY[url]
    cache = _cache_dir()
    path = None
    if cache is not None:
        import hashlib
        path = cache / (hashlib.sha1(url.encode(), usedforsecurity=False).hexdigest() + ".tile")
        try:
            if time.time() - path.stat().st_mtime < _TILE_TTL:
                data = path.read_bytes()
                _MEMORY[url] = data
                return data
        except OSError:
            pass
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Rundere trip planner (self-hosted; "
                          "+https://github.com/rundere/rundere-server)"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = resp.read()
    except Exception:
        return None
    _MEMORY[url] = data
    if path is not None:
        try:
            path.write_bytes(data)
        except OSError:
            pass
    return data


def _paint_tiles(img, canvas: _Canvas, template: str) -> bool:
    """Paste the tiles covering the canvas; False if any is missing."""
    from PIL import Image
    z, n, t = canvas.zoom, 2 ** canvas.zoom, TILE * canvas.k   # t: tile size on the image (1x)
    x0, y0 = int(canvas.left // t), int(canvas.top // t)
    x1 = int((canvas.left + canvas.width) // t)
    y1 = int((canvas.top + canvas.height) // t)
    jobs = [(tx, ty) for tx in range(x0, x1 + 1) for ty in range(y0, y1 + 1)
            if 0 <= ty < n]
    if len(jobs) > 80:                          # a sanity cap per image
        return False
    with ThreadPoolExecutor(max_workers=6) as pool:
        blobs = list(pool.map(lambda t: _tile(template, z, t[0] % n, t[1]), jobs))
    ok = True
    size = int(math.ceil(t * SCALE)) + 1                  # +1 hides seams
    for (tx, ty), blob in zip(jobs, blobs):
        if blob is None:
            ok = False
            continue
        try:
            tile = Image.open(io.BytesIO(blob)).convert("RGB").resize((size, size))
        except Exception:
            ok = False
            continue
        img.paste(tile, (int(math.floor((tx * t - canvas.left) * SCALE)),
                         int(math.floor((ty * t - canvas.top) * SCALE))))
    return ok


# ------------------------------------------------------------------ drawing

def _font(px: int):
    from PIL import ImageFont
    for name in ("DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, px)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=px)
    except TypeError:
        return ImageFont.load_default()


def _hex(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _schematic(draw, canvas: _Canvas) -> None:
    """No tiles: a quiet background with a lat/lon grid for scale."""
    w, h = canvas.width * SCALE, canvas.height * SCALE
    draw.rectangle([0, 0, w, h], fill=(238, 241, 244))
    world = TILE * 2 ** canvas.zoom * canvas.k
    span = canvas.width / world * 360          # degrees of longitude in view
    step = next((s for s in (0.01, 0.05, 0.1, 0.5, 1, 2, 5, 10, 20, 45)
                 if span / s <= 8), 90)
    lon = math.floor((canvas.left / world * 360 - 180) / step) * step
    while True:
        x, _ = canvas.xy(0, lon)
        if x > w:
            break
        draw.line([(x, 0), (x, h)], fill=(222, 227, 232), width=SCALE)
        lon += step
    world_y = TILE * 2 ** canvas.zoom * canvas.k

    def lat_at(py: float) -> float:              # inverse Mercator
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * py / world_y))))
    south, north = lat_at(canvas.top + canvas.height), lat_at(canvas.top)
    lat = math.floor(south / step) * step
    while lat <= north + step:
        _, y = canvas.xy(lat, 0)
        if 0 <= y <= h:
            draw.line([(0, y), (w, y)], fill=(222, 227, 232), width=SCALE)
        lat += step


def _polyline(draw, pts, colour, width, dashed=False):
    if len(pts) < 2:
        return
    if not dashed:
        draw.line(pts, fill=(255, 255, 255), width=width + 4 * SCALE // 2, joint="curve")
        draw.line(pts, fill=_hex(colour), width=width, joint="curve")
        return
    dash, gap, carry, on = 9 * SCALE, 6 * SCALE, 0.0, True
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        seg = math.hypot(x2 - x1, y2 - y1)
        pos = 0.0
        while pos < seg:
            run = min((dash if on else gap) - carry, seg - pos)
            if on:
                a, b = pos / seg, (pos + run) / seg
                draw.line([(x1 + (x2 - x1) * a, y1 + (y2 - y1) * a),
                           (x1 + (x2 - x1) * b, y1 + (y2 - y1) * b)],
                          fill=_hex(colour), width=width)
            pos += run
            carry += run
            if carry >= (dash if on else gap) - 1e-6:
                carry, on = 0.0, not on


def _marker(draw, xy, label: str, colour: str, font, caption: str = "",
            caption_font=None):
    """A pin: a circle, or a pill wide enough for its label; white-ringed so it
    reads on any background. A caption, if any, sits to its right."""
    x, y = xy
    r = 10 * SCALE
    tw = 0
    if label:
        box = draw.textbbox((0, 0), label, font=font)
        tw = box[2] - box[0]
    half = max(0, tw / 2 + 5 * SCALE - r)          # extra half-width for long labels
    ring = 2 * SCALE
    draw.rounded_rectangle([x - r - half - ring, y - r - ring, x + r + half + ring, y + r + ring],
                           radius=r + ring, fill=(255, 255, 255))
    draw.rounded_rectangle([x - r - half, y - r, x + r + half, y + r], radius=r,
                           fill=_hex(colour))
    if label:
        draw.text((x, y), label, fill=(255, 255, 255), font=font, anchor="mm")
    if caption and caption_font is not None:
        draw.text((x + r + half + 5 * SCALE, y), caption, fill=(30, 30, 34),
                  font=caption_font, anchor="lm",
                  stroke_width=3 * SCALE // 2, stroke_fill=(255, 255, 255))


def _cluster(markers: list[Marker], canvas: _Canvas) -> list[Marker]:
    """Pins that would overlap become one ("1,4"); an unlabelled end-dot
    under a labelled pin just disappears."""
    out: list[tuple[float, float, Marker]] = []
    near = 20 * SCALE
    for m in markers:
        x, y = canvas.xy(m.lat, m.lon)
        hit = next((o for o in out if math.hypot(o[0] - x, o[1] - y) < near), None)
        if hit is None:
            out.append((x, y, Marker(m.lat, m.lon, m.label, m.colour, m.caption)))
            continue
        kept = hit[2]
        if m.label and m.label not in kept.label.split(","):
            kept.label = f"{kept.label},{m.label}" if kept.label else m.label
        if not kept.caption:
            kept.caption = m.caption
    return [o[2] for o in out]


def render_map(markers: list[Marker], lines: list[Line], *, width: int, height: int
               ) -> MapImage | None:
    """A PNG/JPEG map of the given markers and lines, framed to fit them all."""
    from app.config import settings
    coords = [(m.lat, m.lon) for m in markers] + [p for ln in lines for p in ln.points]
    if not coords:
        return None
    try:
        from PIL import Image, ImageDraw
        canvas = _fit(coords, width, height)
        img = Image.new("RGB", (width * SCALE, height * SCALE), (238, 241, 244))
        draw = ImageDraw.Draw(img)
        template = (settings.map_tile_url or "").strip()
        canvas.tiles_ok = bool(template) and _paint_tiles(img, canvas, template)
        if not canvas.tiles_ok:
            _schematic(draw, canvas)
        for ln in lines:
            _polyline(draw, [canvas.xy(la, lo) for la, lo in ln.points], ln.colour,
                      4 * SCALE if not ln.dashed else 3 * SCALE, ln.dashed)
        font, caption_font = _font(11 * SCALE), _font(10 * SCALE)
        for m in _cluster(markers, canvas):
            _marker(draw, canvas.xy(m.lat, m.lon), m.label, m.colour, font,
                    m.caption, caption_font)
        attribution = settings.map_tile_attribution if canvas.tiles_ok else ""
        if attribution:
            small = _font(8 * SCALE)
            box = draw.textbbox((0, 0), attribution, font=small)
            tw, th = box[2] - box[0], box[3] - box[1]
            W, H = img.size
            draw.rectangle([W - tw - 10 * SCALE, H - th - 8 * SCALE, W, H],
                           fill=(255, 255, 255))
            draw.text((W - tw - 5 * SCALE, H - th - 5 * SCALE), attribution,
                      fill=(80, 80, 80), font=small)
        buf = io.BytesIO()
        if canvas.tiles_ok:
            img.save(buf, "JPEG", quality=85, optimize=True)
            mime = "image/jpeg"
        else:
            img.save(buf, "PNG", optimize=True)
            mime = "image/png"
        return MapImage(buf.getvalue(), mime, width, height,
                        attribution if canvas.tiles_ok else "")
    except Exception:
        log.warning("map render failed", exc_info=True)
        return None
