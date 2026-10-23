# routes/jarvis/jarvis_routes.py
"""JARVIS API: daily command centre, Life Ledger review, resources,
action history, permissions and system health.

Ledger review (approve/reject/merge/correct) exists only here, behind the
user's session - the agent tool can only propose - so Jarvis cannot approve
its own memories. Action history is read-only.
"""

import logging
from contextlib import contextmanager
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from core.database import SessionLocal
from src.auth_helpers import require_user
from services.quests import store as quest_store
from services.quests.store import QuestError
from services.jarvis import audit, ledger, resources, today as today_svc

logger = logging.getLogger(__name__)


class TodayUpdate(BaseModel):
    energy: Optional[str] = None
    minutes_available: Optional[int] = None


class MissionsUpdate(BaseModel):
    quest_ids: List[str]


class Capture(BaseModel):
    text: str
    kind: str = "quest"          # quest | ledger
    campaign_id: Optional[str] = None


class LedgerAdd(BaseModel):
    text: str
    kind: str = "fact"
    campaign_id: Optional[str] = None
    quest_id: Optional[str] = None
    relevant_until: Optional[str] = None
    source_ref: Optional[str] = None


class LedgerReview(BaseModel):
    decision: str                # approve | reject
    text: Optional[str] = None
    kind: Optional[str] = None
    relevant_until: Optional[str] = None


class LedgerMerge(BaseModel):
    into_id: str
    text: Optional[str] = None


class LedgerCorrect(BaseModel):
    text: str
    kind: Optional[str] = None


class ResourceIn(BaseModel):
    kind: Optional[str] = None
    name: Optional[str] = None
    amount: Optional[float] = None
    target: Optional[float] = None
    unit: Optional[str] = None
    notes: Optional[str] = None
    campaign_id: Optional[str] = None


def _fields(body: BaseModel) -> dict:
    return body.model_dump(exclude_unset=True) if hasattr(body, "model_dump") else body.dict(exclude_unset=True)


def setup_jarvis_routes():
    router = APIRouter(prefix="/api/jarvis", tags=["jarvis"])

    def _owner(request: Request) -> Optional[str]:
        return require_user(request) or None

    @contextmanager
    def _session():
        db = SessionLocal()
        try:
            yield db
        except QuestError as e:
            db.rollback()
            raise HTTPException(e.status, e.message)
        finally:
            db.close()

    # --- Command centre ---
    @router.get("/today")
    def get_today(request: Request):
        owner = _owner(request)
        with _session() as db:
            return today_svc.today_view(db, owner, ledger_pending=ledger.pending_count(db, owner))

    @router.post("/today")
    def update_today(request: Request, body: TodayUpdate):
        owner = _owner(request)
        with _session() as db:
            today_svc.plan_day(db, owner, energy=body.energy, minutes=body.minutes_available)
            db.commit()
            return today_svc.today_view(db, owner, ledger_pending=ledger.pending_count(db, owner))

    @router.put("/today/missions")
    def set_missions(request: Request, body: MissionsUpdate):
        owner = _owner(request)
        with _session() as db:
            today_svc.set_missions(db, owner, body.quest_ids)
            db.commit()
            return today_svc.today_view(db, owner, ledger_pending=ledger.pending_count(db, owner))

    @router.post("/capture")
    def capture(request: Request, body: Capture):
        """Quick capture: a thought becomes a side quest or a ledger candidate."""
        owner = _owner(request)
        with _session() as db:
            if body.kind == "ledger":
                e, created = ledger.propose(db, owner, body.text, kind="fact", source="user",
                                            source_ref="quick capture")
                db.commit()
                return {"type": "ledger", "created": created, "entry": ledger.entry_dict(e)}
            if body.kind != "quest":
                raise HTTPException(422, "kind must be quest or ledger")
            q = quest_store.create_quest(db, owner, {
                "title": body.text, "campaign_id": body.campaign_id, "notes": "Captured from the command centre",
            })
            db.commit()
            views = quest_store.views_for(quest_store.all_quests(db, owner))
            return {"type": "quest", "quest": quest_store.quest_dict(q, views)}

    # --- Life Ledger ---
    @router.get("/ledger")
    def list_ledger(request: Request, status: Optional[str] = None):
        owner = _owner(request)
        with _session() as db:
            rows = ledger.list_entries(db, owner, status=status)
            out = []
            for e in rows:
                d = ledger.entry_dict(e)
                if e.status == "pending":
                    d["conflicts"] = ledger.conflicts_for(db, owner, e.text, exclude_id=e.id)
                out.append(d)
            return {"entries": out, "pending": ledger.pending_count(db, owner)}

    @router.post("/ledger")
    def add_ledger(request: Request, body: LedgerAdd):
        """The user writing an entry themselves: approved immediately."""
        owner = _owner(request)
        with _session() as db:
            e, _created = ledger.propose(
                db, owner, body.text, kind=body.kind, source="user",
                source_ref=body.source_ref, confidence="high", campaign_id=body.campaign_id,
                quest_id=body.quest_id, relevant_until=body.relevant_until)
            if e.status == "pending":
                e = ledger.review(db, owner, e.id, "approve")
            db.commit()
            return ledger.entry_dict(e)

    @router.post("/ledger/{entry_id}/review")
    def review_ledger(request: Request, entry_id: str, body: LedgerReview):
        owner = _owner(request)
        with _session() as db:
            e = ledger.review(db, owner, entry_id, body.decision, _fields(body))
            db.commit()
            return ledger.entry_dict(e)

    @router.post("/ledger/{entry_id}/merge")
    def merge_ledger(request: Request, entry_id: str, body: LedgerMerge):
        owner = _owner(request)
        with _session() as db:
            e = ledger.merge(db, owner, entry_id, body.into_id, body.text)
            db.commit()
            return ledger.entry_dict(e)

    @router.post("/ledger/{entry_id}/correct")
    def correct_ledger(request: Request, entry_id: str, body: LedgerCorrect):
        owner = _owner(request)
        with _session() as db:
            e = ledger.correct(db, owner, entry_id, body.text, body.kind)
            db.commit()
            return ledger.entry_dict(e)

    # --- Resources ---
    @router.get("/resources")
    def list_resources(request: Request):
        owner = _owner(request)
        with _session() as db:
            return resources.summary(db, owner)

    @router.post("/resources")
    def add_resource(request: Request, body: ResourceIn):
        owner = _owner(request)
        with _session() as db:
            r = resources.create(db, owner, _fields(body))
            db.commit()
            return resources.resource_dict(r)

    @router.put("/resources/{resource_id}")
    def update_resource(request: Request, resource_id: str, body: ResourceIn):
        owner = _owner(request)
        with _session() as db:
            r = resources.update(db, owner, resource_id, _fields(body))
            db.commit()
            return resources.resource_dict(r)

    @router.delete("/resources/{resource_id}")
    def delete_resource(request: Request, resource_id: str):
        owner = _owner(request)
        with _session() as db:
            resources.delete(db, owner, resource_id)
            db.commit()
            return {"ok": True}

    # --- Action history, permissions, health (read-only) ---
    @router.get("/actions")
    def list_actions(request: Request, limit: int = 100, status: Optional[str] = None,
                     tool: Optional[str] = None):
        owner = _owner(request)
        if status and status not in audit.STATUSES:
            raise HTTPException(422, f"status must be one of {', '.join(audit.STATUSES)}")
        with _session() as db:
            rows = audit.list_actions(db, owner, limit=limit, status=status, tool=tool)
            return {"actions": [audit.entry_dict(e) for e in rows]}

    @router.get("/permissions")
    def permissions(request: Request):
        owner = _owner(request)
        from services.jarvis import permissions as perms
        with _session() as db:
            return perms.snapshot(db, owner)

    @router.get("/health")
    def health(request: Request):
        owner = _owner(request)
        from services.jarvis import health as health_svc
        return health_svc.snapshot(owner)

    return router
