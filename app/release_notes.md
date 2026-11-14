# Run·dere release notes

<!--
What changed in each version, for the people using Run·dere (SPEC-8 §14, D-234).
The web shows these after an upgrade, the About page lists them all, and the apps
read them from GET /api/v1/release-notes.

How to write a section (the release PR does this; RELEASING.md):
- `## X.Y.Z — YYYY-MM-DD`, newest first. A version that isn't out yet has no date.
- `### For everyone`: what people will notice, in plain words. Not PR titles.
- `### For admins` (only when needed): setup steps, settings, one-time commands.
  Only admins see these.
- One short item per line, starting with "- ". **Bold**, `code` and [links](https://…)
  work; nothing else is rendered.
- A patch with nothing to say: "- Fixes and small improvements."

Run·dere was called Yonder until 4.4.0, so older notes use that name.
-->

## 5.0.0

### For everyone
- **Packing starts from what you own.** A short questionnaire under Settings → Packing profile sets up your inventory, your bags and your own packing templates. Lists are built from your inventory, and anything you don't own shows as a suggestion you can add or mark not needed. Everyone retakes the questionnaire once.
- **Pick a trip's bags and see what fits.** Choose bags for a trip and Run·dere estimates how full each one is, in litres, and warns when one is over.
- **Cuisines you like and don't like.** The Discovery profile has a cuisine list where each one is Like, Neutral or Don't like. Likes now steer the food research, not only dislikes.
- **Choose what the trip PDF carries.** Export PDF asks first: times, addresses, maps and booking details, plus your own packing list and Discover research, topic by topic. Leave out times and addresses when you hand it to someone else.
- **The PDF prints addresses** for places found on the map and for airports, labelled "from the map".
- **Search on the Places map.** A search made on the Map tab narrows the pins and zooms to them instead of leaving the map.
- **The Places search helps you write it.** As you type, it suggests the kinds after `kind:`, your own lists after `list:`, and your cities and countries after `city:` and `country:`, with counts. Chips under the box filter by kind, and list names with spaces work: `list:"Date night"`.
- **Lists in the apps.** The iPhone and Android apps can show your Places lists, make and edit them, and put a place in the right list as you save it.
- **What's new.** After an upgrade, Run·dere shows you once what changed, like this. The About page lists every version.
- **Export your data.** Account → Export my data builds a zip of your own data as JSON files, with the files you uploaded: trips, places, journal, profiles. It is kept for 7 days, and downloading it asks you to confirm it's you. Passwords, tokens and other people's data are never in it.
- **Typing in a capture by hand.** When a capture can't be read automatically, review shows the original email, page or file next to the form so you can fill it in.

### For admins
- **AI is now off unless you turn it on.** Set `TX_AI_ENABLED=true` to keep reading booking emails and running Discover's research with AI. Point `TX_AI_URL` at Ollama or any OpenAI-compatible server (LiteLLM, vLLM, a cloud provider). The old `TX_OLLAMA_URL` still works in 5.0, with a warning.
- **Settings → AI** shows what AI is used for, any configuration problems, a connection test and a log of calls (never their content).
- **Tell everyone to retake the packing questionnaire** (Settings → Packing profile). Packing lists can't be generated until they do.
- **Database migrations run on start.** Back up first as usual.
- **Keep the export folder out of backups.** People's archives wait in `TX_EXPORT_DIR` (default: `exports/` in the artifacts folder) for 7 days. Each is a person's whole record with no password; see the self-hosting guide.
- **Nothing to run by hand for addresses:** about two minutes after the first start, the server fills in the addresses of older map pins once by itself.
- **A stricter Content Security Policy:** pages now load only the server's own scripts. A reverse proxy that injects scripts into pages will find them blocked.

## 4.13.6 — 2026-11-07

### For everyone
- Groundwork for the Android app. Nothing changes on the web.

### For admins
- The server's API contract is now checked against the Android app's, as it already was for iOS.

## 4.13.5 — 2026-11-05

### For everyone
- A saved place whose address is a house number on a street now gets its map pin.

## 4.13.4 — 2026-11-05

### For everyone
- **Times show where they happened.** Journal entries show the time in the zone they were written in, and times keep their UTC offset everywhere.
- Groundwork for signing in from the Android app.

## 4.13.3 — 2026-10-31

### For everyone
- **Notebook.** Journal and Places sit together under a Notebook label in the sidebar.
- The demo server has sample saved places.

## 4.13.2 — 2026-10-30

### For everyone
- Fixes for pushing a trip to AdventureLog, and for splitting a road trip into legs.

## 4.13.1 — 2026-10-30

### For everyone
- Fixes found at launch: the traveller roster, stays with dates but no times, renaming yourself, and journal titles.

## 4.13.0 — 2026-10-29

### For everyone
- **The Places map.** A Map view on the Places page shows your saved places, in clusters that open into pins as you zoom in.
- **Pins from addresses.** A saved place with an address but no pin gets one from its address.

## 4.12.13 — 2026-10-26

### For everyone
- A saved place's address saves as you change it, and can place its pin.

## 4.12.12 — 2026-10-26

### For everyone
- **The Places page.** One search box over all your saved places, with chips like `must`, `kind:restaurant` and `city:Lisbon`, and four views: Unsorted, Atlas, Lists and All. Each place opens in a panel with everything about it.
- **Support access is yours to give.** Admins no longer see your data by default. Under Settings → Account, "Let admins look" gives them read-only access for 24 hours, 7 days or until you turn it off, and you see each day they looked.
- **Your journal is closed to admins**, always.
- The Capture page is now the **Capture Hub**.

### For admins
- Admins lose their standing access to other people's data. Anyone who shared with admins keeps a read-only grant and sees a one-time note about the change.
- Admins can no longer set someone else's password or email; reset links go to the person.
- If you skipped it at 4.12.11, run `python -m app.cli places-backfill` once.

## 4.12.11 — 2026-10-25

### For everyone
- **New accounts are private.** Admins see only the trips you put them on until you opt in under Settings → Account.
- **Local accounts need an email**, for password resets. You'll be asked for one at your next visit if yours has none.
- **Saved places become a library.** A place stays saved after you add it to a trip, and can go on any number of trips. Places gain a to-do, *must* or *someday*, and the date you went.
- Places shared from a Maps app reach Saved Places reliably.

### For admins
- Run `python -m app.cli places-backfill` once after upgrading.

## 4.12.10 — 2026-10-23

### For everyone
- **Forwarded emails are picked up again.** Since 4.11.0 some mail servers' messages were skipped.
- **A Maps link no longer wipes the activity form.** It fills only what's empty and never changes a type you chose.
- **Trip items can wait for a date.** Directions without a date are kept, wait in the Inbox, and show on the trip under "Date not set".
- Readable time zones, a country picker for nationality, and clearer home and packing profiles.

### For admins
- The CLI-made API token is retired: switch scripts to a personal API token (Settings → API keys).
- New image name: `ghcr.io/rundere/rundere-server`.
- The server checks its data folders are writable before it starts, and says how to fix them.
- Settings → Server mail can read the inbox now, and an hourly alert fires if forwarded mail is still waiting.

## 4.12.9 — 2026-10-19

### For admins
- The image runs as your own user id with a writable home folder.

## 4.12.8 — 2026-10-19

### For admins
- Security review: secrets and configuration are checked at start, and the image is unprivileged and production-only.

## 4.12.7 — 2026-10-19

### For everyone
- Security review: tighter sign-in pages, safer handling of links and pasted content, and a strict browser security policy.

## 4.12.6 — 2026-10-19

### For everyone
- **Sign-in is required by default.**
- Security fixes, including one where a visitor who wasn't signed in could see trip names on the capture page.

### For admins
- Sign-in is now on unless you turn it off with `TX_AUTH_REQUIRED=false`.

## 4.12.5 — 2026-10-15

### For admins
- Security: the PDF library is upgraded to fix eight known vulnerabilities.

## 4.12.4 — 2026-10-15

### For everyone
- **A public About page**, with how to get the app, linked from every signed-out page.

## 4.12.3 — 2026-10-15

### For admins
- A demo server keeps no request log.

## 4.12.2 — 2026-10-15

### For everyone
- The timeline's line no longer runs through an opened item.

## 4.12.1 — 2026-10-15

### For everyone
- **Dark by default.** The web app starts dark when you haven't picked a theme.

### For admins
- Demo servers hide the controls they would refuse.

## 4.12.0 — 2026-10-14

### For everyone
- **Roles on a trip.** You see whether you own a trip, travel on it or only view it, and a view-only person sees the trip without edit controls.
- **A new timeline**, with the next thing lit up.
- **The Inbox.** Review becomes the Inbox, and Capture is reachable from every page.
- **Refine Discovery** from its own tab.
- The Apple Watch can show what's next.

### For admins
- A read-only demo server mode, with sample trips that roll forward each night.

## 4.11.1 — 2026-10-14

### For everyone
- Edit a trip from the pencil on its card; delete only from inside the trip.
- The Capture page shows your connected iPhones and AI assistants.

### For admins
- Saved notification targets can be edited.

## 4.11.0 — 2026-10-14

### For everyone
- **Capture files.** Drop in a PDF, a calendar file (.ics) or an email file (.eml).

### For admins
- Admin notifications when a service fails, starting with Pushover.
- A daily sweep removes old files, raw captures and mailbox copies after 12 months.
- The email inbox only touches mail sent to a forwarding (plus) address.

## 4.10.0 — 2026-10-13

### For everyone
- **Exchange rates are fixed per trip and date**, so costs don't silently change.
- Start Discovery from the iPhone app and watch its progress.

### For admins
- Emails and attachments are scanned (ClamAV and PDF checks) before they're read.

## 4.9.4 — 2026-10-13

### For everyone
- Signing in from the iPhone app no longer needs any setup.

## 4.9.3 — 2026-10-13

### For everyone
- **Taxi** is a way to get around, and you can choose an item's kind in review.

## 4.9.2 — 2026-10-13

### For everyone
- A taxi to the airport lands on the right trip, and the trip is pre-selected.

### For admins
- Settings → Server mail, with verified TLS for sending mail.

## 4.9.1 — 2026-10-13

### For admins
- The email inbox verifies the mail server's certificate.

## 4.9.0 — 2026-10-13

### For everyone
- **Forward booking emails.** Each person gets a forwarding address, and mail sent there is read within moments.
- Better weather in Discover.

## 4.8.2 — 2026-10-12

### For everyone
- Linking a capture to a trip, or changing a trip's dates, re-checks captures waiting in the Inbox.
- Costs opens on the plan before a trip starts.

## 4.8.1 — 2026-10-12

### For everyone
- The costs dialog closes after you save, and Plan and Actual both show on the web.

## 4.8.0 — 2026-10-12

### For everyone
- **A Costs tab** on every trip.
- **Your own connections.** AirTrail and AdventureLog are connected per person under Settings → Connections, not for the whole server.

## 4.7.2 — 2026-10-12

### For everyone
- **Trip costs.** Plan a budget, record what you spent, and settle up between travellers.

## 4.7.1 — 2026-10-08

### For everyone
- "Next up" shows the soonest moment, not a multi-day item.

## 4.7.0 — 2026-10-04

### For everyone
- **Saved places.** Share or paste a Google or Apple Maps place link to save it for later. AI assistants can save places too.

## 4.6.1 — 2026-10-02

### For everyone
- Fixes for the new design: stale styles, trip tab widths and capture tabs.

## 4.6.0 — 2026-10-02

### For everyone
- **A new look.** The design system arrives: new colours and fonts, a sidebar, trip cards and a Next-up card on Home.
- **A rebuilt Capture page**, with a guide for each way to capture and a working bookmarklet.
- No Discovery on trips that are already over.

### For admins
- AirTrail and AdventureLog writes are admin-only for now.

## 4.5.2 — 2026-09-28

### For admins
- The server stays up under a flood of requests, and rows with no owner are closed off.

## 4.5.1 — 2026-09-27

### For everyone
- Currency fixes in Discover, and a wider Discovery page.
- The iPhone app shows trip maps and can capture straight into a trip.

## 4.5.0 — 2026-09-27

### For everyone
- **Discovery 2.0**, with recorded pronunciation for phrasebook phrases.

## 4.4.0 — 2026-09-27

### For everyone
- **Yonder is now Run·dere**: a new name, logo and design.

## 4.3.3 — 2026-09-27

### For admins
- Single sign-on fixes: account lookup, linking a traveller, and per-host sign-in.

## 4.3.2 — 2026-09-26

### For everyone
- The trip PDF has its map on page one, with flights in an appendix.

## 4.3.1 — 2026-09-26

### For everyone
- More activities are found on the map, and map tiles are kept between restarts.

## 4.3.0 — 2026-09-26

### For everyone
- **Road trips and maps.** Activities get map pins and links, drives get routes, and the trip PDF has maps.

## 4.2.1 — 2026-09-26

### For everyone
- **The iPhone and iPad app**: edit trips, write in your journal and review captures.
- Maps directions no longer get stuck in review.

## 4.2.0 — 2026-09-26

### For everyone
- **Connect Claude and other AI assistants** to your trips, with sign-in through the server.
- Share trips, and act on captures from the app.

## 4.1.2 — 2026-09-25

### For everyone
- "Log approved" logs only your own approved captures.

## 4.1.1 — 2026-09-25

### For everyone
- The same Settings sidebar on every settings page.

## 4.1.0 — 2026-09-25

### For everyone
- **Settings** replaces the Administer links.
- **Your own email and password**, changed by you.
- **Tag trip items with who they're for.**
- **Profiles are per person**: home, packing and Discovery profiles belong to each traveller.

### For admins
- Admins can manage profiles on someone's behalf.

## 4.0.0 — 2026-09-24

### For everyone
- **Accounts.** Sign-in, and each person sees their own trips.
- **The iPhone app**, an API and an MCP server for AI assistants.
- **The trip PDF**, a printable itinerary.
- **The travel journal.**
- **Apple Maps directions**, and car, transit and walking routes.
- Editable exchange rates, based on your home currency.

## 3.9.2 — 2026-08-26

### For everyone
- **Bag inventory.** Keep a list of your real bags and their sizes, and assign packing items to them.

## 3.9.1 — 2026-08-26

### For everyone
- Packing rows are readable on a phone again.

## 3.9.0 — 2026-08-25

### For everyone
- **Edit packing lines.** Change quantities and names, choose backpack, carry-on or checked, and add your own items. Your edits survive a re-generate.

## 3.8.1 — 2026-08-24

### For everyone
- A dashboard on the home page.

## 3.8.0 — 2026-08-22

### For everyone
- **Two-way AdventureLog sync.** Push trip items and findings to AdventureLog, and pull its transport and lodging into your timeline.

## 3.7.0 — 2026-08-22

### For everyone
- Phrasebooks for national languages that weren't covered, and local phrases that don't vanish when a search comes back thin.

## 3.6.3 — 2026-08-22

### For everyone
- Languages that are widely spoken but not official (French in Morocco) show with their phrasebooks, and Arabic has a phrasebook.

## 3.6.2 — 2026-08-22

### For everyone
- Discovery retries a slow search, and smaller towns still get things to do.

## 3.6.1 — 2026-08-22

### For everyone
- Discovery keeps its findings when a re-run comes back empty.

## 3.6.0 — 2026-08-22

### For admins
- Phrasebooks and the regional language map can be edited in the admin.

## 3.5.1 — 2026-08-22

### For everyone
- Big festivals during your trip show up in Events again.

## 3.5.0 — 2026-08-22

### For everyone
- **Regional languages**: Kannada in Bengaluru, Catalan in Barcelona, Welsh in Cardiff. Hindi has a phrasebook.

## 3.4.1 — 2026-08-22

### For everyone
- Language fixes: countries with no official language, and your own language picks survive a re-run.

## 3.4.0 — 2026-08-22

### For everyone
- **Local phrases**: how people in a city actually greet and talk, with sources.

## 3.3.0 — 2026-08-21

### For everyone
- **Safety and getting around** in Discovery, and map links on the timeline.

## 3.2.4 — 2026-08-21

### For admins
- Adding a packing item in the admin works again.

## 3.2.3 — 2026-08-21

### For everyone
- The trip page gets collapsible sections, a sticky jump bar and a filter.

## 3.2.2 — 2026-08-20

### For everyone
- Events stick to your trip's dates, and food research includes breweries and distilleries.

## 3.2.1 — 2026-08-20

### For everyone
- Events list real named events and holidays; food always includes drinks; packing knows the climate and says why each item is there.

## 3.2.0 — 2026-08-19

### For everyone
- **Discovery researches with AI**: food and drink, and events and activities, from a Discovery profile of what you like. It runs in the background.

## 3.1.3 — 2026-08-17

### For everyone
- Weather shows for trips more than two weeks out, blending the forecast with a seasonal estimate.

## 3.1.2 — 2026-08-16

### For everyone
- The packing profile has its own page.

## 3.1.1 — 2026-08-16

### For everyone
- The packing profile is a proper form.

## 3.1.0 — 2026-08-16

### For everyone
- **Packing plans.** A packing list built from your trip's places, length, weather and activities, with laundry tasks instead of 14 of everything.

## 3.0.2 — 2026-08-15

### For everyone
- Collapsible timeline days and Discovery topics, Re-run per topic, and a language picker.

## 3.0.1 — 2026-08-14

### For everyone
- Hand-entered places are found on the map, and you can leave a place out of Discovery.

## 3.0.0 — 2026-08-14

### For everyone
- **Discovery.** Yonder researches a trip for you: plugs, weather, currency (with a calculator) and languages, held for you to review.

## 2.3.1 — 2026-08-13

### For everyone
- Push to AdventureLog sends the trip's flights, lodging and notes, with coordinates.

## 2.3.0 — 2026-08-11

### For everyone
- A new journey creates a Yonder trip as well as an AdventureLog one.

## 2.2.2 — 2026-08-11

### For everyone
- The two legs of a round trip land on the same trip.

## 2.2.1 — 2026-08-10

### For everyone
- Car rentals and ferries reach AdventureLog.

## 2.2.0 — 2026-08-10

### For everyone
- **Approved captures land on your Yonder trip's timeline.**

## 2.1.3 — 2026-08-10

### For everyone
- Review uses the whole width of a wide screen, and a capture links itself when only one trip fits.

## 2.1.2 — 2026-08-10

### For everyone
- **Pick the trip in review**, for AdventureLog and for Yonder.

## 2.1.1 — 2026-08-10

### For everyone
- A flight whose trip can't be worked out no longer gets stuck in review.

## 2.1.0 — 2026-08-09

### For everyone
- **Push a trip to AdventureLog**, and open it there from the trip page.

## 2.0.1 — 2026-08-09

### For everyone
- Fixes for home profiles and the travellers list.

## 2.0.0 — 2026-08-09

### For everyone
- **Trips in Yonder.** Trips and their timeline live in Yonder itself.

## 1.2.0 — 2026-08-09

### For everyone
- **Capture from the web.** A capture page and a bookmarklet turn booking pages, emails, calendar invites, PDFs and Google Maps directions into trip records for AdventureLog and AirTrail.
- Correct time zones for trains, buses and ferries.
- An About page.
