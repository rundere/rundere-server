# Rundere design system

The brand, tokens and platform rules for every Rundere surface: the web app, iOS, iPadOS
and Android. Builder agents: read this file before touching UI, use the files here
as-is, and don't invent new colours, fonts or logo variants.

- **Visual reference (canvas):** https://claude.ai/artifact/MMcg9tcUzQqRPKm2HUy733
  (private to the owner — ask for access if you can't open it). Boards: Logo, Color,
  Typography, Foundations & components, Web dashboard, iOS guidelines + screens,
  iPadOS split view, Android guidelines + screens.
- **Tokens source of truth:** [`tokens/tokens.json`](tokens/tokens.json). The CSS,
  Swift and Kotlin files are hand-kept mirrors; change all four in one commit.

```
design/
├── logo/     mark, wordmark, lockups (SVG; outlined — no font needed)
├── icons/    favicons, PWA icons, iOS app icon master, Android adaptive layers
├── fonts/    self-hosted Outfit, Figtree, JetBrains Mono (woff2, Latin + Latin-ext) + fonts.css
├── audio/    rundere.m4a + rundere.webm — the name, spoken (pronunciation button)
└── tokens/   tokens.json · tokens.css · RundereTheme.swift · RundereTheme.kt
```

---

## 1. The mark

A jet **taking off** over a four-point star, drawn as a rune: straight strokes,
mitred corners, cut ends, no fills. The dot below the tail is the **point of origin**
(the gate-glyph idea): where you are, what the plane climbs away from. The same dot
separates the wordmark: **Run·dere**.

| File | Use |
|---|---|
| `logo/rundere-mark.svg` | Full mark, `currentColor`. Inline it and set `color`. ≥ 64px. |
| `logo/rundere-mark-silver.svg` / `-night.svg` | Same, fixed colour, for `<img>` on dark / light grounds. |
| `logo/rundere-mark-32.svg` | Reduced mark for 24–63px: no tail fins, solid fuselage. `currentColor`. |
| `logo/rundere-mark-16.svg` | Pixel-fitted mark for 16px only. `currentColor`. |
| `logo/rundere-wordmark*.svg` | "Run·dere", outlined from Outfit Light 300. |
| `logo/rundere-lockup-horizontal*.svg` | Mark + wordmark in a row: nav bars, headers. |
| `logo/rundere-lockup-stacked*.svg` | Mark over wordmark: splash, login, marketing. |

Each logo file comes in three variants: `currentColor` (no suffix), `-silver` (#E6EAF0,
for dark grounds) and `-night` (#0F2135, for light grounds).

**Rules**
- Always points up. Never rotate, flip, fill, outline-stroke or add effects other than the glow.
- Glow (`filter: drop-shadow(0 0 14px rgba(169,204,255,.55))`) only on dark grounds, only on the mark.
- Colours: Silver 100 on dark, Night 800 on light. Beacon 300 is allowed for the iOS dark
  app icon only. Never Runway or status colours.
- Clear space = 2× fuselage width on every side. Minimum sizes: 16px (use `-16`), lockup 96px wide.
- Switch files by size (don't just scale down the full mark): ≥ 64px full · 24–63px `-32` · 16px `-16`.
- Don't typeset the wordmark: use the outlined SVG. Never swap the dot for a hyphen or period.

## 1a. Pronunciation

Say it **/ run-THAIR /** — like "run there". `audio/` holds the owner's own recording
of the name, trimmed to 1.5 s and normalised to −1.5 dBFS peak, in two encodings:
`rundere.m4a` (AAC, ~13 KB: iOS, Safari, Chrome, Edge, Android) and `rundere.webm`
(Opus, ~9 KB: browsers without AAC). Use these for every "hear it" control on web, iOS
and Android; don't use a TTS voice. Web serves them at `/brand/audio/*` and lists both as
`<source>`s, falling back to the browser's speech voice only if neither plays. To replace
it: record a new take, trim to the word, normalise, export both formats, keep the names.

## 2. Icons

| File | Where |
|---|---|
| `icons/favicon.svg` | `<link rel="icon" type="image/svg+xml">`. Switches Night/Silver with the OS theme. |
| `icons/favicon.ico` | 16/32/48 fallback: `<link rel="icon" href="/favicon.ico" sizes="any">`. |
| `icons/favicon-16.png`, `favicon-32.png` | PNG fallbacks (mark on a Night 800 tile). |
| `icons/apple-touch-icon.png` | 180×180, no alpha. |
| `icons/icon-192.png`, `icon-512.png`, `icon-maskable-512.png`, `site.webmanifest` | PWA. |
| `icons/app-icon.svg` → `app-icon-1024.png` | iOS/iPadOS master: square, **unrounded**, no alpha; the OS applies the mask. |
| `icons/android/ic_launcher_{background,foreground,monochrome}.svg` | Android adaptive icon (108dp canvas, glyph inside the 66dp safe circle). Import via Android Studio → Vector Asset. Monochrome is required for themed icons. |

PNG files are rendered from the SVGs next to them. If you change an SVG, re-render the PNGs.

## 3. Colour

Dark is the designed default on every platform; light follows the OS. The web app is dark until the person picks light with the header toggle, whatever the OS prefers (SPEC-8 D-201).

| Ramp | Role |
|---|---|
| **Night** 950–400 | Grounds and surfaces; ink on light. |
| **Silver** 50–600 | Text, the mark, dividers. |
| **Beacon** 100–700 | The only interactive colour: primary action, links, focus, selection. |
| **Runway** 100–700 | "Next up" and warnings only. Never decoration. |
| **Status** | Success green / danger red, each with a dark-mode (400) and light-mode (600) shade. |

Use semantic tokens in UI code, never the ramp values directly:

| Token | Dark | Light |
|---|---|---|
| `bg` | #0A1624 | #F4F6F9 |
| `surface` | #0F2135 | #FFFFFF |
| `surface-raised` | #16304A | #FFFFFF |
| `border` | #22405F | #CBD3DD |
| `text` | #E6EAF0 | #0F2135 |
| `text-secondary` | #AAB6C4 | #44536A |
| `text-muted` | #8594A6 | #5E6E82 |
| `accent` | #7FB2FF | #1F57B0 |
| `on-accent` | #0A1624 | #FFFFFF |
| `highlight` | #F2B544 | #8A5C08 |
| `success` / `danger` | #4CC38A / #F2766B | #1E7F52 / #C23B30 |

Status badges: a capsule with the colour at 14% alpha as the fill, full colour for text and a 6px dot.

## 4. Typography

| Family | Use |
|---|---|
| **Outfit** (300/400/500) | Display and headings. It matches the wordmark. Never below 20px. |
| **Figtree** (400/500/600) | UI and body on web and Android. On iOS/iPadOS use **SF Pro** instead. |
| **JetBrains Mono** (400/500) | IATA codes, flight numbers, confirmation refs, times in tables. SF Mono on iOS. |

**Web:** the app serves these from `design/fonts/` (`/brand/fonts/fonts.css`, linked after `tokens.css`),
so no page calls a font CDN. Outfit is used only for headings of 20px and up.

**Web scale** (size/line, weight): display 56/64 300 · h1 40/48 300 · h2 28/36 400 · h3 20/28 500 ·
body-lg 17/26 · body 15/22 · label 14/20 600 · caption 13/18 500 ·
overline 12/16 600 +0.12em uppercase · mono 13/20 500.

**iOS** (Dynamic Type; Outfit via `Font.custom(_:size:relativeTo:)`): largeTitle 34 Outfit Light ·
title1 28 / title2 22 Outfit Regular · everything from title3 down is SF Pro system styles.

**Android** (sp): displayMedium 45/52 Outfit 300 · headlineLarge 32/40 Outfit 300 ·
headlineSmall 24/32 Outfit 400 · titleLarge 22/28 Outfit 400 · titleMedium 16/24 Figtree 600 ·
bodyLarge 16/24 · bodyMedium 14/20 · labelLarge 14/20 600 · codes JetBrains Mono 14/20.

## 5. Foundations

- **Space:** 4pt grid: 4 · 8 · 12 · 16 · 24 · 32 · 48 · 64.
- **Radius:** 6 (xs, chips) · 10 (controls; 12 continuous on iOS) · 16 (cards) · 24 (sheets) · pill.
- **Elevation:** use a lighter surface plus a hairline border (`rgba(170,182,196,.14)`), not heavy shadows.
  Raised = Night 700 + `0 8px 24px rgba(0,0,0,.35)`.
- **Focus:** a 2px surface gap, a 2px Beacon 300 ring and a soft Beacon glow (see `tokens.css`).
- **Touch targets:** 44×44pt on iOS and web, 48dp on Android. Controls are 44 tall (32 for the small size).
- **Motion:** quick 120ms ease-out · standard 200ms `(0.2,0,0,1)` · takeoff 320ms `(0.3,0,0,1)` for
  screen/sheet entrances. Reduce Motion means crossfade only.
- **Icons:** 24 grid, 1.75 stroke, round caps/joins, outline only. On iOS prefer SF Symbols; on
  Android prefer Material Symbols (Outlined, weight 400).

## 6. Components (all platforms)

- **One Beacon-filled primary action per screen.** Secondary is Night 700 fill + border; tertiary is a text button.
- **Destructive:** danger text on a 8% danger fill, with a 50% danger border.
- **Next-up card:** the signature element. Runway overline ("NEXT UP · IN 3 DAYS"), mono IATA
  codes, and a dashed route line that ends in the brand dot. A soft Night radial behind it. Only one per screen.
- **Timeline:** a vertical 1px Night 600 line with 8px nodes; the next item's node is Silver with the glow.
- **List rows:** min 56px (web) / 60pt (iOS) / 72dp (Android), leading 32–40px icon tile, status badge trailing.
- **Item-type chips:** Flight, Hotel, Train, Event, … Night 700 pill with an icon and a label.

## 7. Platform specifics

**Web (desktop):** a 248px sidebar with the horizontal lockup at the top. The active item has a Night 700
fill and a 2px Beacon inset bar. Content is 48px padded. Capture is the primary header button.
Load `tokens/tokens.css`, then use `var(--rd-*)`.

**iOS:** tab bar with Today · Trips · Capture · Review · Journal (SF Symbols `safari`, `suitcase`,
`plus.viewfinder`, `tray`, `book`). Use native controls tinted with `.rdAccent`, `List(.insetGrouped)`,
sheets with `[.medium, .large]` detents, and `.ultraThinMaterial` chrome. Success haptic on commit.
App icon: default / dark (Beacon 300 on black) / tinted (greyscale). Next-flight Live Activity.
Use `tokens/RundereTheme.swift`.

**iPadOS:** `NavigationSplitView` with sidebar · timeline · inspector in landscape. The sidebar replaces the
tab bar at regular width and collapses to the iPhone tab bar in compact width. The selected sidebar
row is Beacon-filled. Keyboard: ⌘N capture, ⌘R review, ⌘F search. Hover lift and a context menu on
every timeline item. Drop an email, PDF or .ics on the window to capture it. Supports down to 500pt wide.

**Android (Material 3 / Compose):** NavigationBar with Today · Trips · Review · Discover · Journal,
plus an ExtendedFloatingActionButton for **Capture**. LargeTopAppBar, SingleChoiceSegmentedButtonRow
for Timeline/Map/Packing, and ModalBottomSheet for item detail. Edge-to-edge, predictive back.
Dynamic colour is off by default (offered as a setting). Use `tokens/RundereTheme.kt`
(the package name is a placeholder; move it into the app's module).

## 8. Don'ts

- Gradient washes anywhere except the soft Night radial behind the mark and the next-up card.
- More than one accent. Runway is a signal, not a second brand colour.
- Emoji as icons. Inter/Roboto/Arial as brand type.
- A glow on light grounds, or on anything other than the mark and the next timeline node.
- Status conveyed by colour alone: always add a label or an icon.
