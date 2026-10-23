"""Quest log persistence and rules, shared by the HTTP routes and the agent tool.

Every function takes an open SQLAlchemy session and the acting owner (None in
single-user mode) and raises QuestError for anything the caller should report.
Callers own commit/close. Keeping one implementation means the web page and
Jarvis's manage_quests tool can never disagree about ownership, XP or
dependency rules.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import func

from core.database import Quest, QuestCampaign, QuestEvent, utcnow_naive
from services.quests import engine

TITLE_MAX = 200
NOTES_MAX = 5000
OUTCOME_MAX = 2000
SKILL_MAX = 60
CAMPAIGN_STATUSES = ("active", "paused", "done")

# Fields a caller may set when creating/updating a quest.
QUEST_FIELDS = ("title", "campaign_id", "notes", "impact", "effort", "due_date",
                "depends_on", "skill", "is_boss")


class QuestError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# ---------------------------------------------------------------------------
# Cleaning
# ---------------------------------------------------------------------------

def clean_title(value: Optional[str]) -> str:
    title = (value or "").strip()
    if not title:
        raise QuestError(422, "Title is required")
    return title[:TITLE_MAX]


def clean_text(value: Optional[str], limit: int) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def clean_due(value: Optional[str]) -> Optional[str]:
    if value is None or value == "":
        return None
    parsed = engine.parse_day(value)
    if parsed is None:
        raise QuestError(422, "due_date must be YYYY-MM-DD")
    return parsed.isoformat()


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------

def deps_of(quest: Quest) -> List[str]:
    if not quest.depends_on:
        return []
    try:
        deps = json.loads(quest.depends_on)
    except (json.JSONDecodeError, TypeError):
        return []
    return [d for d in deps if isinstance(d, str)]


def view_of(quest: Quest, order: int = 0) -> engine.QuestView:
    return engine.QuestView(
        id=quest.id,
        title=quest.title,
        status=quest.status or "todo",
        impact=quest.impact or 2,
        effort=quest.effort or 2,
        due_date=quest.due_date,
        depends_on=tuple(deps_of(quest)),
        is_boss=bool(quest.is_boss),
        campaign_id=quest.campaign_id,
        created_order=order,
    )


def quest_dict(quest: Quest, views: Dict[str, engine.QuestView]) -> Dict[str, Any]:
    view = views.get(quest.id) or view_of(quest)
    return {
        "id": quest.id,
        "campaign_id": quest.campaign_id,
        "title": quest.title,
        "notes": quest.notes,
        "status": quest.status or "todo",
        "impact": quest.impact or 2,
        "effort": quest.effort or 2,
        "due_date": quest.due_date,
        "depends_on": deps_of(quest),
        "blocked_by": [{"id": b.id, "title": b.title} for b in engine.blockers(view, views)],
        "skill": quest.skill,
        "is_boss": bool(quest.is_boss),
        "xp_value": engine.xp_for(quest.effort, bool(quest.is_boss)),
        "outcome": quest.outcome,
        "completed_at": quest.completed_at.isoformat() if quest.completed_at else None,
        "created_at": quest.created_at.isoformat() if quest.created_at else None,
    }


def campaign_dict(c: QuestCampaign) -> Dict[str, Any]:
    return {
        "id": c.id,
        "title": c.title,
        "description": c.description,
        "status": c.status or "active",
        "sort_order": c.sort_order or 0,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


def event_dict(e: QuestEvent) -> Dict[str, Any]:
    return {
        "id": e.id,
        "quest_id": e.quest_id,
        "campaign_id": e.campaign_id,
        "kind": e.kind,
        "detail": e.detail,
        "skill": e.skill,
        "xp_delta": e.xp_delta or 0,
        "created_at": e.created_at.isoformat() if e.created_at else None,
    }


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def _scoped(q, model, owner):
    return q.filter(model.owner == owner) if owner is not None else q


def all_quests(db, owner) -> List[Quest]:
    return _scoped(db.query(Quest), Quest, owner).order_by(Quest.created_at.asc()).all()


def all_campaigns(db, owner) -> List[QuestCampaign]:
    return _scoped(db.query(QuestCampaign), QuestCampaign, owner).order_by(
        QuestCampaign.sort_order.asc(), QuestCampaign.created_at.asc()).all()


def views_for(quests: List[Quest]) -> Dict[str, engine.QuestView]:
    return {q.id: view_of(q, i) for i, q in enumerate(quests)}


def get_quest(db, owner, quest_id: str) -> Quest:
    quest = db.query(Quest).filter(Quest.id == quest_id).first()
    if not quest or (owner is not None and quest.owner != owner):
        raise QuestError(404, "Quest not found")
    return quest


def get_campaign(db, owner, campaign_id: str) -> QuestCampaign:
    c = db.query(QuestCampaign).filter(QuestCampaign.id == campaign_id).first()
    if not c or (owner is not None and c.owner != owner):
        raise QuestError(404, "Campaign not found")
    return c


def _norm(text: str) -> str:
    return " ".join((text or "").casefold().split())


def find_quest(db, owner, ref: str, include_finished: bool = True) -> Quest:
    """Resolve a quest by id, id prefix (>= 6 chars) or title.

    Title matching is exact (case/whitespace-insensitive) first, then a unique
    substring match. Ambiguity is an error rather than a guess, so the agent
    never edits the wrong quest.
    """
    ref = (ref or "").strip()
    if not ref:
        raise QuestError(422, "Which quest? Give its id or title")
    quests = all_quests(db, owner)
    if not include_finished:
        quests = [q for q in quests if q.status in engine.OPEN_STATUSES]
    for q in quests:
        if q.id == ref:
            return q
    if len(ref) >= 6:
        by_prefix = [q for q in quests if q.id.startswith(ref)]
        if len(by_prefix) == 1:
            return by_prefix[0]
    target = _norm(ref)
    exact = [q for q in quests if _norm(q.title) == target]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        open_exact = [q for q in exact if q.status in engine.OPEN_STATUSES]
        if len(open_exact) == 1:
            return open_exact[0]
    partial = [q for q in quests if target in _norm(q.title)]
    if len(partial) == 1:
        return partial[0]
    if not partial and not exact:
        raise QuestError(404, f"No quest matches '{ref}'")
    names = "; ".join(f"{q.title} [{q.id[:8]}]" for q in (exact or partial)[:6])
    raise QuestError(409, f"'{ref}' matches several quests: {names}. Use the id.")


def find_campaign(db, owner, ref: str) -> Optional[QuestCampaign]:
    """Resolve a campaign by id or title; None when nothing matches."""
    ref = (ref or "").strip()
    if not ref:
        return None
    campaigns = all_campaigns(db, owner)
    for c in campaigns:
        if c.id == ref:
            return c
    target = _norm(ref)
    exact = [c for c in campaigns if _norm(c.title) == target]
    if exact:
        return exact[0]
    partial = [c for c in campaigns if target in _norm(c.title)]
    return partial[0] if len(partial) == 1 else None


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------

def log_event(db, owner, quest: Optional[Quest], kind: str, detail: Optional[str] = None, xp: int = 0):
    db.add(QuestEvent(
        id=str(uuid.uuid4()),
        owner=owner,
        quest_id=quest.id if quest else None,
        campaign_id=quest.campaign_id if quest else None,
        kind=kind,
        detail=detail,
        skill=quest.skill if quest else None,
        xp_delta=xp,
    ))


def validate_deps(db, owner, quest_id: Optional[str], deps: Iterable[str]) -> List[str]:
    deps = list(dict.fromkeys(d for d in (deps or []) if isinstance(d, str) and d))
    if quest_id and quest_id in deps:
        raise QuestError(422, "A quest cannot depend on itself")
    owned = {q.id: deps_of(q) for q in all_quests(db, owner)}
    if any(d not in owned for d in deps):
        raise QuestError(422, "Unknown prerequisite quest")
    if quest_id and engine.would_create_cycle(quest_id, deps, owned):
        raise QuestError(422, "That prerequisite would create a dependency loop")
    return deps


def require_unblocked(db, owner, quest: Quest):
    views = views_for(all_quests(db, owner))
    blocking = engine.blockers(views[quest.id], views)
    if blocking:
        raise QuestError(409, "Blocked by: " + ", ".join(b.title for b in blocking))


def create_campaign(db, owner, title: str, description: Optional[str] = None) -> QuestCampaign:
    c = QuestCampaign(
        id=str(uuid.uuid4()),
        owner=owner,
        title=clean_title(title),
        description=clean_text(description, NOTES_MAX),
        status="active",
    )
    db.add(c)
    db.flush()
    return c


def update_campaign(db, owner, campaign_id: str, fields: Dict[str, Any]) -> QuestCampaign:
    c = get_campaign(db, owner, campaign_id)
    if "title" in fields and fields["title"] is not None:
        c.title = clean_title(fields["title"])
    if "description" in fields and fields["description"] is not None:
        c.description = clean_text(fields["description"], NOTES_MAX)
    if "status" in fields and fields["status"] is not None:
        if fields["status"] not in CAMPAIGN_STATUSES:
            raise QuestError(422, f"status must be one of {', '.join(CAMPAIGN_STATUSES)}")
        c.status = fields["status"]
    if "sort_order" in fields and fields["sort_order"] is not None:
        c.sort_order = int(fields["sort_order"])
    db.flush()
    return c


def delete_campaign(db, owner, campaign_id: str):
    c = get_campaign(db, owner, campaign_id)
    if _scoped(db.query(Quest), Quest, owner).filter(Quest.campaign_id == c.id).count():
        raise QuestError(409, "Campaign still has quests; mark it done or move them first")
    db.delete(c)


def create_quest(db, owner, fields: Dict[str, Any]) -> Quest:
    campaign_id = fields.get("campaign_id") or None
    if campaign_id:
        get_campaign(db, owner, campaign_id)
    deps = validate_deps(db, owner, None, fields.get("depends_on") or [])
    quest = Quest(
        id=str(uuid.uuid4()),
        owner=owner,
        campaign_id=campaign_id,
        title=clean_title(fields.get("title")),
        notes=clean_text(fields.get("notes"), NOTES_MAX),
        status="todo",
        impact=engine.clamp_scale(fields.get("impact", 2)),
        effort=engine.clamp_scale(fields.get("effort", 2)),
        due_date=clean_due(fields.get("due_date")),
        depends_on=json.dumps(deps) if deps else None,
        skill=clean_text(fields.get("skill"), SKILL_MAX),
        is_boss=bool(fields.get("is_boss", False)),
    )
    db.add(quest)
    log_event(db, owner, quest, "created")
    db.flush()
    return quest


def update_quest(db, owner, quest_id: str, fields: Dict[str, Any]) -> Quest:
    """Apply only the keys present in `fields`."""
    quest = get_quest(db, owner, quest_id)
    if "title" in fields:
        quest.title = clean_title(fields["title"])
    if "campaign_id" in fields:
        if fields["campaign_id"]:
            get_campaign(db, owner, fields["campaign_id"])
        quest.campaign_id = fields["campaign_id"] or None
    if "notes" in fields:
        quest.notes = clean_text(fields["notes"], NOTES_MAX)
    if "impact" in fields:
        quest.impact = engine.clamp_scale(fields["impact"])
    if "effort" in fields:
        if quest.status == "done":
            raise QuestError(409, "Reopen the quest before changing its effort")
        quest.effort = engine.clamp_scale(fields["effort"])
    if "due_date" in fields:
        quest.due_date = clean_due(fields["due_date"])
    if "depends_on" in fields:
        deps = validate_deps(db, owner, quest.id, fields["depends_on"] or [])
        quest.depends_on = json.dumps(deps) if deps else None
    if "skill" in fields:
        quest.skill = clean_text(fields["skill"], SKILL_MAX)
    if "is_boss" in fields:
        if quest.status == "done":
            raise QuestError(409, "Reopen the quest before changing boss status")
        quest.is_boss = bool(fields["is_boss"])
    db.flush()
    return quest


def delete_quest(db, owner, quest_id: str):
    quest = get_quest(db, owner, quest_id)
    if quest.status == "done":
        raise QuestError(409, "Completed quests are part of your history; reopen it first")
    # Free dependants so nothing stays blocked on a quest that no longer exists.
    for other in all_quests(db, owner):
        deps = deps_of(other)
        if quest.id in deps:
            deps.remove(quest.id)
            other.depends_on = json.dumps(deps) if deps else None
    log_event(db, owner, quest, "deleted", detail=quest.title)
    db.delete(quest)


def start_quest(db, owner, quest_id: str) -> Quest:
    quest = get_quest(db, owner, quest_id)
    if quest.status != "todo":
        raise QuestError(409, f"Quest is {quest.status}, not todo")
    require_unblocked(db, owner, quest)
    quest.status = "doing"
    log_event(db, owner, quest, "started")
    db.flush()
    return quest


def complete_quest(db, owner, quest_id: str, outcome: Optional[str]) -> tuple[Quest, int]:
    outcome = clean_text(outcome, OUTCOME_MAX)
    if not outcome:
        raise QuestError(422, "Describe the outcome: what actually changed?")
    quest = get_quest(db, owner, quest_id)
    if quest.status not in engine.OPEN_STATUSES:
        raise QuestError(409, f"Quest is {quest.status}")
    require_unblocked(db, owner, quest)
    xp = engine.xp_for(quest.effort, bool(quest.is_boss))
    quest.status = "done"
    quest.outcome = outcome
    quest.completed_at = utcnow_naive()
    log_event(db, owner, quest, "completed", detail=outcome, xp=xp)
    db.flush()
    return quest, xp


def reopen_quest(db, owner, quest_id: str) -> Quest:
    quest = get_quest(db, owner, quest_id)
    if quest.status not in ("done", "dropped"):
        raise QuestError(409, "Only done or dropped quests can be reopened")
    # Reverse whatever XP this quest has netted so far, as a new event.
    net = _scoped(db.query(func.coalesce(func.sum(QuestEvent.xp_delta), 0)), QuestEvent, owner).filter(
        QuestEvent.quest_id == quest.id).scalar() or 0
    quest.status = "todo"
    quest.completed_at = None
    quest.outcome = None
    log_event(db, owner, quest, "reopened", xp=-int(net))
    db.flush()
    return quest


def drop_quest(db, owner, quest_id: str) -> Quest:
    quest = get_quest(db, owner, quest_id)
    if quest.status not in engine.OPEN_STATUSES:
        raise QuestError(409, f"Quest is {quest.status}")
    quest.status = "dropped"
    log_event(db, owner, quest, "dropped")
    db.flush()
    return quest


# ---------------------------------------------------------------------------
# Read models
# ---------------------------------------------------------------------------

def board(db, owner, today: Optional[date] = None) -> Dict[str, Any]:
    campaigns = all_campaigns(db, owner)
    quests = all_quests(db, owner)
    views = views_for(quests)
    inactive = [c.id for c in campaigns if (c.status or "active") != "active"]

    events_q = _scoped(db.query(QuestEvent), QuestEvent, owner)
    total_xp = events_q.with_entities(func.coalesce(func.sum(QuestEvent.xp_delta), 0)).scalar() or 0
    skill_rows = events_q.with_entities(QuestEvent.skill, func.sum(QuestEvent.xp_delta)).filter(
        QuestEvent.skill.isnot(None)).group_by(QuestEvent.skill).all()
    campaign_rows = events_q.with_entities(QuestEvent.campaign_id, func.sum(QuestEvent.xp_delta)).filter(
        QuestEvent.campaign_id.isnot(None)).group_by(QuestEvent.campaign_id).all()
    today_start = datetime.combine(today or date.today(), datetime.min.time())
    done_today = events_q.filter(
        QuestEvent.kind == "completed", QuestEvent.created_at >= today_start).count()

    campaign_xp = {cid: int(xp or 0) for cid, xp in campaign_rows}
    campaign_dicts = []
    for c in campaigns:
        d = campaign_dict(c)
        mine = [q for q in quests if q.campaign_id == c.id]
        d["quest_count"] = len(mine)
        d["done_count"] = sum(1 for q in mine if q.status == "done")
        d["xp"] = campaign_xp.get(c.id, 0)
        campaign_dicts.append(d)

    return {
        "campaigns": campaign_dicts,
        "quests": [quest_dict(q, views) for q in quests],
        "stats": {
            **engine.level_for(int(total_xp)),
            "done_today": done_today,
            "skills": sorted(
                ({"skill": s, "xp": int(x or 0)} for s, x in skill_rows if int(x or 0) > 0),
                key=lambda r: -r["xp"]),
        },
        "next": engine.recommend(list(views.values()), inactive_campaigns=inactive, today=today),
    }


def history(db, owner, limit: int = 50) -> List[Dict[str, Any]]:
    limit = min(200, max(1, int(limit)))
    rows = _scoped(db.query(QuestEvent), QuestEvent, owner).order_by(
        QuestEvent.created_at.desc()).limit(limit).all()
    titles = {q.id: q.title for q in all_quests(db, owner)}
    out = []
    for e in rows:
        d = event_dict(e)
        d["quest_title"] = titles.get(e.quest_id)
        out.append(d)
    return out
