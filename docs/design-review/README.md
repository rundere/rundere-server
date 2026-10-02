# Run·dere web UI — page-by-page design review (2026-10-02)

Reviewed against `design/README.md` (tokens, type, components, web rules) and the
canvas's Web dashboard board. Branch `claude/wizardly-maxwell-dp5ymc` at `9815972`.

## How this was done, and what it can't tell you

- 33 pages rendered in headless Chromium against seeded demo data (admin user, a current,
  a future and a past trip, a journal entry, two review items), at 1280px dark and light,
  plus 390px mobile for the dark theme. Screenshots: `shots/<page>-dark.png`.
- Per page, a script measured fonts, control heights, emoji, primary buttons, focus-ring
  rules and horizontal overflow (`audit.py` → `audit.json`).
- **I looked at 10 pages by eye** (home dark+light, review queue, trips list, trip detail
  desktop+mobile, capture, login, packing profile, rules admin, integrations). The other
  rows are judged from the measurements and the shared templates, and are marked
  *metrics only*.
- **Not covered:** HTMX fragments and modals (trip edit, activity form, currency
  calculator, derived locations, finding editor), the Discovery and Packing tabs with real
  findings, `/setup`, `/reset/<token>`, `/oauth/authorize`, and the registration-open
  state. `/review/item/<id>` and `/trips/new` are fragments: loaded directly they render
  unstyled, which is expected.
- **"Confirmed" needs you.** I can check pages against the written system; only the design
  owner can sign off a look. Every row has an owner column left open.

Verdicts rate each page's own structure and components; G1–G5 below apply to every page, so no page is fully on-system until those land. **Aligned** = follows the system. **Partial** = right structure, wrong tokens or
details. **Off** = conflicts with the system.

## Cross-cutting findings (apply to every page)

| # | Finding | Evidence | Fix |
|---|---|---|---|
| G1 | **Design tokens are not loaded.** The app runs on its own palette in `app/review/static/theme.css` (comment: "the ONE source of truth (D-52)"). `design/tokens/tokens.css` is never linked. | 0 `--rd-*` variables on any page | YONDER-202. Needs a spec decision, because D-52 names theme.css as the source of truth |
| G2 | **Palette drift.** Dark ground `#0D1117` vs design `bg #0A1624`; surface `#161B22` vs `#0F2135`; border `#262D36` vs `#22405F`; accent `#6B93FF` vs `#7FB2FF`; light accent `#2B5CD9` vs `#1F57B0`. The app's greys are GitHub-dark neutrals; the design's are Night blues. | `theme.css` vs `tokens.css` | G1 |
| G3 | **No brand type.** Every page's body and heading font is `system-ui`. No Outfit (headings), Figtree (UI) or JetBrains Mono (codes, refs, times); mono is the system stack. | computed `font-family` on all 31 styled pages | G1 |
| G4 | **Radius and elevation.** Controls and cards use 3px radius; the system says 10 (controls) and 16 (cards). | `--r:3px` | G1 |
| G5 | **Touch targets under 44px on every page.** Median small control is 16–40px tall (typically 25–27px); the system says 44 on web. | `audit.json` `small`, `smallMed` | Component pass, after G1 |
| G6 | **Navigation is a top bar, not the 248px sidebar** the web spec describes (lockup on top, active row with a Beacon inset bar). Settings does have a sidebar. | every page | Layout pass |
| G7 | **Emoji used as icons** (forbidden by README §8): Home "Go to" cards 📥🗂🧭📓⚙, Review "Log approved" 📤. | home, review | Replace with the outline icon set |
| G8 | **Blueprint corner crosses** (owner decision 2026-10-02: not in `design/`, so drop them) (`+` at card corners) on trip detail, packing and the other profile pages are not in the design system. | trip detail, packing profile | Drop the `.blueprint` corner markup and CSS |
| G9 | **Admin (SQLAdmin) tables look unfinished in dark** (owner decision 2026-10-02: admin pages get the theme). White header row, white checkboxes and a white Search button sit on a dark table; "Search" text is low contrast. | `shots/admin-rules-dark.png` | Admin theme pass (`admin-theme.css`) |
| G10 | **Signature element missing.** The Web board's Next-up card (Runway overline, mono IATA codes, dashed route line) has no counterpart: Home shows a text row "In progress · Japan, October". | home | Layout pass |
| G11 | **Primary action count.** The system wants one Beacon primary per screen. Account has 3, capture 2, past-trip detail 2. | `audit.json` `primary` | Per page, below |

**What already matches:** dark default with light following the OS (both render, no
overflow); the logo mark and wordmark are the design files; favicons, apple-touch icon and
manifest are served from `design/icons`; status capsules (`pill`, "Needs you", "In progress")
follow the 14%-alpha-fill pattern; hairline-border cards instead of heavy shadows; Beacon-style
single accent; no horizontal scroll at 390px on any page; focus-visible rules exist on every
styled page.

## Page by page

| Page | Route | Seen | Verdict | Notes | Owner |
|---|---|---|---|---|---|
| Sign in | `/login` | yes | Partial | Lockup, single accent, status-free: good. 3 controls under 44px; system-ui type; the inputs are 3px-radius. Should use the stacked lockup per README (splash/login) | ☐ |
| Register | `/register` | metrics only | Partial | Same card as sign in (redirects to login while registration is closed) | ☐ |
| Forgot password | `/forgot` | metrics only | Partial | Same card | ☐ |
| Home | `/` | yes (dark, light) | Partial | Good: status capsule, hairline cards, mono numerals. Off: emoji icons (G7), no Next-up card (G10), no sidebar (G6), pipeline and capture tiles show many zeros with equal weight, the 5 Go-to cards duplicate the top nav. | ☐ |
| Capture | `/capture` | yes | Partial | Clear two-column task layout; 2 primaries (bookmarklet button and the paste form's submit); 17 of 18 controls under 44px; the form labels are right-aligned small text with weak contrast. Radios sit in a white-bordered box that clashes with dark. | ☐ |
| Review queue | `/review` | yes | Partial | Good: counts header, Runway-style "Needs you" capsule, left rail. Off: 📤 emoji (G7), small controls, rail rows under 56px | ☐ |
| Review item | `/review/item/<id>` | metrics only (fragment) | n/a | HTMX detail panel; review inside the queue page | ☐ |
| Home profiles | `/review/home` | metrics only | Partial | 14 controls under 44px; uses the settings sidebar correctly | ☐ |
| Journal | `/journal` | metrics only | Partial | Compose form and grouped list fit the card pattern; new trip select now sorted by date. 9 of 10 controls small | ☐ |
| Trips list | `/trips` | yes | Partial | Clean table and Upcoming/Past segmented tabs. Should be cards with codes, dates and a progress bar per the Web board (upcoming-trips grid). Delete sits next to Edit with no destructive styling | ☐ |
| Trip detail | `/trips/<id>` | yes (desktop, mobile) | Partial | Good: tabs, collapsible timeline, new Advanced menu. Off: crosses (G8), item chips are outline boxes not Night 700 pills with icons, header actions wrap awkwardly at 390px, "Expand all/Collapse all" appear twice. Past trips correctly drop run buttons | ☐ |
| Trip AdventureLog sync | `/trips/<id>/adventurelog` | metrics only | Partial | Advanced-only page; low priority | ☐ |
| Settings hub | `/settings` | metrics only | Aligned (structure) | Card grid with descriptions; sidebar shared | ☐ |
| Integrations | `/settings/integrations` | yes | Aligned (structure) | New: capsules, hairline cards, sidebar. Per-user setup will extend it | ☐ |
| Travel profile | `/trips/settings/travel-profile` | metrics only | Partial | Form on the settings shell; 7 small controls | ☐ |
| Packing profile | `/trips/settings/packing-profile` | yes | Partial | Long checkbox grid; 94 controls under 44px (checkboxes and labels); crosses (G8); native `<select>` chrome clashes with dark | ☐ |
| Discovery profile | `/trips/settings/discovery-profile` | metrics only | Partial | 37 small controls, same pattern as packing | ☐ |
| Account | `/account` | metrics only | Partial | 3 primary buttons on one page; demote two | ☐ |
| Sharing | `/account/sharing` | metrics only | Partial | Small, single form | ☐ |
| Traveller claim | `/account/traveller` | metrics only | n/a | Redirected to `/account` for this user | ☐ |
| Users | `/users` | metrics only | Partial | Admin table, 31 small controls | ☐ |
| About | `/about` | metrics only | Partial | Text page | ☐ |
| Admin home | `/admin/` | metrics only | Off | SQLAdmin shell (G9) | ☐ |
| Travellers | `/admin/traveler-identity/list` | metrics only | Off | G9 | ☐ |
| Rules | `/admin/rule-setting/list` | yes | Off | G9: white header and checkboxes on dark, low-contrast search | ☐ |
| Source rules | `/admin/source-rule/list` | metrics only | Off | G9 | ☐ |
| API keys | `/admin/api-key/list` | metrics only | Off | G9 | ☐ |
| Packing catalog | `/admin/packing-catalog-item/list` | metrics only | Off | G9, 89 small controls | ☐ |
| Bags | `/admin/bag/list` | metrics only | Off | G9 | ☐ |
| Phrasebook | `/admin/phrasebook-phrase/list` | metrics only | Off | G9, 110 small controls | ☐ |
| Regional languages | `/admin/regional-language/list` | metrics only | Off | G9 | ☐ |

## Suggested order of work (each its own PR, under YONDER-202)

1. **Tokens** (G1, G2, G4): link `tokens.css`, alias the existing `--ground/--ink/--accent`
   variables to `--rd-*`, one decision record superseding D-52. Biggest visible change for
   the least markup.
2. **Type** (G3): self-host Outfit, Figtree and JetBrains Mono (the app avoids third-party
   CDNs); headings and codes first.
3. **Components** (G5, G7, G9, G11): 44px controls, icon set instead of emoji, one primary
   per page, admin theme pass.
4. **Layouts** (G6, G10): sidebar shell, Next-up card on Home, trip cards on the list.
5. **Decided by the owner (2026-10-02):** the blueprint crosses go (G8); admin pages get the full
   theme (G9). **Still open:** dense data screens (rules, packing, catalog) may justify a 32px
   "small" control, which the README allows.

## Completion check (2026-10-02, merged `main` 79662a8)

**Verdict: not complete.** Foundations are in and verified on every page; the Web board's
layouts and several component specs are only partly applied; no page has owner sign-off.

Method: the same 33 pages re-rendered (dark 1280, light 1280, dark 390) with a measuring
script (`audit2.py`), plus static greps of the CSS. I re-viewed home, trip detail (light),
journal and admin rules by eye; other pages are metrics only. Fragments, modals, Discovery
and Packing tabs with real findings, `/setup`, `/reset`, `/oauth/authorize` are still
uncovered.

### Done and measured
- G1/G2/G4 tokens: `tokens.css` loaded; 0 hard-coded hex in `theme.css`, `trips.css`,
  `admin-theme.css`, `base.html`.
- G3 type: Outfit, Figtree and JetBrains Mono self-hosted and applied.
- G6 sidebar (248px at ≥1024px), G7 no emoji, G8 crosses gone, G9 admin themed (dark rules
  table now readable), G10 Next-up card on Home. No horizontal overflow at 390px.

### Remaining gaps
| Area | Gap |
|---|---|
| Home | Layout still differs from the board: no greeting/search header, Capture is not the single primary, no review-queue panel or upcoming-trips grid. Old stat tiles and the "Go to" cards (duplicating the sidebar) remain. |
| Sidebar | No review-count badge. |
| Trips | Cards lack the packing progress bar. Trip detail item chips are outline boxes, not filled pills with icons. |
| Status capsule | `.pill` has the 14% fill but not the 6px dot. |
| Controls | 57 controls under 32px: the `◐` theme toggle (28px, on ~30 pages), native selects, `<summary>` disclosures, trip `yn-tab` buttons. 44px is not met on dense screens; the 32px "small" decision is still open. |
| Contrast | 17 failures dark, 8 light: admin `text-muted` (3.63 dark), `cap-tab-st` (4.36), light `bp-field-label` (~3.37), light Delete button. |
| Tokens | Residual px radii (theme 2, trips 16, base 5) and font-size under 12px (theme 5, trips 13, base 32). |
| Admin | Headings are not Outfit. |
| Login | Uses the horizontal lockup, not the stacked one. |
| Components | No toggle-switch. The board's "Auto-commit trusted senders" example conflicts with D-31 and must not be built. |
| Sign-off | Every owner box above is still ☐. |

### Not design, but open from the capture work
Email forward (D-162, YONDER-199) and the iOS share-extension notification (D-163,
YONDER-54) are unbuilt. Auto-process covers the capture page only. The narrowed Gmail
add-on scope and the bookmarklet hand-off are untested against real Gmail and browsers.

### Implementation pass (YONDER-202, D-165)

Re-audited with the same script after the change (33 pages × dark/light 1280 + dark 390):

| Measure | Before | After |
|---|---|---|
| Controls under 32px | 171 | 0 |
| Contrast failures | 42 | 0 |
| Overflow / emoji | 0 / 0 | 0 / 0 |

Applied: the 32px floor (D-165), the 6px capsule dot, the Home layout (greeting, search,
Capture as the one primary, Next-up card, review-queue panel, upcoming-trips grid; stat
tiles and "Go to" cards removed), the sidebar Review badge, packing progress on trip cards
(the viewer's own list, D-123), filled trip-item chips with icons, and the contrast fixes.

Still open: stacked login lockup, residual px radii and sub-12px font sizes, Outfit on admin
headings, per-page owner sign-off, and a toggle switch (not built: the board's
"Auto-commit trusted senders" example conflicts with D-31).
