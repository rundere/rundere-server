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
