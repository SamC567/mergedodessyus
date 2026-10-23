# routes/quests/quest_routes.py
"""Quest log API: campaigns, quests, append-only progress history.

XP is derived from QuestEvent rows, never stored as an editable score.
Completing a quest requires an outcome ("what actually changed"), so XP
tracks evidenced progress rather than clicks. There are no routes that
update or delete events.
"""

import json
import uuid
import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func

from core.database import SessionLocal, Quest, QuestCampaign, QuestEvent, utcnow_naive
from src.auth_helpers import require_user
from services.quests import engine

logger = logging.getLogger(__name__)

TITLE_MAX = 200
NOTES_MAX = 5000
OUTCOME_MAX = 2000
CAMPAIGN_STATUSES = ("active", "paused", "done")


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class CampaignCreate(BaseModel):
    title: str
    description: Optional[str] = None


class CampaignUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    sort_order: Optional[int] = None


class QuestCreate(BaseModel):
    title: str
    campaign_id: Optional[str] = None
    notes: Optional[str] = None
    impact: int = 2
    effort: int = 2
    due_date: Optional[str] = None
    depends_on: Optional[List[str]] = None
    skill: Optional[str] = None
    is_boss: bool = False


class QuestUpdate(BaseModel):
    title: Optional[str] = None
    campaign_id: Optional[str] = None
    notes: Optional[str] = None
    impact: Optional[int] = None
    effort: Optional[int] = None
    due_date: Optional[str] = None
    depends_on: Optional[List[str]] = None
    skill: Optional[str] = None
    is_boss: Optional[bool] = None


class QuestComplete(BaseModel):
    outcome: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clean_title(value: Optional[str]) -> str:
    title = (value or "").strip()
    if not title:
        raise HTTPException(422, "Title is required")
    return title[:TITLE_MAX]


def _clean_text(value: Optional[str], limit: int) -> Optional[str]:
    if value is None:
        return None
    text = value.strip()
    return text[:limit] if text else None


def _clean_due(value: Optional[str]) -> Optional[str]:
    if value is None or value == "":
        return None
    parsed = engine.parse_day(value)
    if parsed is None:
        raise HTTPException(422, "due_date must be YYYY-MM-DD")
    return parsed.isoformat()


def _deps(quest: Quest) -> List[str]:
    if not quest.depends_on:
        return []
    try:
        deps = json.loads(quest.depends_on)
    except (json.JSONDecodeError, TypeError):
        return []
    return [d for d in deps if isinstance(d, str)]


def _view(quest: Quest, order: int = 0) -> engine.QuestView:
    return engine.QuestView(
        id=quest.id,
        title=quest.title,
        status=quest.status or "todo",
        impact=quest.impact or 2,
        effort=quest.effort or 2,
        due_date=quest.due_date,
        depends_on=tuple(_deps(quest)),
        is_boss=bool(quest.is_boss),
        campaign_id=quest.campaign_id,
        created_order=order,
    )


def _quest_to_dict(quest: Quest, by_id: Dict[str, engine.QuestView]) -> Dict[str, Any]:
    view = by_id.get(quest.id) or _view(quest)
    return {
        "id": quest.id,
        "campaign_id": quest.campaign_id,
        "title": quest.title,
        "notes": quest.notes,
        "status": quest.status or "todo",
        "impact": quest.impact or 2,
        "effort": quest.effort or 2,
        "due_date": quest.due_date,
        "depends_on": _deps(quest),
        "blocked_by": [{"id": b.id, "title": b.title} for b in engine.blockers(view, by_id)],
        "skill": quest.skill,
        "is_boss": bool(quest.is_boss),
        "xp_value": engine.xp_for(quest.effort, bool(quest.is_boss)),
        "outcome": quest.outcome,
        "completed_at": quest.completed_at.isoformat() if quest.completed_at else None,
        "created_at": quest.created_at.isoformat() if quest.created_at else None,
    }


def _campaign_to_dict(c: QuestCampaign) -> Dict[str, Any]:
    return {
        "id": c.id,
        "title": c.title,
        "description": c.description,
        "status": c.status or "active",
        "sort_order": c.sort_order or 0,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


def _event_to_dict(e: QuestEvent) -> Dict[str, Any]:
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


def setup_quest_routes():
    router = APIRouter(prefix="/api/quests", tags=["quests"])

    def _owner(request: Request) -> Optional[str]:
        # Same fail-closed policy as notes: no identity under configured
        # auth is a 401, never single-user blanket access.
        return require_user(request) or None

    def _scoped(q, model, user):
        return q.filter(model.owner == user) if user is not None else q

    def _get_quest(db, user, quest_id: str) -> Quest:
        quest = db.query(Quest).filter(Quest.id == quest_id).first()
        if not quest or (user is not None and quest.owner != user):
            raise HTTPException(404, "Quest not found")
        return quest

    def _get_campaign(db, user, campaign_id: str) -> QuestCampaign:
        c = db.query(QuestCampaign).filter(QuestCampaign.id == campaign_id).first()
        if not c or (user is not None and c.owner != user):
            raise HTTPException(404, "Campaign not found")
        return c

    def _owner_quests(db, user) -> List[Quest]:
        return _scoped(db.query(Quest), Quest, user).order_by(Quest.created_at.asc()).all()

    def _views(quests: List[Quest]) -> Dict[str, engine.QuestView]:
        return {q.id: _view(q, i) for i, q in enumerate(quests)}

    def _log(db, user, quest: Optional[Quest], kind: str, detail: Optional[str] = None, xp: int = 0):
        db.add(QuestEvent(
            id=str(uuid.uuid4()),
            owner=user,
            quest_id=quest.id if quest else None,
            campaign_id=quest.campaign_id if quest else None,
            kind=kind,
            detail=detail,
            skill=quest.skill if quest else None,
            xp_delta=xp,
        ))

    def _validate_deps(db, user, quest_id: Optional[str], deps: List[str]) -> List[str]:
        deps = list(dict.fromkeys(d for d in deps if isinstance(d, str) and d))
        if quest_id and quest_id in deps:
            raise HTTPException(422, "A quest cannot depend on itself")
        owned = {q.id: _deps(q) for q in _owner_quests(db, user)}
        missing = [d for d in deps if d not in owned]
        if missing:
            raise HTTPException(422, "Unknown prerequisite quest")
        if quest_id and engine.would_create_cycle(quest_id, deps, owned):
            raise HTTPException(422, "That prerequisite would create a dependency loop")
        return deps

    def _require_unblocked(db, user, quest: Quest):
        views = _views(_owner_quests(db, user))
        blocking = engine.blockers(views[quest.id], views)
        if blocking:
            names = ", ".join(b.title for b in blocking)
            raise HTTPException(409, f"Blocked by: {names}")

    # --- BOARD (everything the page needs in one call) ---
    @router.get("/board")
    def board(request: Request):
        user = _owner(request)
        db = SessionLocal()
        try:
            campaigns = _scoped(db.query(QuestCampaign), QuestCampaign, user).order_by(
                QuestCampaign.sort_order.asc(), QuestCampaign.created_at.asc()).all()
            quests = _owner_quests(db, user)
            views = _views(quests)
            inactive = [c.id for c in campaigns if (c.status or "active") != "active"]

            events_q = _scoped(db.query(QuestEvent), QuestEvent, user)
            total_xp = events_q.with_entities(func.coalesce(func.sum(QuestEvent.xp_delta), 0)).scalar() or 0
            skill_rows = events_q.with_entities(
                QuestEvent.skill, func.sum(QuestEvent.xp_delta)).filter(
                QuestEvent.skill.isnot(None)).group_by(QuestEvent.skill).all()
            campaign_rows = events_q.with_entities(
                QuestEvent.campaign_id, func.sum(QuestEvent.xp_delta)).filter(
                QuestEvent.campaign_id.isnot(None)).group_by(QuestEvent.campaign_id).all()
            today_start = datetime.combine(date.today(), datetime.min.time())
            done_today = events_q.filter(
                QuestEvent.kind == "completed", QuestEvent.created_at >= today_start).count()

            campaign_xp = {cid: int(xp or 0) for cid, xp in campaign_rows}
            campaign_dicts = []
            for c in campaigns:
                d = _campaign_to_dict(c)
                mine = [q for q in quests if q.campaign_id == c.id]
                d["quest_count"] = len(mine)
                d["done_count"] = sum(1 for q in mine if q.status == "done")
                d["xp"] = campaign_xp.get(c.id, 0)
                campaign_dicts.append(d)

            return {
                "campaigns": campaign_dicts,
                "quests": [_quest_to_dict(q, views) for q in quests],
                "stats": {
                    **engine.level_for(int(total_xp)),
                    "done_today": done_today,
                    "skills": sorted(
                        ({"skill": s, "xp": int(x or 0)} for s, x in skill_rows if int(x or 0) > 0),
                        key=lambda r: -r["xp"]),
                },
                "next": engine.recommend(list(views.values()), inactive_campaigns=inactive),
            }
        finally:
            db.close()

    # --- HISTORY (read-only) ---
    @router.get("/history")
    def history(request: Request, limit: int = 50):
        user = _owner(request)
        limit = min(200, max(1, int(limit)))
        db = SessionLocal()
        try:
            rows = _scoped(db.query(QuestEvent), QuestEvent, user).order_by(
                QuestEvent.created_at.desc()).limit(limit).all()
            titles = {q.id: q.title for q in _owner_quests(db, user)}
            out = []
            for e in rows:
                d = _event_to_dict(e)
                d["quest_title"] = titles.get(e.quest_id)
                out.append(d)
            return {"events": out}
        finally:
            db.close()

    # --- CAMPAIGNS ---
    @router.post("/campaigns")
    def create_campaign(request: Request, body: CampaignCreate):
        user = _owner(request)
        db = SessionLocal()
        try:
            c = QuestCampaign(
                id=str(uuid.uuid4()),
                owner=user,
                title=_clean_title(body.title),
                description=_clean_text(body.description, NOTES_MAX),
                status="active",
            )
            db.add(c)
            db.commit()
            db.refresh(c)
            return _campaign_to_dict(c)
        finally:
            db.close()

    @router.put("/campaigns/{campaign_id}")
    def update_campaign(request: Request, campaign_id: str, body: CampaignUpdate):
        user = _owner(request)
        db = SessionLocal()
        try:
            c = _get_campaign(db, user, campaign_id)
            if body.title is not None:
                c.title = _clean_title(body.title)
            if body.description is not None:
                c.description = _clean_text(body.description, NOTES_MAX)
            if body.status is not None:
                if body.status not in CAMPAIGN_STATUSES:
                    raise HTTPException(422, f"status must be one of {', '.join(CAMPAIGN_STATUSES)}")
                c.status = body.status
            if body.sort_order is not None:
                c.sort_order = body.sort_order
            db.commit()
            db.refresh(c)
            return _campaign_to_dict(c)
        finally:
            db.close()

    @router.delete("/campaigns/{campaign_id}")
    def delete_campaign(request: Request, campaign_id: str):
        user = _owner(request)
        db = SessionLocal()
        try:
            c = _get_campaign(db, user, campaign_id)
            if _scoped(db.query(Quest), Quest, user).filter(Quest.campaign_id == c.id).count():
                raise HTTPException(409, "Campaign still has quests; mark it done or move them first")
            db.delete(c)
            db.commit()
            return {"ok": True}
        finally:
            db.close()

    # --- QUESTS ---
    @router.post("")
    def create_quest(request: Request, body: QuestCreate):
        user = _owner(request)
        db = SessionLocal()
        try:
            if body.campaign_id:
                _get_campaign(db, user, body.campaign_id)
            deps = _validate_deps(db, user, None, body.depends_on or [])
            quest = Quest(
                id=str(uuid.uuid4()),
                owner=user,
                campaign_id=body.campaign_id or None,
                title=_clean_title(body.title),
                notes=_clean_text(body.notes, NOTES_MAX),
                status="todo",
                impact=engine.clamp_scale(body.impact),
                effort=engine.clamp_scale(body.effort),
                due_date=_clean_due(body.due_date),
                depends_on=json.dumps(deps) if deps else None,
                skill=_clean_text(body.skill, 60),
                is_boss=bool(body.is_boss),
            )
            db.add(quest)
            _log(db, user, quest, "created")
            db.commit()
            db.refresh(quest)
            return _quest_to_dict(quest, _views(_owner_quests(db, user)))
        finally:
            db.close()

    @router.put("/{quest_id}")
    def update_quest(request: Request, quest_id: str, body: QuestUpdate):
        user = _owner(request)
        db = SessionLocal()
        try:
            quest = _get_quest(db, user, quest_id)
            fields = body.model_dump(exclude_unset=True) if hasattr(body, "model_dump") else body.dict(exclude_unset=True)
            if "title" in fields:
                quest.title = _clean_title(body.title)
            if "campaign_id" in fields:
                if body.campaign_id:
                    _get_campaign(db, user, body.campaign_id)
                quest.campaign_id = body.campaign_id or None
            if "notes" in fields:
                quest.notes = _clean_text(body.notes, NOTES_MAX)
            if "impact" in fields:
                quest.impact = engine.clamp_scale(body.impact)
            if "effort" in fields:
                if quest.status == "done":
                    raise HTTPException(409, "Reopen the quest before changing its effort")
                quest.effort = engine.clamp_scale(body.effort)
            if "due_date" in fields:
                quest.due_date = _clean_due(body.due_date)
            if "depends_on" in fields:
                deps = _validate_deps(db, user, quest.id, body.depends_on or [])
                quest.depends_on = json.dumps(deps) if deps else None
            if "skill" in fields:
                quest.skill = _clean_text(body.skill, 60)
            if "is_boss" in fields:
                if quest.status == "done":
                    raise HTTPException(409, "Reopen the quest before changing boss status")
                quest.is_boss = bool(body.is_boss)
            db.commit()
            db.refresh(quest)
            return _quest_to_dict(quest, _views(_owner_quests(db, user)))
        finally:
            db.close()

    @router.delete("/{quest_id}")
    def delete_quest(request: Request, quest_id: str):
        user = _owner(request)
        db = SessionLocal()
        try:
            quest = _get_quest(db, user, quest_id)
            if quest.status == "done":
                raise HTTPException(409, "Completed quests are part of your history; reopen it first")
            # Free dependants so nothing stays blocked on a quest that no longer exists.
            for other in _owner_quests(db, user):
                deps = _deps(other)
                if quest.id in deps:
                    deps.remove(quest.id)
                    other.depends_on = json.dumps(deps) if deps else None
            _log(db, user, quest, "deleted", detail=quest.title)
            db.delete(quest)
            db.commit()
            return {"ok": True}
        finally:
            db.close()

    @router.post("/{quest_id}/start")
    def start_quest(request: Request, quest_id: str):
        user = _owner(request)
        db = SessionLocal()
        try:
            quest = _get_quest(db, user, quest_id)
            if quest.status != "todo":
                raise HTTPException(409, f"Quest is {quest.status}, not todo")
            _require_unblocked(db, user, quest)
            quest.status = "doing"
            _log(db, user, quest, "started")
            db.commit()
            db.refresh(quest)
            return _quest_to_dict(quest, _views(_owner_quests(db, user)))
        finally:
            db.close()

    @router.post("/{quest_id}/complete")
    def complete_quest(request: Request, quest_id: str, body: QuestComplete):
        user = _owner(request)
        outcome = _clean_text(body.outcome, OUTCOME_MAX)
        if not outcome:
            raise HTTPException(422, "Describe the outcome: what actually changed?")
        db = SessionLocal()
        try:
            quest = _get_quest(db, user, quest_id)
            if quest.status not in engine.OPEN_STATUSES:
                raise HTTPException(409, f"Quest is {quest.status}")
            _require_unblocked(db, user, quest)
            xp = engine.xp_for(quest.effort, bool(quest.is_boss))
            quest.status = "done"
            quest.outcome = outcome
            quest.completed_at = utcnow_naive()
            _log(db, user, quest, "completed", detail=outcome, xp=xp)
            db.commit()
            db.refresh(quest)
            return {"quest": _quest_to_dict(quest, _views(_owner_quests(db, user))), "xp_awarded": xp}
        finally:
            db.close()

    @router.post("/{quest_id}/reopen")
    def reopen_quest(request: Request, quest_id: str):
        user = _owner(request)
        db = SessionLocal()
        try:
            quest = _get_quest(db, user, quest_id)
            if quest.status not in ("done", "dropped"):
                raise HTTPException(409, "Only done or dropped quests can be reopened")
            # Reverse whatever XP this quest has netted so far, as a new event.
            net = _scoped(db.query(func.coalesce(func.sum(QuestEvent.xp_delta), 0)), QuestEvent, user).filter(
                QuestEvent.quest_id == quest.id).scalar() or 0
            quest.status = "todo"
            quest.completed_at = None
            quest.outcome = None
            _log(db, user, quest, "reopened", xp=-int(net))
            db.commit()
            db.refresh(quest)
            return _quest_to_dict(quest, _views(_owner_quests(db, user)))
        finally:
            db.close()

    @router.post("/{quest_id}/drop")
    def drop_quest(request: Request, quest_id: str):
        user = _owner(request)
        db = SessionLocal()
        try:
            quest = _get_quest(db, user, quest_id)
            if quest.status not in engine.OPEN_STATUSES:
                raise HTTPException(409, f"Quest is {quest.status}")
            quest.status = "dropped"
            _log(db, user, quest, "dropped")
            db.commit()
            db.refresh(quest)
            return _quest_to_dict(quest, _views(_owner_quests(db, user)))
        finally:
            db.close()

    return router
