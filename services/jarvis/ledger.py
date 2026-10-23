"""Life Ledger: reviewed long-term memory with provenance and supersession.

Flow: authorised source -> propose() (status 'pending') -> Review Inbox ->
the user approves / rejects / edits / merges / supersedes -> approved entries
are what Jarvis may treat as true.

propose() and search() are the only functions the agent tool calls. Review
functions are wired to HTTP routes only, so the AI cannot approve its own
proposals.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from core.database import LedgerEntry, utcnow_naive
from services.quests import engine
from services.quests.store import QuestError

KINDS = ("fact", "decision", "preference", "reminder", "inference")
STATUSES = ("pending", "approved", "rejected", "superseded")
CONFIDENCE = ("low", "medium", "high")
SOURCES = ("user", "jarvis", "research", "import")
TEXT_MAX = 1000
REF_MAX = 500


def _norm(text: str) -> str:
    return " ".join((text or "").casefold().split())


def _scoped(q, owner):
    return q.filter(LedgerEntry.owner == owner) if owner is not None else q


def entry_dict(e: LedgerEntry) -> Dict[str, Any]:
    return {
        "id": e.id,
        "kind": e.kind,
        "text": e.text,
        "status": e.status,
        "source": e.source,
        "source_ref": e.source_ref,
        "confidence": e.confidence,
        "campaign_id": e.campaign_id,
        "quest_id": e.quest_id,
        "relevant_until": e.relevant_until,
        "supersedes_id": e.supersedes_id,
        "superseded_by_id": e.superseded_by_id,
        "reviewed_at": e.reviewed_at.isoformat() if e.reviewed_at else None,
        "created_at": e.created_at.isoformat() if e.created_at else None,
        "updated_at": e.updated_at.isoformat() if e.updated_at else None,
    }


def get_entry(db, owner, entry_id: str) -> LedgerEntry:
    e = db.query(LedgerEntry).filter(LedgerEntry.id == entry_id).first()
    if not e or (owner is not None and e.owner != owner):
        raise QuestError(404, "Ledger entry not found")
    return e


def _clean(value: Optional[str], allowed, default: str, label: str) -> str:
    if value in (None, ""):
        return default
    v = str(value).strip().lower()
    if v not in allowed:
        raise QuestError(422, f"{label} must be one of {', '.join(allowed)}")
    return v


def _clean_text(text: Optional[str]) -> str:
    t = " ".join(str(text or "").split())
    if not t:
        raise QuestError(422, "Ledger text is required")
    return t[:TEXT_MAX]


def conflicts_for(db, owner, text: str, exclude_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Approved entries that share most of their words with `text`.

    A cheap lexical overlap check; it flags likely duplicates or contradictions
    for the reviewer rather than deciding anything.
    """
    words = {w for w in _norm(text).split() if len(w) > 3}
    if not words:
        return []
    out = []
    for e in _scoped(db.query(LedgerEntry), owner).filter(LedgerEntry.status == "approved").all():
        if e.id == exclude_id:
            continue
        other = {w for w in _norm(e.text).split() if len(w) > 3}
        if other and len(words & other) / min(len(words), len(other)) >= 0.6:
            out.append({"id": e.id, "text": e.text, "kind": e.kind})
    return out[:5]


def propose(db, owner, text: str, kind: str = "fact", source: str = "jarvis",
            source_ref: Optional[str] = None, confidence: str = "medium",
            campaign_id: Optional[str] = None, quest_id: Optional[str] = None,
            relevant_until: Optional[str] = None, supersedes_id: Optional[str] = None) -> tuple[LedgerEntry, bool]:
    """Add a pending candidate. Returns (entry, created). Duplicates are not re-added."""
    text = _clean_text(text)
    key = _norm(text)
    for e in _scoped(db.query(LedgerEntry), owner).filter(
            LedgerEntry.status.in_(("pending", "approved"))).all():
        if _norm(e.text) == key:
            return e, False
    if supersedes_id:
        get_entry(db, owner, supersedes_id)
    until = None
    if relevant_until:
        day = engine.parse_day(relevant_until)
        if day is None:
            raise QuestError(422, "relevant_until must be YYYY-MM-DD")
        until = day.isoformat()
    e = LedgerEntry(
        id=str(uuid.uuid4()),
        owner=owner,
        kind=_clean(kind, KINDS, "fact", "kind"),
        text=text,
        status="pending",
        source=_clean(source, SOURCES, "jarvis", "source"),
        source_ref=(str(source_ref).strip()[:REF_MAX] or None) if source_ref else None,
        confidence=_clean(confidence, CONFIDENCE, "medium", "confidence"),
        campaign_id=campaign_id or None,
        quest_id=quest_id or None,
        relevant_until=until,
        supersedes_id=supersedes_id or None,
    )
    db.add(e)
    db.flush()
    return e, True


def _supersede(db, owner, old_id: str, new_id: str):
    old = get_entry(db, owner, old_id)
    if old.id == new_id:
        raise QuestError(422, "An entry cannot supersede itself")
    old.status = "superseded"
    old.superseded_by_id = new_id
    old.reviewed_at = utcnow_naive()


def review(db, owner, entry_id: str, decision: str, edits: Optional[Dict[str, Any]] = None) -> LedgerEntry:
    """Approve or reject a pending entry, optionally editing it first (UI only)."""
    e = get_entry(db, owner, entry_id)
    if e.status != "pending":
        raise QuestError(409, f"Entry is already {e.status}")
    edits = edits or {}
    if "text" in edits and edits["text"] is not None:
        e.text = _clean_text(edits["text"])
    if edits.get("kind"):
        e.kind = _clean(edits["kind"], KINDS, e.kind, "kind")
    if "relevant_until" in edits:
        day = engine.parse_day(edits["relevant_until"]) if edits["relevant_until"] else None
        if edits["relevant_until"] and day is None:
            raise QuestError(422, "relevant_until must be YYYY-MM-DD")
        e.relevant_until = day.isoformat() if day else None
    if decision == "approve":
        e.status = "approved"
        if e.supersedes_id:
            _supersede(db, owner, e.supersedes_id, e.id)
    elif decision == "reject":
        e.status = "rejected"
    else:
        raise QuestError(422, "decision must be approve or reject")
    e.reviewed_at = utcnow_naive()
    db.flush()
    return e


def merge(db, owner, entry_id: str, into_id: str, text: Optional[str] = None) -> LedgerEntry:
    """Fold a pending entry into an approved one, optionally rewording it."""
    e = get_entry(db, owner, entry_id)
    target = get_entry(db, owner, into_id)
    if e.status != "pending":
        raise QuestError(409, f"Entry is already {e.status}")
    if target.status != "approved" or target.id == e.id:
        raise QuestError(409, "Can only merge into a different approved entry")
    if text:
        # Rewording an approved fact is itself a change of truth: keep the old
        # wording as a superseded row so history stays readable.
        old = LedgerEntry(
            id=str(uuid.uuid4()), owner=target.owner, kind=target.kind, text=target.text,
            status="superseded", source=target.source, source_ref=target.source_ref,
            confidence=target.confidence, campaign_id=target.campaign_id,
            quest_id=target.quest_id, relevant_until=target.relevant_until,
            superseded_by_id=target.id, reviewed_at=utcnow_naive(),
        )
        db.add(old)
        target.text = _clean_text(text)
    e.status = "superseded"
    e.superseded_by_id = target.id
    e.reviewed_at = utcnow_naive()
    db.flush()
    return target


def correct(db, owner, entry_id: str, text: str, kind: Optional[str] = None) -> LedgerEntry:
    """Replace an approved entry with a corrected one (old row kept as superseded)."""
    old = get_entry(db, owner, entry_id)
    if old.status != "approved":
        raise QuestError(409, "Only approved entries can be corrected")
    new = LedgerEntry(
        id=str(uuid.uuid4()), owner=old.owner,
        kind=_clean(kind, KINDS, old.kind, "kind") if kind else old.kind,
        text=_clean_text(text), status="approved", source="user",
        source_ref=f"correction of {old.id[:8]}", confidence="high",
        campaign_id=old.campaign_id, quest_id=old.quest_id,
        relevant_until=old.relevant_until, supersedes_id=old.id, reviewed_at=utcnow_naive(),
    )
    db.add(new)
    db.flush()
    _supersede(db, owner, old.id, new.id)
    db.flush()
    return new


def list_entries(db, owner, status: Optional[str] = None, limit: int = 200) -> List[LedgerEntry]:
    q = _scoped(db.query(LedgerEntry), owner)
    if status:
        if status not in STATUSES:
            raise QuestError(422, f"status must be one of {', '.join(STATUSES)}")
        q = q.filter(LedgerEntry.status == status)
    return q.order_by(LedgerEntry.created_at.desc()).limit(min(500, max(1, limit))).all()


def pending_count(db, owner) -> int:
    return _scoped(db.query(LedgerEntry), owner).filter(LedgerEntry.status == "pending").count()


def search(db, owner, query: str, limit: int = 8, today=None) -> List[LedgerEntry]:
    """Approved, still-relevant entries ranked by word overlap with the query."""
    from datetime import date as _date
    today = today or _date.today()
    words = {w for w in _norm(query).split() if len(w) > 2}
    rows = _scoped(db.query(LedgerEntry), owner).filter(LedgerEntry.status == "approved").all()
    scored = []
    for e in rows:
        until = engine.parse_day(e.relevant_until)
        if until is not None and until < today:
            continue
        text = _norm(e.text)
        score = sum(1 for w in words if w in text) if words else 1
        if score:
            scored.append((score, e.updated_at or e.created_at, e))
    scored.sort(key=lambda s: (-s[0], -(s[1].timestamp() if s[1] else 0)))
    return [e for _, _, e in scored[:limit]]
