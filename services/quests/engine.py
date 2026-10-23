"""Pure quest-log rules: XP, levels, dependencies and next-action ranking.

Nothing here touches the database or an LLM, so every number the UI shows
can be explained and tested. The rules deliberately reward finished,
evidenced work (a completed quest with an outcome) rather than activity:
there is no XP for creating, starting or editing quests.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, Iterable, List, Optional, Sequence

EFFORT_XP = {1: 10, 2: 25, 3: 50}
BOSS_MULTIPLIER = 3
LEVEL_STEP = 50  # level n starts at LEVEL_STEP * n * (n - 1) XP: 0, 100, 300, 600 ...

OPEN_STATUSES = ("todo", "doing")


def clamp_scale(value, default: int = 2) -> int:
    """Coerce an impact/effort value into the 1..3 scale."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return min(3, max(1, n))


def xp_for(effort, is_boss: bool) -> int:
    base = EFFORT_XP[clamp_scale(effort)]
    return base * (BOSS_MULTIPLIER if is_boss else 1)


def level_threshold(level: int) -> int:
    return LEVEL_STEP * level * (level - 1)


def level_for(total_xp: int) -> Dict[str, int]:
    """Level plus the XP bounds of the current level, for a progress bar."""
    xp = max(0, int(total_xp or 0))
    level = int((1 + math.sqrt(1 + 4 * xp / LEVEL_STEP)) // 2)
    # Guard float rounding at exact thresholds.
    while level_threshold(level + 1) <= xp:
        level += 1
    while level > 1 and level_threshold(level) > xp:
        level -= 1
    return {
        "level": level,
        "xp": xp,
        "level_floor": level_threshold(level),
        "next_level_xp": level_threshold(level + 1),
    }


def parse_day(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


@dataclass
class QuestView:
    """The fields the rules need, decoupled from the ORM row."""
    id: str
    title: str
    status: str = "todo"
    impact: int = 2
    effort: int = 2
    due_date: Optional[str] = None
    depends_on: Sequence[str] = field(default_factory=tuple)
    is_boss: bool = False
    campaign_id: Optional[str] = None
    created_order: int = 0


def blockers(quest: QuestView, by_id: Dict[str, QuestView]) -> List[QuestView]:
    """Prerequisites that are still open.

    A dropped prerequisite no longer blocks (otherwise its dependants would
    be stuck forever), and ids of deleted quests are ignored.
    """
    out = []
    for dep_id in quest.depends_on or ():
        dep = by_id.get(dep_id)
        if dep is not None and dep.status in OPEN_STATUSES:
            out.append(dep)
    return out


def would_create_cycle(quest_id: str, new_deps: Iterable[str], deps_of: Dict[str, Sequence[str]]) -> bool:
    """True if making quest_id depend on new_deps closes a loop."""
    stack = list(new_deps)
    seen = set()
    while stack:
        current = stack.pop()
        if current == quest_id:
            return True
        if current in seen:
            continue
        seen.add(current)
        stack.extend(deps_of.get(current, ()))
    return False


def _due_bonus(due: Optional[date], today: date):
    if due is None:
        return 0, None
    days = (due - today).days
    if days < 0:
        return 40, f"overdue by {-days} day{'s' if days != -1 else ''}"
    if days == 0:
        return 35, "due today"
    if days == 1:
        return 25, "due tomorrow"
    if days <= 3:
        return 15, f"due in {days} days"
    if days <= 7:
        return 8, "due this week"
    return 0, None


def recommend(
    quests: Sequence[QuestView],
    inactive_campaigns: Iterable[str] = (),
    today: Optional[date] = None,
    limit: int = 3,
) -> List[Dict]:
    """Rank open, unblocked quests and say why each one ranks where it does."""
    today = today or date.today()
    inactive = set(inactive_campaigns)
    by_id = {q.id: q for q in quests}

    unblocks: Dict[str, int] = {}
    for q in quests:
        if q.status in OPEN_STATUSES:
            for dep_id in q.depends_on or ():
                unblocks[dep_id] = unblocks.get(dep_id, 0) + 1

    ranked = []
    for q in quests:
        if q.status not in OPEN_STATUSES:
            continue
        if q.campaign_id and q.campaign_id in inactive:
            continue
        if blockers(q, by_id):
            continue

        impact = clamp_scale(q.impact)
        score = impact * 10
        reasons = [("high impact", "medium impact", "low impact")[3 - impact]]

        due_points, due_reason = _due_bonus(parse_day(q.due_date), today)
        score += due_points
        if due_reason:
            reasons.insert(0, due_reason)

        if q.status == "doing":
            score += 15
            reasons.append("already in progress")

        n_unblocks = unblocks.get(q.id, 0)
        if n_unblocks:
            score += min(15, 5 * n_unblocks)
            reasons.append(f"unblocks {n_unblocks} other quest{'s' if n_unblocks != 1 else ''}")

        if clamp_scale(q.effort) == 1:
            score += 5
            reasons.append("quick win")
        if q.is_boss:
            reasons.append("boss battle")

        due = parse_day(q.due_date)
        ranked.append((
            -score,
            due or date.max,
            q.created_order,
            {"quest_id": q.id, "title": q.title, "score": score, "reasons": reasons},
        ))

    ranked.sort(key=lambda item: item[:3])
    return [item[3] for item in ranked[:limit]]
