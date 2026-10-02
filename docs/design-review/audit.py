import json, re, sys
from playwright.sync_api import sync_playwright
R="/tmp/claude-0/-home-user-rundere-server/5d6f4b03-a7fe-54d7-b1c7-ae1674639847/scratchpad/rev"
B="http://localhost:8766"
PAGES=[("login","/login",False),("register","/register",False),("forgot","/forgot",False),
 ("home","/",True),("capture","/capture",True),("review-queue","/review",True),("review-item","/review/item/ITEM",True),
 ("review-home-profiles","/review/home",True),("journal","/journal",True),
 ("trips-list","/trips",True),("trip-detail-current","/trips/tcur",True),("trip-detail-past","/trips/tpast",True),
 ("trip-new","/trips/new",True),("trip-al-sync","/trips/tcur/adventurelog",True),
 ("settings-hub","/settings",True),("settings-integrations","/settings/integrations",True),
 ("settings-travel","/trips/settings/travel-profile",True),("settings-packing","/trips/settings/packing-profile",True),
 ("settings-discovery","/trips/settings/discovery-profile",True),
 ("account","/account",True),("account-sharing","/account/sharing",True),("account-traveller","/account/traveller",True),
 ("users","/users",True),("about","/about",True),
 ("admin-home","/admin/",True),("admin-traveler","/admin/traveler-identity/list",True),("admin-rules","/admin/rule-setting/list",True),
 ("admin-source-rules","/admin/source-rule/list",True),("admin-api-keys","/admin/api-key/list",True),
 ("admin-packing-catalog","/admin/packing-catalog-item/list",True),("admin-bags","/admin/bag/list",True),
 ("admin-phrasebook","/admin/phrasebook-phrase/list",True),("admin-regional-lang","/admin/regional-language/list",True)]
JS = r"""() => {
 const vis = e => { const r=e.getBoundingClientRect(), cs=getComputedStyle(e); return r.width>0&&r.height>0&&cs.visibility!=='hidden'&&cs.display!=='none'; };
 const fam = e => getComputedStyle(e).fontFamily.split(',')[0].replace(/["']/g,'').trim();
 const h = document.querySelector('h1,h2,.pagetitle');
 const ctl = [...document.querySelectorAll('a.btn,button,input:not([type=hidden]),select,textarea,summary,.act')].filter(vis);
 const small = ctl.filter(e=>e.getBoundingClientRect().height<44);
 const txt = document.body.innerText||'';
 const emoji = (txt.match(/\p{Extended_Pictographic}/gu)||[]).slice(0,8);
 const prim = [...document.querySelectorAll('.btn-primary,.act.primary,button[type=submit].primary')].filter(vis).length;
 const root = getComputedStyle(document.documentElement);
 const hasRd = [...document.styleSheets].some(s=>{try{return [...s.cssRules].some(r=>r.cssText.includes('--rd-'))}catch(e){return false}});
 const imgs = [...document.querySelectorAll('img[src*=logo],img[src*=brand],.rd-mark,.rd-wordmark')].length;
 return {title:document.title, bodyFont:fam(document.body), headFont:h?fam(h):null, bg:getComputedStyle(document.body).backgroundColor,
   ctl:ctl.length, small:small.length, smallMed:Math.round(small.map(e=>e.getBoundingClientRect().height).sort((a,b)=>a-b)[Math.floor(small.length/2)]||0),
   primary:prim, emoji, hasRdTokens:hasRd, brandMarks:imgs, overflowX:document.documentElement.scrollWidth>window.innerWidth+1,
   focusRingDefined: [...document.styleSheets].some(s=>{try{return [...s.cssRules].some(r=>r.selectorText&&r.selectorText.includes(':focus-visible'))}catch(e){return false}})};
}"""
out={}
with sync_playwright() as p:
    b=p.chromium.launch(executable_path="/opt/pw-browsers/chromium")
    for scheme in ("dark","light"):
        ctx=b.new_context(viewport={"width":1280,"height":800},color_scheme=scheme)
        pg=ctx.new_page()
        pg.goto(B+"/login"); 
        for name,path,auth in PAGES:
            if auth and "demo-admin" not in (pg.evaluate("document.cookie") or "") and not pg.url.endswith("x"):
                pass
        # log in once per context
        pg.goto(B+"/login")
        pg.fill("input[name=username]","demo-admin"); pg.fill("input[name=password]","demo-pass-123"); pg.click("button[type=submit]")
        pg.wait_for_load_state()
        # unauth pages need a logged-out context
        anon=b.new_context(viewport={"width":1280,"height":800},color_scheme=scheme).new_page()
        import sqlite3
        item=sqlite3.connect("/tmp/rd-rev.db").execute("select id from travel_items limit 1").fetchone()[0]
        for name,path,auth in PAGES:
            page = pg if auth else anon
            try:
                r=page.goto(B+path.replace("ITEM",item)); page.wait_for_timeout(250)
                m=page.evaluate(JS); m["status"]=r.status if r else None; m["url"]=page.url.replace(B,"")
                page.screenshot(path=f"{R}/{name}-{scheme}.png",full_page=False)
                if scheme=="dark":
                    page.set_viewport_size({"width":390,"height":844}); page.wait_for_timeout(150)
                    m["mobileOverflowX"]=page.evaluate("document.documentElement.scrollWidth>window.innerWidth+1")
                    page.screenshot(path=f"{R}/{name}-mobile.png"); page.set_viewport_size({"width":1280,"height":800})
            except Exception as e:
                m={"error":str(e)[:120]}
            out.setdefault(name,{})[scheme]=m
    b.close()
json.dump(out,open(R+"/audit.json","w"),indent=1)
print("pages",len(out))
