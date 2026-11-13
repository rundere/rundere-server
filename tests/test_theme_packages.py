"""SPEC-8 §15: travel theme packages (D-237) and their scenery floors (D-239).

Every package under design/themes/ must pass the contrast guardrails, keep its
scene quiet, and match the generated outputs. A new theme that fails any of
these fails CI before it reaches a page.
"""
import itertools
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build_tokens as bt  # noqa: E402

THEMES = bt.load_themes()
IDS = [t["id"] for t in THEMES]


def _lum(hex_: str) -> float:
    def ch(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (int(hex_[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def hue(hex_: str) -> float:
    r, g, b = (int(hex_[i:i + 2], 16) / 255 for i in (1, 3, 5))
    mx, mn = max(r, g, b), min(r, g, b)
    if mx == mn:
        return 0.0
    d = mx - mn
    if mx == r:
        h = ((g - b) / d) % 6
    elif mx == g:
        h = (b - r) / d + 2
    else:
        h = (r - g) / d + 4
    return h * 60


def test_the_five_5_0_themes_are_packaged():
    assert IDS == ["coast", "desert", "forest", "metro", "winter"]


def test_generated_outputs_are_fresh():
    # Like `uv lock --check`: edit a package, then run scripts/build_tokens.py.
    assert bt.stale() == []


@pytest.mark.parametrize("theme", THEMES, ids=IDS)
@pytest.mark.parametrize("mode", bt.MODES)
def test_guardrail_1_text_and_signals_reach_4_5(theme, mode):
    c = theme["modes"][mode]["color"]
    misses = [
        f"{fg} on {bg} {contrast(c[fg], c[bg]):.2f}"
        for fg in ("text", "text-secondary", "text-muted", "accent", "highlight", "success", "danger")
        for bg in ("bg", "surface", "surface-raised")
        if contrast(c[fg], c[bg]) < 4.5
    ]
    misses += [
        f"on-accent on {bg} {contrast(c['on-accent'], c[bg]):.2f}"
        for bg in ("accent", "accent-hover")
        if contrast(c["on-accent"], c[bg]) < 4.5
    ]
    assert not misses, misses


@pytest.mark.parametrize("theme", THEMES, ids=IDS)
@pytest.mark.parametrize("mode", bt.MODES)
def test_guardrail_2_signals_are_told_apart(theme, mode):
    # Accent, highlight, success and danger differ by hue family or lightness,
    # so "Committed" never looks like the accent and danger never like a link.
    c = theme["modes"][mode]["color"]
    for a, b in itertools.combinations(("accent", "highlight", "success", "danger"), 2):
        dh = abs(hue(c[a]) - hue(c[b]))
        dh = min(dh, 360 - dh)
        assert dh >= 25 or contrast(c[a], c[b]) >= 1.5, (a, b, c[a], c[b])


@pytest.mark.parametrize("theme", THEMES, ids=IDS)
@pytest.mark.parametrize("mode", bt.MODES)
def test_guardrail_3_highlight_stays_amber(theme, mode):
    assert 30 <= hue(theme["modes"][mode]["color"]["highlight"]) <= 50


@pytest.mark.parametrize("theme", THEMES, ids=IDS)
@pytest.mark.parametrize("mode", bt.MODES)
def test_scenery_stays_quiet(theme, mode):
    # D-239: the floor is tints of the ground; only the signature shape comes forward.
    s, bg = theme["modes"][mode]["scene"], theme["modes"][mode]["color"]["bg"]
    for role in ("far", "mid", "near"):
        assert contrast(s[role], bg) < 2.5, (role, contrast(s[role], bg))
    assert contrast(s["feature"], bg) <= 3.1, contrast(s["feature"], bg)


def test_manifest_lists_base_first_then_every_package():
    manifest = json.loads((bt.THEMES / "themes.json").read_text())
    assert [t["id"] for t in manifest["themes"]] == ["base"] + IDS
    base = manifest["themes"][0]
    assert base["kind"] == "base" and "scene" not in base
    assert base["modes"]["dark"]["color"]["bg"] == "#0A1624"   # resolved from tokens.json
    for t in manifest["themes"][1:]:
        for key in ("svg", "light", "dark"):
            assert (bt.THEMES / t["scene"][key]).is_file()


def test_css_blocks_follow_the_mode_rules():
    css = (bt.TOKENS / "tokens.css").read_text()
    head = css.split(bt.CSS_BEGIN)[0]
    assert ':root[data-theme="light"]' in head            # Base stays hand-kept above the block
    for tid in IDS:
        assert f':root[data-travel="{tid}"] {{' in css
        assert f':root[data-travel="{tid}"][data-theme="light"] {{' in css
        assert f':root[data-travel="{tid}"]:not([data-theme="dark"]) {{' in css


def test_app_palettes_are_keyed_by_id_with_a_base_fallback():
    swift = (bt.TOKENS / "RundereThemes.swift").read_text()
    kotlin = (bt.TOKENS / "RundereThemes.kt").read_text()
    assert 'all[id] ?? all["base"]!' in swift
    assert "?: return if (darkTheme) RundereDark else RundereLight" in kotlin
    for tid in IDS:
        assert f'"{tid}": RDThemePalettes(' in swift and f'"{tid}" to RundereSchemes(' in kotlin
    assert "enum class" not in kotlin                     # no closed list of themes (D-237)


def test_scene_exports_are_fixed_colour():
    for t in THEMES:
        for mode in bt.MODES:
            svg = (bt.THEMES / t["id"] / f"scene-{mode}.svg").read_text()
            assert "var(--rd-scene" not in svg
            assert t["modes"][mode]["scene"]["near"] in svg


# ---------------------------------------------------------------- validation

def _copy(tmp_path: Path, tid: str = "coast") -> Path:
    dest = tmp_path / tid
    shutil.copytree(bt.THEMES / "coast", dest)
    for f in dest.glob("scene-*.svg"):
        f.unlink()
    return dest


def _edit(folder: Path, fn) -> None:
    pkg = json.loads((folder / "theme.json").read_text())
    fn(pkg)
    (folder / "theme.json").write_text(json.dumps(pkg))


def test_a_valid_copy_loads(tmp_path):
    assert bt.load_theme(_copy(tmp_path))["kind"] == "travel"


@pytest.mark.parametrize("fn, reason", [
    (lambda p: p.update(id="beach"), "must match its folder"),
    (lambda p: p["modes"]["dark"]["color"].pop("accent"), "exactly"),
    (lambda p: p["modes"]["light"]["color"].update(accent="blue"), "#RRGGBB"),
    (lambda p: p["modes"].pop("light"), "missing the light mode"),
    (lambda p: p.update(version=0), "version"),
])
def test_a_broken_package_is_refused(tmp_path, fn, reason):
    folder = _copy(tmp_path)
    _edit(folder, fn)
    with pytest.raises(bt.ThemeError, match=reason):
        bt.load_theme(folder)


@pytest.mark.parametrize("tid", ["base", "random", "next-trip", "Coast"])
def test_reserved_or_badly_formed_ids_are_refused(tmp_path, tid):
    folder = _copy(tmp_path, tid)
    _edit(folder, lambda p: p.update(id=tid))
    with pytest.raises(bt.ThemeError, match="slug"):
        bt.load_theme(folder)


def test_a_scene_with_a_literal_colour_is_refused(tmp_path):
    folder = _copy(tmp_path)
    svg = folder / "scene.svg"
    svg.write_text(svg.read_text().replace("</svg>", '<rect fill="#FF0000" width="1" height="1"/></svg>'))
    with pytest.raises(bt.ThemeError, match="literal colours"):
        bt.load_theme(folder)


def test_a_scene_with_an_unknown_role_is_refused(tmp_path):
    folder = _copy(tmp_path)
    svg = folder / "scene.svg"
    svg.write_text(svg.read_text().replace("</svg>", '<rect style="fill:var(--rd-scene-lava)"/></svg>'))
    with pytest.raises(bt.ThemeError, match="unknown scene roles"):
        bt.load_theme(folder)
