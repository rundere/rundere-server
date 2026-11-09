"""A person's packing inventory and templates (SPEC-5 §6.6, D-127, D-230).

The rows are own-only (app.scope.own_filter); every caller has already checked
the person may touch them. Links are soft (house rule 3), so deleting sweeps
the rows that point at what's gone: a template's lines go with it, an
inventory item leaves every template it was in, and packing lines that were
that item keep their text but lose the link.
"""
from __future__ import annotations

from sqlalchemy.orm import Session


def delete_inventory_item(session: Session, item) -> None:
    """Delete an inventory item, its template lines, and its packing-line
    links. The caller commits."""
    from app.models import PackingItem, PackingTemplateItem
    session.query(PackingTemplateItem).filter(
        PackingTemplateItem.inventory_item_id == item.id).delete(synchronize_session=False)
    # A packed line stays on the trip's list: it was packed, whatever happens
    # to the item since.
    session.query(PackingItem).filter(PackingItem.inventory_item_id == item.id).update(
        {"inventory_item_id": None}, synchronize_session=False)
    session.delete(item)


def delete_template(session: Session, template) -> None:
    """Delete a template and its lines; the inventory items stay. The caller
    commits."""
    from app.models import PackingTemplateItem
    session.query(PackingTemplateItem).filter(
        PackingTemplateItem.template_id == template.id).delete(synchronize_session=False)
    session.delete(template)


def _own(session: Session, model, user_id):
    q = session.query(model)
    return q.filter(model.user_id == user_id) if user_id else q.filter(model.user_id.is_(None))


def seed_inventory(session: Session, user_id, answers: dict, items: dict) -> int:
    """Add what the questionnaire answers seed (SPEC-5 §6.3) and the person
    doesn't own yet, matched by `type_key`. Never deletes, renames or
    re-counts, so re-running is safe. Returns how many were added; the
    caller commits."""
    from app.models import InventoryItem
    from planning.catalog import seed_for
    have = {k for (k,) in _own(session, InventoryItem, user_id)
            .with_entities(InventoryItem.type_key) if k}
    style = answers.get("style") or "neutral"
    added = 0
    for iid, qty in seed_for(answers, items).items():
        if iid in have:
            continue
        it = items[iid]
        label = (it.get("variants") or {}).get(style) or it.get("label") or iid
        session.add(InventoryItem(user_id=user_id, type_key=iid, label=label,
                                  category=it.get("category", "activity"),
                                  tags=list(it.get("tags") or []), quantity=qty))
        added += 1
    return added


# Q5's bags (SPEC-5 §6.3): each becomes Bag rows, `kind` from the answer.
BAG_SEEDS = {"daypack": ("Daypack", "backpack"),
             "backpack": ("Travel backpack", "backpack"),
             "carry_on": ("Carry-on suitcase", "suitcase"),
             "checked": ("Checked suitcase", "suitcase"),
             "personal_item": ("Personal item", "personal_item")}


def seed_bags(session: Session, user_id, counts: dict) -> int:
    """Make sure the person has `counts[answer]` bags of each kind, named
    "Daypack", "Daypack 2", …; a name they already have counts. Add-only,
    like the inventory. The caller commits."""
    from app.models import Bag
    have = {b.label for b in _own(session, Bag, user_id)}
    added = 0
    for key, n in counts.items():
        if key not in BAG_SEEDS:
            continue
        base, kind = BAG_SEEDS[key]
        for i in range(1, max(0, min(int(n), 9)) + 1):
            label = base if i == 1 else f"{base} {i}"
            if label not in have:
                session.add(Bag(user_id=user_id, label=label, kind=kind))
                added += 1
    return added


def pretick(template_type: dict, owned: list, items: dict) -> set[str]:
    """The ids of the owned inventory items a new template of this type
    proposes (SPEC-5 §6.5): catalogue items whose `seed` tags match, plus the
    named ones. Only proposes; the person saves."""
    rule = template_type.get("pretick") or {}
    seeds, ids = set(rule.get("seed") or []), set(rule.get("items") or [])
    out = set()
    for row in owned:
        if not row.type_key:
            continue
        cat = items.get(row.type_key) or {}
        if row.type_key in ids or seeds & set(cat.get("seed") or []):
            out.add(row.id)
    return out
