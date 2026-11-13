# Travel themes

Each folder here is one travel theme (SPEC-8 §15, D-237): a set of semantic
colours for a light and a dark mode, plus a scenery floor (D-239). Base isn't
here; it stays in `design/tokens/tokens.json`.

```
design/themes/<id>/
├── theme.json        the source: name, description, version, tags, colours per mode
├── scene.svg         the source: the floor, coloured only with rd-scene-* tokens
├── scene-light.svg   generated: fixed colours, for iOS image sets / Android drawables
└── scene-dark.svg    generated
```

## Adding a theme

1. Copy a folder and rename it. The id is a lowercase slug and matches the
   folder; `base`, `random`, `next-trip` and `system` are taken.
2. In `theme.json`, set `name`, `description`, `tags` (what *Match my next trip*
   matches against a destination, D-240), and for both `light` and `dark`:
   - `color`: all 15 semantic roles, as `#RRGGBB` (`border-subtle` may be `rgba()`);
   - `effect`: `shadow-raised` and `glow-color`;
   - `scene`: the nine scene roles (`far`, `mid`, `near`, `feature`, `detail`,
     `sun`, `light`, `star`, `foam`).
3. Draw `scene.svg`: 1440×240, bottom-anchored, flat shapes, every fill or
   stroke `style="fill:var(--rd-scene-<role>)"`. Keep the signature shape in
   the middle third so it survives a phone crop.
4. Run `uv run python scripts/build_tokens.py`, then
   `uv run --extra dev pytest -q tests/test_theme_packages.py`.

The tests hold every theme to the guardrails:

- text, secondary and muted text, accent, highlight, success and danger reach
  4.5:1 on bg, surface and surface-raised, and on-accent reaches 4.5:1 on accent
  and accent-hover;
- accent, highlight, success and danger differ by hue family or lightness;
- highlight stays amber, so "next up" reads the same everywhere;
- the scene's far, mid and near layers stay under 2.5:1 against bg, and the
  feature at about 3:1 or less.

Bump `version` when a theme's values change, so the apps refresh their cached copy.
