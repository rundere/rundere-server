"""YONDER-202 rendered audit (2026-10-14): every page against design/README.md + the canvas boards.
Usage: uv run --with playwright python docs/design-review/audit202.py <out-dir> <lisbon-trip-id> <france-trip-id> <nyc-trip-id> x
Against a server on :8818 seeded with app/demo_seed.py (demo mode off), users jane (admin) and viewer."""
import json, sys
from playwright.sync_api import sync_playwright
SP = sys.argv[1]; B = "http://127.0.0.1:8818"
LIS, FR, NYC, ITEM = sys.argv[2:6]
P = [("login", "/login", "anon"), ("register", "/register", "anon"), ("forgot", "/forgot", "anon"),
     ("home", "/", "jane"), ("capture", "/capture", "jane"),
     ("capture-setup-gmail", "/capture/setup?tab=gmail", "jane"),
     ("capture-setup-ios", "/capture/setup?tab=ios", "jane"),
     ("capture-setup-mcp", "/capture/setup?tab=mcp", "jane"),
     ("inbox", "/inbox", "jane"), ("review-home-profiles", "/review/home", "jane"),
     ("journal", "/journal", "jane"), ("places", "/places", "jane"),
     ("trips", "/trips", "jane"),
     *[(f"trip-{t}", f"/trips/{LIS}#{t}", "jane") for t in
       ("travellers", "timeline", "discovery", "packing", "costs", "journal", "sharing")],
     ("trip-france-discovery", f"/trips/{FR}#discovery", "jane"),
     ("trip-france-packing", f"/trips/{FR}#packing", "jane"),
     ("trip-nyc-costs", f"/trips/{NYC}#costs", "jane"),
     ("trip-viewer", f"/trips/{LIS}", "viewer"),
     ("trip-al-sync", f"/trips/{LIS}/adventurelog", "jane"),
     ("settings", "/settings", "jane"), ("settings-integrations", "/settings/integrations", "jane"),
     ("settings-mail", "/settings/mail", "jane"), ("settings-notifications", "/settings/notifications", "jane"),
     ("settings-travel", "/trips/settings/travel-profile", "jane"),
     ("settings-packing", "/trips/settings/packing-profile", "jane"),
     ("settings-discovery", "/trips/settings/discovery-profile", "jane"),
     ("account", "/account", "jane"), ("account-sharing", "/account/sharing", "jane"),
     ("users", "/users", "jane"), ("about", "/about", "jane"),
     ("admin-home", "/admin/", "jane"), ("admin-travellers", "/admin/traveler-identity/list", "jane"),
     ("admin-rules", "/admin/rule-setting/list", "jane"), ("admin-catalog", "/admin/packing-catalog-item/list", "jane"),
     ("admin-phrasebook", "/admin/phrasebook-phrase/list", "jane")]
# Modals: (name, page, button text that opens it)
M = [("modal-activity", f"/trips/{LIS}", "+ Add activity"), ("modal-trip-edit", f"/trips/{LIS}", "Edit trip"),
     ("modal-expense", f"/trips/{LIS}#costs", "Add expense"), ("modal-plan", f"/trips/{LIS}#costs", "Plan a cost"),
     ("modal-derived", f"/trips/{LIS}", "What Run·dere found")]
JS = open(__file__.replace(".py", ".js")).read()
out = {}
with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome")
    for scheme in ("dark", "light"):
        ctxs = {}
        for who in ("anon", "jane", "viewer"):
            c = b.new_context(viewport={"width": 1280, "height": 800}, color_scheme=scheme)
            pg = c.new_page()
            if who != "anon":
                pg.goto(B + "/login"); pg.fill("input[name=username]", who)
                pg.fill("input[name=password]", "audit-pass-1"); pg.click("button[type=submit]")
                pg.wait_for_load_state()
            ctxs[who] = pg
        def measure(name, pg):
            m = pg.evaluate(JS)
            pg.screenshot(path=f"{SP}/shots/{name}-{scheme}.png")
            if scheme == "dark":
                pg.set_viewport_size({"width": 390, "height": 844}); pg.wait_for_timeout(150)
                m["mobileOverflowX"] = pg.evaluate("document.documentElement.scrollWidth>window.innerWidth+1")
                pg.screenshot(path=f"{SP}/shots/{name}-mobile.png")
                pg.set_viewport_size({"width": 1280, "height": 800})
            return m
        for name, path, who in P:
            pg = ctxs[who]
            try:
                url, _, tab = path.partition("#")
                r = pg.goto(B + url); pg.wait_for_timeout(300)
                if tab:
                    pg.click(f'.yn-tab[data-tab="{tab}"]'); pg.wait_for_timeout(200)
                m = measure(name, pg); m["status"] = r.status; m["url"] = pg.url.replace(B, "")
            except Exception as e:
                m = {"error": str(e)[:160]}
            out.setdefault(name, {})[scheme] = m
        pg = ctxs["jane"]
        for name, path, label in M:
            try:
                url, _, tab = path.partition("#")
                pg.goto(B + url); pg.wait_for_timeout(300)
                if tab:
                    pg.click(f'.yn-tab[data-tab="{tab}"]'); pg.wait_for_timeout(150)
                pg.get_by_text(label, exact=True).first.click(); pg.wait_for_timeout(600)
                m = measure(name, pg)
            except Exception as e:
                m = {"error": str(e)[:160]}
            out.setdefault(name, {})[scheme] = m
    b.close()
json.dump(out, open(SP + "/audit202.json", "w"), indent=1)
print("pages", len(out))
