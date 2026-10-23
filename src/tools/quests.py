"""Quest-log tool for the agent (manage_quests).

Lets Jarvis turn conversation into campaigns and quests, report what to do
next, and record progress the user reports. Shares every rule with the
/api/quests routes through services.quests.store.

Deliberately missing: delete. The agent can drop a quest (reversible); hard
deletes stay in the UI. Completing requires an outcome describing what the
user said actually changed, so XP can't be granted on the model's own say-so.
"""
import logging
from typing import Any, Dict, List, Optional

from src.tools._common import _parse_tool_args

logger = logging.getLogger(__name__)

_ACTION_ALIASES = {
    "list": "board", "overview": "board", "status": "board", "show": "board", "view": "board",
    "what_next": "next", "suggest": "next", "recommend": "next",
    "create": "add_quest", "add": "add_quest", "new": "add_quest", "create_quest": "add_quest",
    "new_quest": "add_quest",
    "create_campaign": "add_campaign", "new_campaign": "add_campaign",
    "edit": "update", "update_quest": "update", "edit_quest": "update",
    "begin": "start", "done": "complete", "finish": "complete",
    "abandon": "drop", "cancel": "drop", "undo": "reopen",
    "edit_campaign": "update_campaign", "log": "history",
}

_STATUS_MARK = {"todo": " ", "doing": "~", "done": "x", "dropped": "-"}
_EFFORT_WORD = {1: "S", 2: "M", 3: "L"}
_SCALE_WORDS = {"low": 1, "small": 1, "s": 1, "medium": 2, "med": 2, "m": 2,
                "high": 3, "large": 3, "big": 3, "l": 3}


def _scale(value: Any) -> Any:
    if isinstance(value, str) and value.strip().casefold() in _SCALE_WORDS:
        return _SCALE_WORDS[value.strip().casefold()]
    return value


def _due(value: Any) -> Optional[str]:
    """Accept YYYY-MM-DD or natural language ('next friday') in the user's tz."""
    from services.quests import engine
    if value in (None, ""):
        return None
    text = str(value).strip()
    if engine.parse_day(text):
        return text[:10]
    try:
        from routes.calendar_routes import parse_due_for_user
        parsed = parse_due_for_user(text)
        if parsed and engine.parse_day(parsed):
            return parsed[:10]
    except Exception as e:  # pragma: no cover - parser is optional here
        logger.debug("manage_quests due parse failed for %r: %s", text, e)
    return text  # let store validation produce the error message


def _refs(value: Any) -> List[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value if v not in (None, "")]


def _quest_fields(spec: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    for key in ("title", "notes", "skill"):
        if key in spec:
            out[key] = spec[key]
    if "description" in spec and "notes" not in spec:
        out["notes"] = spec["description"]
    for key in ("impact", "effort"):
        if key in spec:
            out[key] = _scale(spec[key])
    if "due_date" in spec or "due" in spec:
        out["due_date"] = _due(spec.get("due_date", spec.get("due")))
    if "is_boss" in spec or "boss" in spec:
        out["is_boss"] = bool(spec.get("is_boss", spec.get("boss")))
    return out


def _line(q: Dict[str, Any]) -> str:
    bits = [f"{_EFFORT_WORD.get(q['effort'], 'M')} {q['xp_value']}xp"]
    if q["is_boss"]:
        bits.append("BOSS")
    if q["skill"]:
        bits.append(q["skill"])
    if q["due_date"] and q["status"] != "done":
        bits.append(f"due {q['due_date']}")
    if q["blocked_by"] and q["status"] in ("todo", "doing"):
        bits.append("blocked by " + ", ".join(b["title"] for b in q["blocked_by"]))
    return f"  [{_STATUS_MARK.get(q['status'], '?')}] {q['title']} [{q['id'][:8]}] ({'; '.join(bits)})"


def _format_next(board: Dict[str, Any]) -> str:
    if not board["next"]:
        return "Next up: nothing available (no open, unblocked quests in active campaigns)."
    lines = ["Next up:"]
    for i, rec in enumerate(board["next"], 1):
        lines.append(f"  {i}. {rec['title']} [{rec['quest_id'][:8]}] - {', '.join(rec['reasons'])}")
    return "\n".join(lines)


def _format_board(board: Dict[str, Any], include_finished: bool = False) -> str:
    s = board["stats"]
    lines = [f"Level {s['level']} - {s['xp']}/{s['next_level_xp']} XP, {s['done_today']} done today.",
             _format_next(board), ""]
    by_campaign: Dict[Optional[str], List[Dict]] = {}
    for q in board["quests"]:
        by_campaign.setdefault(q["campaign_id"], []).append(q)
    groups = [(c, by_campaign.get(c["id"], [])) for c in board["campaigns"]]
    if by_campaign.get(None):
        groups.append(({"title": "Side quests", "status": "active", "done_count": None}, by_campaign[None]))
    if not groups:
        lines.append("No campaigns or quests yet.")
    for c, quests in groups:
        shown = [q for q in quests if include_finished or q["status"] in ("todo", "doing")]
        head = f"Campaign: {c['title']}"
        if c.get("id"):
            head += f" [{c['id'][:8]}] ({c['status']}, {c['done_count']}/{c['quest_count']} done, {c['xp']} XP)"
        lines.append(head)
        lines.extend(_line(q) for q in shown)
        hidden = len(quests) - len(shown)
        if not shown:
            lines.append("  (no open quests)")
        if hidden:
            lines.append(f"  ...plus {hidden} finished")
    return "\n".join(lines)


def _resolve_after(db, owner, store, refs: List[str], batch: Dict[str, str]) -> List[str]:
    ids = []
    for ref in refs:
        key = " ".join(ref.casefold().split())
        if key in batch:
            ids.append(batch[key])
        else:
            ids.append(store.find_quest(db, owner, ref).id)
    return ids


async def do_manage_quests(content: Any, owner: Optional[str] = None) -> Dict:
    """Handle manage_quests tool calls."""
    from core.database import SessionLocal
    from services.quests import store
    from services.quests.store import QuestError

    try:
        args = _parse_tool_args(content)
    except ValueError:
        return {"error": "Invalid JSON arguments", "exit_code": 1}

    raw = (args.get("action") or "board").replace("-", "_").strip().lower()
    action = _ACTION_ALIASES.get(raw, raw)
    owner = owner or None
    db = SessionLocal()
    try:
        if action == "board":
            b = store.board(db, owner)
            return {"results": _format_board(b, bool(args.get("include_finished"))), "exit_code": 0}

        if action == "next":
            return {"results": _format_next(store.board(db, owner)), "exit_code": 0}

        if action == "history":
            events = store.history(db, owner, int(args.get("limit") or 15))
            if not events:
                return {"results": "No quest history yet.", "exit_code": 0}
            lines = []
            for e in events:
                title = e["quest_title"] or e["detail"] or "(removed quest)"
                xp = f" {e['xp_delta']:+d} XP" if e["xp_delta"] else ""
                lines.append(f"- {e['created_at'][:16]} {e['kind']}: {title}{xp}")
            return {"results": "\n".join(lines), "exit_code": 0}

        if action == "add_campaign":
            c = store.create_campaign(db, owner, args.get("title") or args.get("campaign"),
                                      args.get("description"))
            db.commit()
            return {"results": f"Created campaign '{c.title}' [{c.id[:8]}].", "exit_code": 0}

        if action == "update_campaign":
            c = store.find_campaign(db, owner, args.get("campaign") or args.get("id") or "")
            if not c:
                return {"error": "Campaign not found", "exit_code": 1}
            fields = {k: args[k] for k in ("title", "description", "status") if k in args}
            c = store.update_campaign(db, owner, c.id, fields)
            db.commit()
            return {"results": f"Campaign '{c.title}' is now {c.status}.", "exit_code": 0}

        if action in ("plan", "add_quest"):
            notes = []
            campaign = None
            campaign_ref = args.get("campaign") or args.get("campaign_id")
            if campaign_ref:
                campaign = store.find_campaign(db, owner, campaign_ref)
                if not campaign:
                    campaign = store.create_campaign(db, owner, campaign_ref, args.get("campaign_description"))
                    notes.append(f"Created campaign '{campaign.title}' [{campaign.id[:8]}].")
            specs = args.get("quests") if action == "plan" else [args]
            if not isinstance(specs, list) or not specs:
                return {"error": "plan needs a non-empty 'quests' list", "exit_code": 1}
            # Open quests already in this campaign, so re-planning the same goal
            # doesn't duplicate work (and the model needn't read the board first).
            campaign_id = campaign.id if campaign else None
            existing = {
                " ".join(q.title.casefold().split()): q
                for q in store.all_quests(db, owner)
                if q.campaign_id == campaign_id and q.status in ("todo", "doing")
            }
            batch: Dict[str, str] = {}
            created, skipped = [], []
            for spec in specs:
                if isinstance(spec, str):
                    spec = {"title": spec}
                if not isinstance(spec, dict):
                    continue
                key = " ".join(str(spec.get("title") or "").casefold().split())
                if key and key in existing:
                    batch[key] = existing[key].id
                    skipped.append(existing[key].title)
                    continue
                fields = _quest_fields(spec)
                fields["campaign_id"] = campaign_id
                after = _refs(spec.get("after") or spec.get("depends_on") or spec.get("requires"))
                fields["depends_on"] = _resolve_after(db, owner, store, after, batch)
                quest = store.create_quest(db, owner, fields)
                batch[" ".join(quest.title.casefold().split())] = quest.id
                created.append(quest)
            db.commit()
            views = store.views_for(store.all_quests(db, owner))
            lines = notes + [f"Added {len(created)} quest{'s' if len(created) != 1 else ''}:"]
            lines += [_line(store.quest_dict(q, views)) for q in created]
            if skipped:
                lines.append("Already on the board (skipped): " + "; ".join(skipped))
            return {"results": "\n".join(lines), "exit_code": 0}

        # Everything below targets one existing quest.
        ref = args.get("quest") or args.get("id") or args.get("title") or ""
        if action == "update":
            ref = args.get("quest") or args.get("id") or ""
            quest = store.find_quest(db, owner, ref)
            fields = _quest_fields(args)
            if "new_title" in args:
                fields["title"] = args["new_title"]
            if "campaign" in args:
                c = store.find_campaign(db, owner, args["campaign"]) if args["campaign"] else None
                if args["campaign"] and not c:
                    return {"error": f"Campaign '{args['campaign']}' not found", "exit_code": 1}
                fields["campaign_id"] = c.id if c else None
            if any(k in args for k in ("after", "depends_on", "requires")):
                refs = _refs(args.get("after") or args.get("depends_on") or args.get("requires"))
                fields["depends_on"] = _resolve_after(db, owner, store, refs, {})
            quest = store.update_quest(db, owner, quest.id, fields)
            db.commit()
            views = store.views_for(store.all_quests(db, owner))
            return {"results": "Updated:\n" + _line(store.quest_dict(quest, views)), "exit_code": 0}

        if action == "start":
            quest = store.start_quest(db, owner, store.find_quest(db, owner, ref, include_finished=False).id)
            db.commit()
            return {"results": f"Started '{quest.title}'.", "exit_code": 0}

        if action == "complete":
            quest = store.find_quest(db, owner, ref, include_finished=False)
            quest, xp = store.complete_quest(db, owner, quest.id, args.get("outcome"))
            db.commit()
            level = store.board(db, owner)["stats"]
            return {"results": f"Completed '{quest.title}' (+{xp} XP). Now level {level['level']}, "
                               f"{level['xp']}/{level['next_level_xp']} XP.", "exit_code": 0}

        if action == "drop":
            quest = store.drop_quest(db, owner, store.find_quest(db, owner, ref, include_finished=False).id)
            db.commit()
            return {"results": f"Dropped '{quest.title}'. It can be reopened.", "exit_code": 0}

        if action == "reopen":
            quest = store.reopen_quest(db, owner, store.find_quest(db, owner, ref).id)
            db.commit()
            return {"results": f"Reopened '{quest.title}' (any XP it earned was reversed).", "exit_code": 0}

        return {"error": f"Unknown action '{raw}'. Use board, next, plan, add_quest, add_campaign, "
                         "update, start, complete, drop, reopen, update_campaign or history.",
                "exit_code": 1}
    except QuestError as e:
        db.rollback()
        return {"error": e.message, "exit_code": 1}
    except Exception as e:
        db.rollback()
        logger.error("manage_quests error: %s", e)
        return {"error": f"manage_quests failed: {e}", "exit_code": 1}
    finally:
        db.close()
