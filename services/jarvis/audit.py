"""Action history: an append-only record of what Jarvis's tools did.

record_tool_call() is called for every tool execution (and for approval
requests raised before execution). It opens its own DB session and swallows
its own errors: auditing must never break or slow a tool run in a way the
user sees. Arguments are redacted and truncated before storage.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SUMMARY_MAX = 400
DETAIL_MAX = 300
_SECRET_KEY = re.compile(r"pass(word)?|secret|token|api[_-]?key|authorization|cookie|credential", re.I)
_SECRET_INLINE = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization)\b(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|\S+)")
STATUSES = ("ok", "error", "blocked", "approval_required")


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("[redacted]" if _SECRET_KEY.search(str(k)) else _redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    if isinstance(value, str):
        return _SECRET_INLINE.sub(r"\1\2[redacted]", value)
    return value


def summarize_args(content: Any) -> Optional[str]:
    if content is None:
        return None
    payload: Any = content
    if isinstance(content, str):
        raw = content.strip()
        if raw.startswith("{"):
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError):
                payload = raw
        else:
            payload = raw
    payload = _redact(payload)
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, default=str)
    text = " ".join(text.split())
    return text[:SUMMARY_MAX] + ("..." if len(text) > SUMMARY_MAX else "")


def status_of(result: Any) -> str:
    if not isinstance(result, dict):
        return "ok"
    if result.get("approval_required"):
        return "approval_required"
    if result.get("blocked"):
        return "blocked"
    if result.get("error") or result.get("exit_code") not in (None, 0) or result.get("success") is False:
        return "error"
    return "ok"


def _detail(result: Any, approved: bool) -> Optional[str]:
    parts = []
    if approved:
        parts.append("approved by user")
    if isinstance(result, dict):
        err = result.get("error")
        if err:
            parts.append(str(err))
    text = "; ".join(parts)
    return (_redact(text)[:DETAIL_MAX] or None) if text else None


def record_tool_call(owner: Optional[str], session_id: Optional[str], tool: Any, content: Any,
                     result: Any, duration_ms: Optional[int] = None, approved: bool = False) -> None:
    try:
        from core.database import SessionLocal, ActionLog
        from src.tool_capabilities import _action_from_content
        tool_name = str(tool or "unknown")[:100]
        try:
            action = _action_from_content(tool_name, content)
        except Exception:
            action = None
        db = SessionLocal()
        try:
            db.add(ActionLog(
                id=str(uuid.uuid4()),
                owner=owner or None,
                session_id=session_id,
                tool=tool_name,
                action=(action or None) and str(action)[:60],
                summary=summarize_args(content),
                status=status_of(result),
                detail=_detail(result, approved),
                duration_ms=duration_ms,
            ))
            db.commit()
        finally:
            db.close()
    except Exception as e:  # never let auditing break a tool run
        logger.warning("action log write failed for %s: %s", tool, e)


def entry_dict(e) -> Dict[str, Any]:
    return {
        "id": e.id, "session_id": e.session_id, "tool": e.tool, "action": e.action,
        "summary": e.summary, "status": e.status, "detail": e.detail,
        "duration_ms": e.duration_ms,
        "created_at": e.created_at.isoformat() if e.created_at else None,
    }


def list_actions(db, owner, limit: int = 100, status: Optional[str] = None,
                 tool: Optional[str] = None) -> List[Any]:
    from core.database import ActionLog
    q = db.query(ActionLog)
    if owner is not None:
        q = q.filter(ActionLog.owner == owner)
    if status:
        q = q.filter(ActionLog.status == status)
    if tool:
        q = q.filter(ActionLog.tool == tool)
    return q.order_by(ActionLog.created_at.desc()).limit(min(500, max(1, int(limit)))).all()


def counts(db, owner, since=None) -> Dict[str, int]:
    from sqlalchemy import func
    from core.database import ActionLog
    q = db.query(ActionLog.status, func.count(ActionLog.id))
    if owner is not None:
        q = q.filter(ActionLog.owner == owner)
    if since is not None:
        q = q.filter(ActionLog.created_at >= since)
    return {status: n for status, n in q.group_by(ActionLog.status).all()}
