"""Daily Command Centre: today's plan, missions and what needs attention.

Missions are chosen once per day (or when energy/time changes) and stored,
so the list stays stable while you work through it instead of reshuffling
every time a quest is completed.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from core.database import DailyPlan
from services.quests import engine, store
from services.quests.store import QuestError

MINUTES_MAX = 16 * 60


def _today(today: Optional[date]) -> date:
    return today or date.today()


def get_plan(db, owner, day: date) -> Optional[DailyPlan]:
    q = db.query(DailyPlan).filter(DailyPlan.day == day.isoformat())
    q = q.filter(DailyPlan.owner == owner) if owner is not None else q
    return q.first()


def _inactive_campaigns(db, owner) -> List[str]:
    return [c.id for c in store.all_campaigns(db, owner) if (c.status or "active") != "active"]


def plan_day(db, owner, energy: Optional[str] = None, minutes: Optional[int] = None,
             today: Optional[date] = None) -> DailyPlan:
    """Create or refresh today's plan and (re)pick missions."""
    day = _today(today)
    if energy is not None and energy not in engine.ENERGY_LEVELS:
        raise QuestError(422, f"energy must be one of {', '.join(engine.ENERGY_LEVELS)}")
    if minutes is not None:
        try:
            minutes = int(minutes)
        except (TypeError, ValueError):
            raise QuestError(422, "minutes must be a number")
        if minutes < 0 or minutes > MINUTES_MAX:
            raise QuestError(422, "minutes must be between 0 and 960")
    plan = get_plan(db, owner, day)
    if plan is None:
        plan = DailyPlan(id=str(uuid.uuid4()), owner=owner, day=day.isoformat(), energy="normal")
        db.add(plan)
    if energy is not None:
        plan.energy = energy
    if minutes is not None:
        plan.minutes_available = minutes or None
    views = store.views_for(store.all_quests(db, owner))
    missions = engine.pick_missions(
        list(views.values()),
        inactive_campaigns=_inactive_campaigns(db, owner),
        minutes=plan.minutes_available,
        energy=plan.energy or "normal",
        today=day,
    )
    plan.missions = json.dumps([m["quest_id"] for m in missions])
    db.flush()
    return plan


def set_missions(db, owner, quest_ids: List[str], today: Optional[date] = None) -> DailyPlan:
    """Let the user choose today's missions by hand (max 5, own quests only)."""
    ids = list(dict.fromkeys(i for i in quest_ids if isinstance(i, str)))[:5]
    for qid in ids:
        store.get_quest(db, owner, qid)
    plan = get_plan(db, owner, _today(today)) or plan_day(db, owner, today=today)
    plan.missions = json.dumps(ids)
    db.flush()
    return plan


def _mission_state(q: Dict[str, Any]) -> str:
    if q["status"] == "done":
        return "done"
    if q["status"] == "dropped":
        return "dropped"
    if q["blocked_by"]:
        return "locked"
    return "in_progress" if q["status"] == "doing" else "ready"


def today_view(db, owner, today: Optional[date] = None, ledger_pending: int = 0) -> Dict[str, Any]:
    day = _today(today)
    plan = get_plan(db, owner, day)
    if plan is None:
        plan = plan_day(db, owner, today=day)
        db.commit()

    board = store.board(db, owner, today=day)
    quests = {q["id"]: q for q in board["quests"]}
    campaigns = {c["id"]: c for c in board["campaigns"]}

    try:
        mission_ids = json.loads(plan.missions or "[]")
    except (json.JSONDecodeError, TypeError):
        mission_ids = []
    views = store.views_for(store.all_quests(db, owner))
    missions = []
    for qid in mission_ids:
        q = quests.get(qid)
        if not q:
            continue
        state = _mission_state(q)
        if state == "dropped":
            continue
        missions.append({
            **q,
            "state": state,
            "est_minutes_effective": engine.minutes_for(views[qid]) if qid in views else None,
            "campaign_title": campaigns.get(q["campaign_id"], {}).get("title"),
        })

    open_quests = [q for q in board["quests"] if q["status"] in engine.OPEN_STATUSES]
    overdue, due_soon = [], []
    for q in open_quests:
        due = engine.parse_day(q["due_date"])
        if due is None:
            continue
        if due < day:
            overdue.append(q)
        elif due <= day + timedelta(days=3):
            due_soon.append(q)
    blocked = [q for q in open_quests if q["blocked_by"]]

    focus = None
    if board["next"]:
        top = quests.get(board["next"][0]["quest_id"])
        if top and top["campaign_id"] in campaigns:
            focus = campaigns[top["campaign_id"]]

    planned = sum(m["est_minutes_effective"] or 0 for m in missions if m["state"] != "done")
    return {
        "date": day.isoformat(),
        "plan": {
            "energy": plan.energy or "normal",
            "minutes_available": plan.minutes_available,
            "minutes_planned": planned,
        },
        "missions": missions,
        "next": board["next"],
        "overdue": overdue,
        "due_soon": due_soon,
        "blocked": blocked,
        "focus_campaign": focus,
        "stats": board["stats"],
        "ledger_pending": ledger_pending,
    }
