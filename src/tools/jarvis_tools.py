"""JARVIS agent tools: manage_ledger (propose/search only) and system_status.

manage_ledger deliberately has no approve/reject/edit/delete: proposals land
in the Review Inbox (/ledger) and only the user's review turns them into
approved Life Ledger entries.
"""
import logging
from typing import Any, Dict, Optional

from src.tools._common import _parse_tool_args

logger = logging.getLogger(__name__)

_LEDGER_ALIASES = {
    "add": "propose", "remember": "propose", "save": "propose", "record": "propose",
    "suggest": "propose", "create": "propose",
    "find": "search", "recall": "search", "lookup": "search", "query": "search", "list": "search",
}


async def do_manage_ledger(content: Any, owner: Optional[str] = None) -> Dict:
    from core.database import SessionLocal
    from services.jarvis import ledger
    from services.quests import store as quest_store
    from services.quests.store import QuestError

    try:
        args = _parse_tool_args(content)
    except ValueError:
        return {"error": "Invalid JSON arguments", "exit_code": 1}
    raw = (args.get("action") or "search").replace("-", "_").strip().lower()
    action = _LEDGER_ALIASES.get(raw, raw)
    owner = owner or None
    db = SessionLocal()
    try:
        if action == "search":
            rows = ledger.search(db, owner, args.get("query") or "", limit=int(args.get("limit") or 8))
            if not rows:
                return {"results": "No approved Life Ledger entries match.", "exit_code": 0}
            lines = [f"- ({e.kind}) {e.text} [{e.id[:8]}]" + (f" (until {e.relevant_until})" if e.relevant_until else "")
                     for e in rows]
            return {"results": "Approved Life Ledger entries:\n" + "\n".join(lines), "exit_code": 0}

        if action == "propose":
            campaign_id = quest_id = None
            if args.get("campaign"):
                c = quest_store.find_campaign(db, owner, args["campaign"])
                campaign_id = c.id if c else None
            if args.get("quest"):
                try:
                    quest_id = quest_store.find_quest(db, owner, args["quest"]).id
                except QuestError:
                    quest_id = None
            supersedes = None
            if args.get("supersedes"):
                matches = ledger.search(db, owner, args["supersedes"], limit=1)
                supersedes = matches[0].id if matches else None
            e, created = ledger.propose(
                db, owner, args.get("text") or "",
                kind=args.get("kind") or "fact", source=args.get("source") or "jarvis",
                source_ref=args.get("source_ref"), confidence=args.get("confidence") or "medium",
                campaign_id=campaign_id, quest_id=quest_id,
                relevant_until=args.get("relevant_until"), supersedes_id=supersedes,
            )
            db.commit()
            if not created:
                return {"results": f"Already in the Life Ledger ({e.status}): {e.text}", "exit_code": 0}
            note = " It would replace an older entry once approved." if supersedes else ""
            return {"results": f"Proposed for review ({e.kind}): {e.text}.{note} "
                               "Sam approves or rejects it in the Review Inbox (/ledger); "
                               "it is not a confirmed fact until then.", "exit_code": 0}

        return {"error": f"Unknown action '{raw}'. Use propose or search. "
                         "Approving entries is only possible in the Review Inbox.", "exit_code": 1}
    except QuestError as e:
        db.rollback()
        return {"error": e.message, "exit_code": 1}
    except Exception as e:
        db.rollback()
        logger.error("manage_ledger error: %s", e)
        return {"error": f"manage_ledger failed: {e}", "exit_code": 1}
    finally:
        db.close()


async def do_system_status(content: Any = None, owner: Optional[str] = None) -> Dict:
    import asyncio
    from services.jarvis import health
    try:
        snap = await asyncio.to_thread(health.snapshot, owner)
    except Exception as e:
        logger.error("system_status error: %s", e)
        return {"error": f"system_status failed: {e}", "exit_code": 1}
    return {"results": health.as_text(snap), "exit_code": 0}
