"""The 5.0 packing profile: questionnaire, inventory and templates (SPEC-5 §6.1–6.5).

- **Packing profile** (`/trips/settings/packing-profile`): the questionnaire
  (§6.2, D-229) and the packing preferences (§6.4) in one form. Saving seeds
  the person's inventory and bags (§6.3), add-only, so re-saving is safe.
- **Inventory** (`…/inventory`): what the person owns, every category shown
  even when empty, each item renamed, re-counted, marked must-have or deleted;
  their bags too (P6).
- **Templates** (`…/templates`): their own named kits, one per type except
  `custom` (§6.5, D-230). Creating one pre-ticks matching inventory.

The questionnaire and preferences live on the traveller's PackingProfile, so
an admin may still set them up for a traveller without an account (D-211).
Inventory, bags and templates belong to an account and are own-only
(scope.own_filter, SPEC-5 §6.6): acting for someone never shows or seeds
them, and every lookup by id 404s for anyone but the owner.
"""
from __future__ import annotations

from datetime import datetime, timezone
from html import escape

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app import scope
from app.db import get_session
from app.models import Bag, InventoryItem, PackingProfile, PackingTemplate, PackingTemplateItem
from app.trips.routes import PACK_CAT_LABELS, PACK_CAT_ORDER, _profile_row, templates
from planning import catalog, inventory

router = APIRouter(prefix="/trips/settings/packing-profile")
BASE = "/trips/settings/packing-profile"

# The questionnaire's wording (SPEC-5 §6.2). The keys are planning.catalog's.
QUESTIONS = [
    ("style", "one", "Which clothing style should your list use?",
     "This picks item names and what's seeded. It's a style, not who you are.",
     [("masc", "Menswear"), ("fem", "Womenswear"), ("neutral", "Generic / mixed")]),
    ("clothing", "many", "What clothing do you already travel with?", "",
     [("basics", "Everyday basics"), ("smart", "Smart casual"),
      ("formal", "Business / formal"), ("outdoor", "Outdoor and active"),
      ("swim", "Swim and beach"), ("cold", "Cold weather"),
      ("modest", "Modest / cover-up")]),
    ("footwear", "one", "Footwear", "",
     [("minimal", "Minimalist: one or two pairs that do everything"),
      ("varied", "Varied: the right shoe for each thing")]),
    ("care", "many", "Personal care you always take", "",
     [("toiletries", "Basic toiletries"), ("skincare", "Skincare"),
      ("hair", "Hair care and styling"), ("shaving", "Shaving"), ("makeup", "Make-up"),
      ("eyewear", "Glasses or contact lenses"), ("sun", "Sun care"),
      ("meds", "Regular medication")]),
    ("gear", "many", "Bags and organisers you own",
     "Say how many of each bag; organisers are a tick.",
     [("daypack", "Daypack"), ("backpack", "Travel backpack"),
      ("carry_on", "Carry-on suitcase"), ("checked", "Checked suitcase"),
      ("personal_item", "Personal item / tote"), ("cubes", "Packing cubes"),
      ("compression", "Compression bags"), ("toiletry_bag", "Toiletry bag")]),
    ("tech", "many", "Tech you travel with", "",
     [("phone", "Phone"), ("laptop", "Laptop"), ("tablet", "Tablet or e-reader"),
      ("camera", "Camera"), ("headphones", "Headphones"),
      ("watch", "Smartwatch or fitness tracker"), ("adapter", "Travel adapter"),
      ("car", "Car charger")]),
]
# The packing preferences (SPEC-5 §6.4): same keys as 4.x where they existed.
PREFERENCES = [
    ("laundry_comfort", "Laundry on trip preference", "none",
     [("none", "No"), ("hand_wash", "Hand wash"), ("machine", "Machine / laundromat")]),
    ("packing_style", "Packing style", "balanced",
     [("one_bag", "One bag only, strict"), ("minimalist", "Minimalist"),
      ("balanced", "Leisurely"), ("prepared", "Well prepared")]),
    ("climate_preference", "On trips my body tends to…", "neutral",
     [("runs_hot", "I run hot"), ("runs_cold", "I run cold"),
      ("neutral", "No preference")]),
]
BAG_KINDS = [("backpack", "Backpack"), ("suitcase", "Suitcase"),
             ("personal_item", "Personal item")]


def _owner(request):
    """(user, user_id) for the signed-in person; both None with auth off."""
    user = scope.user_of(request)
    return user, (user.id if user else None)


def _own(session: Session, model, user):
    return session.query(model).filter(scope.own_filter(model, user))


def _back(path: str = "", anchor: str = "") -> RedirectResponse:
    return RedirectResponse(BASE + path + (f"#{anchor}" if anchor else ""), status_code=303)


def _row_saved(request: Request, path: str, anchor: str, note: str = ""):
    """A row edit's reply. The inventory rows save as you go (htmx posts the
    row on change), so htmx gets the small status beside the row instead of
    a reload that would lose edits in other rows; a plain form post (scripts
    off) still goes back to the page."""
    if request.headers.get("hx-request") == "true":
        return HTMLResponse(f'<span class="yn-saved">{escape(note or "Saved ✓")}</span>')
    return _back(path, anchor)


def _qty(raw, default: int = 1, low: int = 1) -> int:
    try:
        return max(low, min(99, int(raw)))
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- the questionnaire
def _questionnaire_view(saved: dict) -> list[dict]:
    bags = saved.get("bags") or {}
    out = []
    for key, kind, text, help_, options in QUESTIONS:
        cur = saved.get(key)
        chosen = set(cur or []) if kind == "many" else {cur}
        always = catalog.ALWAYS.get(key)
        opts = []
        for v, label in options:
            opt = {"value": v, "label": label, "checked": v in chosen or v == always,
                   "always": v == always}
            if key == "gear" and v in catalog.BAG_ANSWERS:
                opt["count"] = int(bags.get(v) or 0)
            opts.append(opt)
        out.append({"key": key, "kind": kind, "text": text, "help": help_, "options": opts})
    return out


def _profile_ctx(request: Request, session: Session, **extra) -> dict:
    from app.acting import acting_for
    acting = acting_for(session, request)
    tid = acting.traveler.id if acting.traveler else None
    row = _profile_row(session, tid, owner=acting.owner_mode)
    values = (row.values if row else None) or {}
    user, _ = _owner(request)
    ctx = {
        "request": request, "acting": acting, "base": BASE,
        "no_traveller": tid is None and not acting.owner_mode,
        "questions": _questionnaire_view(values.get("questionnaire") or {}),
        "taken": bool(values.get("questionnaire_saved_at")),
        "prefs": [{"key": k, "label": label,
                   "opts": [{"value": v, "label": t, "selected": v == values.get(k, d)}
                            for v, t in opts]}
                  for k, label, d, opts in PREFERENCES],
        "saved": False, "error": None,
    }
    if not acting.is_other:
        ctx["inventory_count"] = _own(session, InventoryItem, user).count()
        ctx["template_count"] = _own(session, PackingTemplate, user).count()
        ctx["bag_count"] = _own(session, Bag, user).filter(Bag.enabled.is_(True)).count()
    ctx.update(extra)
    return ctx


@router.get("", response_class=HTMLResponse)
def edit_packing_profile(request: Request, session: Session = Depends(get_session)):
    """The packing questionnaire and preferences (D-86: pick lists, no JSON)."""
    return templates.TemplateResponse(request, "packing_profile.html",
                                      _profile_ctx(request, session))


@router.post("", response_class=HTMLResponse)
async def save_packing_profile(request: Request, session: Session = Depends(get_session)):
    """Save the answers and preferences, then seed the inventory and bags from
    them (SPEC-5 §6.3), add-only. Read from the raw form: Q5's bag counts are
    one field per bag."""
    form = await request.form()
    q = catalog.QUESTIONNAIRE
    answers = {
        "style": form.get("style") if form.get("style") in q["style"] else None,
        "footwear": form.get("footwear") if form.get("footwear") in q["footwear"] else None,
    }
    for key in ("clothing", "care", "tech"):
        answers[key] = sorted(set(form.getlist(key)) & set(q[key]) - {catalog.ALWAYS[key]})
    bags = {k: _qty(form.get(f"bags:{k}"), 0, 0) for k in sorted(catalog.BAG_ANSWERS)}
    answers["bags"] = {k: n for k, n in bags.items() if n}
    organisers = set(form.getlist("gear")) & (set(q["gear"]) - catalog.BAG_ANSWERS)
    answers["gear"] = sorted(organisers | set(answers["bags"]))
    if not (answers["style"] and answers["footwear"]):
        return templates.TemplateResponse(request, "packing_profile.html", _profile_ctx(
            request, session, error="Pick a clothing style and a footwear answer."))

    values = {"questionnaire": answers,
              "questionnaire_saved_at": datetime.now(timezone.utc).isoformat()}
    for key, _label, default, opts in PREFERENCES:
        v = form.get(key)
        values[key] = v if v in {o for o, _ in opts} else default

    from app.acting import acting_for, audit_if_other
    acting = acting_for(session, request)          # self, or an admin acting for someone
    traveler_id = acting.traveler.id if acting.traveler else None
    if traveler_id is None and not acting.owner_mode:
        # Signed in but no traveller linked: nowhere of their own to save it.
        return templates.TemplateResponse(request, "packing_profile.html",
                                          _profile_ctx(request, session))
    audit_if_other(session, request, acting, "packing profile")
    own = _profile_row(session, traveler_id, owner=acting.owner_mode)
    if own is None:
        session.add(PackingProfile(values=values, traveler_id=traveler_id))
    else:
        own.traveler_id = traveler_id   # claims a pre-4.1 unassigned row for self
        # The 4.x keys are dropped, not migrated: everyone answers again (P4).
        own.values = values
    added = bags_added = 0
    if not acting.is_other:            # the inventory is the account's (§6.6)
        _, uid = _owner(request)
        added = inventory.seed_inventory(session, uid, answers, catalog.items(session))
        bags_added = inventory.seed_bags(session, uid, answers["bags"])
    session.commit()
    if added or bags_added:
        # What the answers did is the inventory, so show it there, not a
        # line on a page of questions that looks the same as before saving.
        return _back(f"/inventory?added={added}&bags={bags_added}")
    return templates.TemplateResponse(request, "packing_profile.html",
                                      _profile_ctx(request, session, saved=True))


# --------------------------------------------------------------------------- the inventory
def _catalog_match(session: Session, category: str, label: str):
    """A hand-added item that names a catalogue item ("Rain jacket") becomes
    that type, so the engine can match it to a need (§6.7)."""
    want = label.strip().casefold()
    for iid, it in catalog.items(session).items():
        if it.get("category") != category:
            continue
        names = {it.get("label", "")} | set((it.get("variants") or {}).values())
        if want in {n.casefold() for n in names if n}:
            return iid, it
    return None, None


def _add_item(session: Session, uid, category: str, label: str, quantity=1):
    iid, it = _catalog_match(session, category, label)
    row = InventoryItem(user_id=uid, type_key=iid, label=label, category=category,
                        tags=list((it or {}).get("tags") or []), quantity=_qty(quantity))
    session.add(row)
    return row


@router.get("/inventory", response_class=HTMLResponse)
def inventory_page(request: Request, added: int = 0,
                   bags_added: int = Query(0, alias="bags"),
                   session: Session = Depends(get_session)):
    user, _ = _owner(request)
    rows = _own(session, InventoryItem, user).order_by(InventoryItem.label).all()
    by_cat: dict[str, list] = {}
    for r in rows:
        by_cat.setdefault(r.category, []).append(r)
    # Every category, even an empty one (§6.1), and nothing filed under one we
    # no longer show.
    cats = [{"key": c, "label": PACK_CAT_LABELS[c], "rows": by_cat.pop(c, [])}
            for c in PACK_CAT_ORDER]
    for c, extra in by_cat.items():
        cats.append({"key": c, "label": c.title(), "rows": extra})
    bags = _own(session, Bag, user).filter(Bag.enabled.is_(True)).order_by(Bag.label).all()
    names = sorted({it.get("label", i) for i, it in catalog.items(session).items()})
    return templates.TemplateResponse(request, "packing_inventory.html", {
        "request": request, "base": BASE, "cats": cats, "bags": bags,
        "bag_kinds": BAG_KINDS, "catalog_names": names, "total": len(rows),
        "taken": _taken(request, session),
        # Arriving from the questionnaire: say what it added (numbers only).
        "added": max(0, added), "bags_added": max(0, bags_added)})


def _taken(request: Request, session: Session) -> bool:
    from app.acting import acting_for
    acting = acting_for(session, request)
    tid = acting.traveler.id if acting.traveler and not acting.is_other else None
    row = _profile_row(session, tid, owner=acting.owner_mode)
    return bool(row and (row.values or {}).get("questionnaire_saved_at"))


@router.post("/inventory/add")
def inventory_add(request: Request, category: str = Form(...), label: str = Form(...),
                  quantity: str = Form("1"), session: Session = Depends(get_session)):
    label = label.strip()[:128]
    if category not in PACK_CAT_LABELS or not label:
        return HTMLResponse("", status_code=400)
    _add_item(session, _owner(request)[1], category, label, quantity)
    session.commit()
    return _back("/inventory", f"cat-{category}")


@router.post("/inventory/{item_id}")
def inventory_save(item_id: str, request: Request, label: str = Form(""),
                   quantity: str = Form("1"), must_have: str = Form(""),
                   session: Session = Depends(get_session)):
    row = scope.require_own(session, _owner(request)[0], InventoryItem, item_id)
    label = label.strip()[:128]
    if label:
        row.label = label
    row.quantity = _qty(quantity, row.quantity)
    row.must_have = bool(must_have)
    session.commit()
    return _row_saved(request, "/inventory", f"cat-{row.category}",
                      "" if label else f"Saved ✓ (kept the name {row.label})")


@router.post("/inventory/{item_id}/delete")
def inventory_delete(item_id: str, request: Request, session: Session = Depends(get_session)):
    row = scope.require_own(session, _owner(request)[0], InventoryItem, item_id)
    category = row.category
    inventory.delete_inventory_item(session, row)
    session.commit()
    return _back("/inventory", f"cat-{category}")


@router.post("/bags/add")
def bag_add(request: Request, label: str = Form(...), kind: str = Form("backpack"),
            capacity: str = Form(""), session: Session = Depends(get_session)):
    label = label.strip()[:64]
    if not label or kind not in dict(BAG_KINDS):
        return HTMLResponse("", status_code=400)
    session.add(Bag(user_id=_owner(request)[1], label=label, kind=kind,
                    capacity=capacity.strip()[:32] or None))
    session.commit()
    return _back("/inventory", "bags")


@router.post("/bags/{bag_id}")
def bag_save(bag_id: str, request: Request, label: str = Form(""),
             kind: str = Form("backpack"), capacity: str = Form(""),
             session: Session = Depends(get_session)):
    bag = scope.require_own(session, _owner(request)[0], Bag, bag_id)
    if label.strip():
        bag.label = label.strip()[:64]
    if kind in dict(BAG_KINDS):
        bag.kind = kind
    bag.capacity = capacity.strip()[:32] or None
    session.commit()
    return _row_saved(request, "/inventory", "bags",
                      "" if label.strip() else f"Saved ✓ (kept the name {bag.label})")


@router.post("/bags/{bag_id}/remove")
def bag_remove(bag_id: str, request: Request, session: Session = Depends(get_session)):
    """Retired, not deleted: packing lines that went in it keep its name."""
    bag = scope.require_own(session, _owner(request)[0], Bag, bag_id)
    bag.enabled = False
    session.commit()
    return _back("/inventory", "bags")


# --------------------------------------------------------------------------- templates
def _types() -> dict[str, dict]:
    return {t["key"]: t for t in catalog.template_types()}


def _free_types(session: Session, user, keep: str | None = None) -> list[dict]:
    """Types the person can still create: one each, except `custom` (§6.5)."""
    used = {k for (k,) in _own(session, PackingTemplate, user)
            .with_entities(PackingTemplate.type_key)}
    return [t for t in catalog.template_types()
            if t["key"] == "custom" or t["key"] not in used or t["key"] == keep]


def _template_ctx(request: Request, session: Session, *, tpl=None, type_key=None,
                  keyword="", ticked=None, qtys=None, error=None) -> dict:
    user, _ = _owner(request)
    owned = _own(session, InventoryItem, user).order_by(InventoryItem.label).all()
    t = _types().get(tpl.type_key if tpl else type_key) or {}
    if ticked is None:
        if tpl is not None:
            lines = (session.query(PackingTemplateItem)
                     .filter(PackingTemplateItem.template_id == tpl.id).all())
            ticked = {l.inventory_item_id for l in lines}
            qtys = {l.inventory_item_id: l.quantity for l in lines}
        else:
            ticked = inventory.pretick(t, owned, catalog.items(session))
    qtys = qtys or {}
    by_cat: dict[str, list] = {}
    for r in owned:
        by_cat.setdefault(r.category, []).append(
            {"id": r.id, "label": r.label, "owned": r.quantity,
             "checked": r.id in ticked, "qty": qtys.get(r.id) or ""})
    cats = [{"key": c, "label": PACK_CAT_LABELS.get(c, c.title()), "rows": by_cat[c]}
            for c in PACK_CAT_ORDER + sorted(set(by_cat) - set(PACK_CAT_ORDER))
            if c in by_cat]
    return {"request": request, "base": BASE, "tpl": tpl, "type": t,
            "name": (tpl.name if tpl else t.get("label", "")),
            "keyword": (tpl.keyword if tpl else keyword) or "",
            "cats": cats, "cat_opts": [(c, PACK_CAT_LABELS[c]) for c in PACK_CAT_ORDER],
            "error": error}


@router.get("/templates", response_class=HTMLResponse)
def templates_page(request: Request, session: Session = Depends(get_session)):
    user, _ = _owner(request)
    types = _types()
    rows = _own(session, PackingTemplate, user).order_by(PackingTemplate.name).all()
    counts = {}
    if rows:
        for (tid,) in (session.query(PackingTemplateItem.template_id)
                       .filter(PackingTemplateItem.template_id.in_([r.id for r in rows]))):
            counts[tid] = counts.get(tid, 0) + 1
    mine = [{"id": r.id, "name": r.name, "keyword": r.keyword,
             "type": types.get(r.type_key, {}).get("label", r.type_key),
             "items": counts.get(r.id, 0)} for r in rows]
    return templates.TemplateResponse(request, "packing_templates.html", {
        "request": request, "base": BASE, "mine": mine,
        "free_types": _free_types(session, user)})


@router.get("/templates/new", response_class=HTMLResponse)
def template_new(request: Request, type: str = "", keyword: str = "",
                 session: Session = Depends(get_session)):
    """The unsaved template, its matching inventory pre-ticked: it only
    proposes until the person saves (§6.5)."""
    user, _ = _owner(request)
    if type not in {t["key"] for t in _free_types(session, user)}:
        return _back("/templates")
    return templates.TemplateResponse(request, "packing_template.html", _template_ctx(
        request, session, type_key=type, keyword=keyword.strip()[:64]))


def _save_lines(request: Request, session: Session, tpl, form) -> None:
    """Replace the template's lines with what's ticked, each only if it's the
    person's own; a hand-added item joins the inventory too (YONDER-62 item 15)."""
    user, uid = _owner(request)
    own_ids = {i for (i,) in _own(session, InventoryItem, user).with_entities(InventoryItem.id)}
    picked = [i for i in dict.fromkeys(form.getlist("item")) if i in own_ids]
    label = (form.get("add_label") or "").strip()[:128]
    category = form.get("add_category") or ""
    if label and category in PACK_CAT_LABELS:
        row = _add_item(session, uid, category, label)
        session.flush()
        picked.append(row.id)
    session.query(PackingTemplateItem).filter(
        PackingTemplateItem.template_id == tpl.id).delete(synchronize_session=False)
    for iid in picked:
        raw = (form.get(f"qty:{iid}") or "").strip()
        session.add(PackingTemplateItem(template_id=tpl.id, inventory_item_id=iid,
                                        quantity=_qty(raw) if raw else None))


@router.post("/templates")
async def template_create(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    user, uid = _owner(request)
    type_key = form.get("type") or ""
    keyword = (form.get("keyword") or "").strip()[:64]
    if type_key not in {t["key"] for t in _free_types(session, user)}:
        return _back("/templates")
    if type_key == "custom" and not keyword:
        return templates.TemplateResponse(request, "packing_template.html", _template_ctx(
            request, session, type_key=type_key, ticked=set(form.getlist("item")),
            error="A custom template needs a keyword."), status_code=400)
    name = (form.get("name") or "").strip()[:128] or _types()[type_key]["label"]
    tpl = PackingTemplate(user_id=uid, type_key=type_key, name=name,
                          keyword=keyword if type_key == "custom" else None)
    session.add(tpl)
    session.flush()
    _save_lines(request, session, tpl, form)
    session.commit()
    return _back(f"/templates/{tpl.id}")


@router.get("/templates/{template_id}", response_class=HTMLResponse)
def template_edit(template_id: str, request: Request, session: Session = Depends(get_session)):
    tpl = scope.require_own(session, _owner(request)[0], PackingTemplate, template_id)
    return templates.TemplateResponse(request, "packing_template.html",
                                      _template_ctx(request, session, tpl=tpl))


@router.post("/templates/{template_id}")
async def template_save(template_id: str, request: Request,
                        session: Session = Depends(get_session)):
    tpl = scope.require_own(session, _owner(request)[0], PackingTemplate, template_id)
    form = await request.form()
    name = (form.get("name") or "").strip()[:128]
    if name:
        tpl.name = name
    if tpl.type_key == "custom":
        keyword = (form.get("keyword") or "").strip()[:64]
        if keyword:
            tpl.keyword = keyword
    _save_lines(request, session, tpl, form)
    session.commit()
    return _back(f"/templates/{tpl.id}")


@router.post("/templates/{template_id}/delete")
def template_delete(template_id: str, request: Request, session: Session = Depends(get_session)):
    tpl = scope.require_own(session, _owner(request)[0], PackingTemplate, template_id)
    inventory.delete_template(session, tpl)
    session.commit()
    return _back("/templates")
