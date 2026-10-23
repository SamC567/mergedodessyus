# routes/quests/quest_routes.py
"""Quest log API: campaigns, quests, append-only progress history.

Thin HTTP layer over services.quests.store, which also backs Jarvis's
manage_quests tool. XP is derived from QuestEvent rows, never stored as an
editable score; completing a quest requires an outcome. There are no routes
that update or delete events.
"""

import logging
from contextlib import contextmanager
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from core.database import SessionLocal
from src.auth_helpers import require_user
from services.quests import store
from services.quests.store import QuestError

logger = logging.getLogger(__name__)


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


def _fields(body: BaseModel, exclude_unset: bool = True) -> dict:
    if hasattr(body, "model_dump"):
        return body.model_dump(exclude_unset=exclude_unset)
    return body.dict(exclude_unset=exclude_unset)


def setup_quest_routes():
    router = APIRouter(prefix="/api/quests", tags=["quests"])

    def _owner(request: Request) -> Optional[str]:
        # Same fail-closed policy as notes: no identity under configured
        # auth is a 401, never single-user blanket access.
        return require_user(request) or None

    @contextmanager
    def _session():
        # SessionLocal is looked up at call time so tests can swap it.
        db = SessionLocal()
        try:
            yield db
        except QuestError as e:
            db.rollback()
            raise HTTPException(e.status, e.message)
        finally:
            db.close()

    def _quest_out(db, owner, quest):
        return store.quest_dict(quest, store.views_for(store.all_quests(db, owner)))

    # --- BOARD / HISTORY (read-only) ---
    @router.get("/board")
    def board(request: Request):
        owner = _owner(request)
        with _session() as db:
            return store.board(db, owner)

    @router.get("/history")
    def history(request: Request, limit: int = 50):
        owner = _owner(request)
        with _session() as db:
            return {"events": store.history(db, owner, limit)}

    # --- CAMPAIGNS ---
    @router.post("/campaigns")
    def create_campaign(request: Request, body: CampaignCreate):
        owner = _owner(request)
        with _session() as db:
            c = store.create_campaign(db, owner, body.title, body.description)
            db.commit()
            return store.campaign_dict(c)

    @router.put("/campaigns/{campaign_id}")
    def update_campaign(request: Request, campaign_id: str, body: CampaignUpdate):
        owner = _owner(request)
        with _session() as db:
            c = store.update_campaign(db, owner, campaign_id, _fields(body))
            db.commit()
            return store.campaign_dict(c)

    @router.delete("/campaigns/{campaign_id}")
    def delete_campaign(request: Request, campaign_id: str):
        owner = _owner(request)
        with _session() as db:
            store.delete_campaign(db, owner, campaign_id)
            db.commit()
            return {"ok": True}

    # --- QUESTS ---
    @router.post("")
    def create_quest(request: Request, body: QuestCreate):
        owner = _owner(request)
        with _session() as db:
            quest = store.create_quest(db, owner, _fields(body, exclude_unset=False))
            db.commit()
            return _quest_out(db, owner, quest)

    @router.put("/{quest_id}")
    def update_quest(request: Request, quest_id: str, body: QuestUpdate):
        owner = _owner(request)
        with _session() as db:
            quest = store.update_quest(db, owner, quest_id, _fields(body))
            db.commit()
            return _quest_out(db, owner, quest)

    @router.delete("/{quest_id}")
    def delete_quest(request: Request, quest_id: str):
        owner = _owner(request)
        with _session() as db:
            store.delete_quest(db, owner, quest_id)
            db.commit()
            return {"ok": True}

    @router.post("/{quest_id}/start")
    def start_quest(request: Request, quest_id: str):
        owner = _owner(request)
        with _session() as db:
            quest = store.start_quest(db, owner, quest_id)
            db.commit()
            return _quest_out(db, owner, quest)

    @router.post("/{quest_id}/complete")
    def complete_quest(request: Request, quest_id: str, body: QuestComplete):
        owner = _owner(request)
        with _session() as db:
            quest, xp = store.complete_quest(db, owner, quest_id, body.outcome)
            db.commit()
            return {"quest": _quest_out(db, owner, quest), "xp_awarded": xp}

    @router.post("/{quest_id}/reopen")
    def reopen_quest(request: Request, quest_id: str):
        owner = _owner(request)
        with _session() as db:
            quest = store.reopen_quest(db, owner, quest_id)
            db.commit()
            return _quest_out(db, owner, quest)

    @router.post("/{quest_id}/drop")
    def drop_quest(request: Request, quest_id: str):
        owner = _owner(request)
        with _session() as db:
            quest = store.drop_quest(db, owner, quest_id)
            db.commit()
            return _quest_out(db, owner, quest)

    return router
