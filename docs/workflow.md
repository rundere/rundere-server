# Daily workflow

The day-to-day loop once Run·dere is running. [howto.md](howto.md) walks through each screen in more detail.

## The loop

1. **Start at Home** (`/`): the trip coming up next, what's waiting in the Inbox, and
   quick links into everything else.
2. **Capture** a booking any way that suits you ([capture.md](capture.md)). The raw capture
   is stored straight away; reading it never happens inside the request.
3. **Processing runs on its own** (D-158, `TX_AUTO_PROCESS`, on by default): Run·dere
   extracts the items and matches them to a trip. The capture page shows the result as soon
   as it's ready. **Process new captures** in the Inbox, or
   `docker compose exec rundere python -m app.cli process`, runs it by hand.
4. **Review in the Inbox.** Items are sorted into *Needs you → Waiting for a trip → Ready
   to approve → Approved → Logged*. Open one to check what was read and correct anything.
   A field you change is locked, so a later capture of the same booking can't overwrite it.
   Approving is always your decision (D-31).
5. **Log.** Approved items go onto the Run·dere trip's timeline, and to AirTrail or
   AdventureLog too if you've connected them. A dry-run preview shows exactly what will be
   written first.
6. **Plan the trip** in `/trips`: the timeline and map, **Discover** research (weather,
   currency, plugs, visa, safety, food, language), the packing list, costs and the travel
   journal.

## Alerts when something breaks

Admins can be told when a service stops working: the email inbox, outgoing
mail, someone's AirTrail or AdventureLog connection, the file store folder,
the virus scanner, the daily retention sweep, or the LLM. Set it up in
**Settings → Notifications**: add a Pushover target (an application token
from pushover.net/apps and your user key, both stored encrypted, which needs
`TX_SECRETS_KEY`), pick what it should hear about, and press **Send test**.
Alerts arrive when something starts failing, once a day while it still is,
and when it recovers; one hiccup that fixes itself pages nobody. More target
kinds can be added over time (SPEC-8 D-197).
