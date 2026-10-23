"""Permission Centre read model: what Jarvis may do, and what it recently tried.

Everything here is derived from the code paths that actually enforce policy
(settings.disabled_tools, tool_capabilities, cloud_privacy) so the page can't
drift from reality. Toggling tools reuses the existing admin /tools route.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


def snapshot(db, owner: Optional[str]) -> Dict[str, Any]:
    from src.settings import get_setting
    from src.tool_capabilities import (
        _POST_EXTERNAL_LOW_RISK_ACTIONS, _PRIVATE_ACTION_READS, _PRIVATE_ACTION_WRITES,
        _ACTION_DESTRUCTIVE,
    )
    from src.cloud_privacy import PRIVATE_DATA_TOOL_NAMES, guard_enabled
    from services.jarvis import audit

    try:
        from src.agent_tools import TOOL_TAGS
        all_tools = sorted(TOOL_TAGS)
    except Exception:
        all_tools = []
    disabled = sorted(set(get_setting("disabled_tools", []) or []))

    gated = {}
    for tool, actions in _PRIVATE_ACTION_WRITES.items():
        free = _POST_EXTERNAL_LOW_RISK_ACTIONS.get(tool, frozenset())
        gated[tool] = {
            "reads": sorted(_PRIVATE_ACTION_READS.get(tool, ())),
            "writes": sorted(actions),
            "destructive": sorted(_ACTION_DESTRUCTIVE.get(tool, ())),
            "no_prompt": sorted(free),
        }

    recent = audit.list_actions(db, owner, limit=40)
    attention = [audit.entry_dict(e) for e in recent if e.status in ("approval_required", "blocked", "error")]
    return {
        "tools": [{"id": t, "enabled": t not in disabled} for t in all_tools],
        "disabled": disabled,
        "approval_policy": {
            "summary": (
                "Private reads and writes run freely until untrusted content (web pages, "
                "emails, recalled memories, skills) is in the conversation. After that, "
                "they need your approval, except the actions listed under 'Runs without asking'."
            ),
            "tools": gated,
        },
        "cloud_private_tools": sorted(PRIVATE_DATA_TOOL_NAMES),
        "cloud_privacy_guard": bool(guard_enabled()),
        "recent_attention": attention[:20],
        "counts_recent": audit.counts(db, owner),
    }
