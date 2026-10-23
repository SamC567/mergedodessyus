"""Life resources: budgets, savings targets, owned equipment and weekly time.

Gives planning a realistic picture: open quests' cost estimates are checked
against the budget linked to their campaign, and weekly hours are compared
with the time today's missions need.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from core.database import LifeResource
from services.quests import engine, store
from services.quests.store import QuestError

KINDS = ("budget", "savings", "equipment", "time")
NAME_MAX = 120
NOTES_MAX = 2000


def _scoped(q, owner):
    return q.filter(LifeResource.owner == owner) if owner is not None else q


def resource_dict(r: LifeResource) -> Dict[str, Any]:
    return {
        "id": r.id, "kind": r.kind, "name": r.name, "amount": r.amount,
        "target": r.target, "unit": r.unit, "notes": r.notes,
        "campaign_id": r.campaign_id,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


def _num(value, label: str) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        n = round(float(value), 2)
    except (TypeError, ValueError):
        raise QuestError(422, f"{label} must be a number")
    if n < 0:
        raise QuestError(422, f"{label} cannot be negative")
    return n


def get_resource(db, owner, resource_id: str) -> LifeResource:
    r = db.query(LifeResource).filter(LifeResource.id == resource_id).first()
    if not r or (owner is not None and r.owner != owner):
        raise QuestError(404, "Resource not found")
    return r


def _apply(db, owner, r: LifeResource, fields: Dict[str, Any]):
    if "kind" in fields:
        kind = str(fields["kind"] or "").strip().lower()
        if kind not in KINDS:
            raise QuestError(422, f"kind must be one of {', '.join(KINDS)}")
        r.kind = kind
    if "name" in fields:
        name = " ".join(str(fields["name"] or "").split())
        if not name:
            raise QuestError(422, "Name is required")
        r.name = name[:NAME_MAX]
    if "amount" in fields:
        r.amount = _num(fields["amount"], "amount")
    if "target" in fields:
        r.target = _num(fields["target"], "target")
    if "unit" in fields:
        r.unit = (str(fields["unit"]).strip()[:20] or None) if fields["unit"] else None
    if "notes" in fields:
        r.notes = (str(fields["notes"]).strip()[:NOTES_MAX] or None) if fields["notes"] else None
    if "campaign_id" in fields:
        if fields["campaign_id"]:
            store.get_campaign(db, owner, fields["campaign_id"])
        r.campaign_id = fields["campaign_id"] or None


def create(db, owner, fields: Dict[str, Any]) -> LifeResource:
    r = LifeResource(id=str(uuid.uuid4()), owner=owner, kind="", name="")
    _apply(db, owner, r, {"kind": fields.get("kind"), "name": fields.get("name"), **{
        k: fields[k] for k in ("amount", "target", "unit", "notes", "campaign_id") if k in fields}})
    if r.kind == "time" and not r.unit:
        r.unit = "hours/week"
    db.add(r)
    db.flush()
    return r


def update(db, owner, resource_id: str, fields: Dict[str, Any]) -> LifeResource:
    r = get_resource(db, owner, resource_id)
    _apply(db, owner, r, fields)
    db.flush()
    return r


def delete(db, owner, resource_id: str):
    db.delete(get_resource(db, owner, resource_id))


def list_resources(db, owner) -> List[LifeResource]:
    return _scoped(db.query(LifeResource), owner).order_by(
        LifeResource.kind.asc(), LifeResource.name.asc()).all()


def summary(db, owner) -> Dict[str, Any]:
    """Resources plus budget pressure from open quests' cost estimates."""
    resources = list_resources(db, owner)
    quests = store.all_quests(db, owner)
    open_quests = [q for q in quests if q.status in engine.OPEN_STATUSES]
    campaigns = {c.id: c.title for c in store.all_campaigns(db, owner)}

    budgets = []
    for r in resources:
        if r.kind != "budget":
            continue
        scope = [q for q in open_quests if r.campaign_id is None or q.campaign_id == r.campaign_id]
        planned = round(sum(q.est_cost or 0 for q in scope), 2)
        budgets.append({
            **resource_dict(r),
            "campaign_title": campaigns.get(r.campaign_id),
            "planned_spend": planned,
            "remaining": None if r.amount is None else round(r.amount - planned, 2),
            "over": r.amount is not None and planned > r.amount,
            "uncosted_quests": sum(1 for q in scope if q.est_cost is None),
        })

    budgeted_campaigns = {r.campaign_id for r in resources if r.kind == "budget"}
    unbudgeted = round(sum(
        q.est_cost or 0 for q in open_quests
        if q.campaign_id not in budgeted_campaigns and None not in budgeted_campaigns), 2)

    hours = sum(r.amount or 0 for r in resources if r.kind == "time")
    return {
        "resources": [resource_dict(r) for r in resources],
        "budgets": budgets,
        "unbudgeted_planned_spend": unbudgeted,
        "weekly_hours": hours or None,
        "open_quest_minutes": sum(engine.minutes_for(store.view_of(q)) for q in open_quests),
    }
