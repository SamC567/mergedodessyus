"""Jarvis's manage_quests tool: planning from conversation, safe progress, routing.

The tool shares services.quests.store with the HTTP routes; these tests cover
the agent-facing contract: one-call planning with prerequisites, idempotent
re-planning, completion only with an outcome, title resolution that refuses to
guess, approval-gate classification, cloud privacy and intent routing.
"""
import asyncio
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb
from core.database import Quest, QuestCampaign, QuestEvent
from src.tools.quests import do_manage_quests

OWNER = "sam"


@pytest.fixture
def db_factory(monkeypatch, tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'quests_tool.db'}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    cdb.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(cdb, "SessionLocal", factory)
    return factory


def run(args, owner=OWNER):
    return asyncio.run(do_manage_quests(json.dumps(args), owner=owner))


def _quests(factory):
    db = factory()
    try:
        return {q.title: q for q in db.query(Quest).all()}
    finally:
        db.close()


def test_plan_creates_campaign_and_linked_quests(db_factory):
    out = run({
        "action": "plan",
        "campaign": "Upgrade the PC",
        "campaign_description": "Run bigger local models",
        "quests": [
            {"title": "Check PSU wattage", "effort": "small", "impact": "high", "skill": "hardware"},
            {"title": "Order 2x32GB DDR4-3200", "effort": "small", "after": ["Check PSU wattage"]},
            {"title": "Install RAM and verify 64 GB", "effort": "medium", "after": ["Order 2x32GB DDR4-3200"],
             "is_boss": True},
        ],
    })
    assert out["exit_code"] == 0, out
    assert "Created campaign 'Upgrade the PC'" in out["results"]
    assert "Added 3 quests" in out["results"]

    quests = _quests(db_factory)
    assert quests["Check PSU wattage"].impact == 3 and quests["Check PSU wattage"].effort == 1
    assert json.loads(quests["Order 2x32GB DDR4-3200"].depends_on) == [quests["Check PSU wattage"].id]
    assert quests["Install RAM and verify 64 GB"].is_boss is True

    nxt = run({"action": "next"})["results"]
    assert "Check PSU wattage" in nxt and "Install RAM" not in nxt


def test_replanning_reuses_campaign_and_skips_duplicates(db_factory):
    run({"action": "plan", "campaign": "Build Jarvis", "quests": [{"title": "Add voice input"}]})
    out = run({"action": "plan", "campaign": "build jarvis",
               "quests": [{"title": "Add voice input"}, {"title": "Add wake word", "after": ["Add voice input"]}]})
    assert out["exit_code"] == 0, out
    assert "Created campaign" not in out["results"]
    assert "Already on the board (skipped): Add voice input" in out["results"]

    db = db_factory()
    try:
        assert db.query(QuestCampaign).count() == 1
        assert db.query(Quest).count() == 2
    finally:
        db.close()
    quests = _quests(db_factory)
    assert json.loads(quests["Add wake word"].depends_on) == [quests["Add voice input"].id]


def test_complete_needs_outcome_and_resolves_by_title(db_factory):
    run({"action": "plan", "campaign": "Build Jarvis", "quests": [{"title": "Turn on local speech-to-text", "effort": "large"}]})

    out = run({"action": "complete", "quest": "speech-to-text"})
    assert out["exit_code"] == 1 and "outcome" in out["error"].lower()

    out = run({"action": "complete", "quest": "speech-to-text", "outcome": "Whisper transcribes my mic locally"})
    assert out["exit_code"] == 0, out
    assert "+50 XP" in out["results"]

    db = db_factory()
    try:
        ev = db.query(QuestEvent).filter(QuestEvent.kind == "completed").one()
        assert ev.detail == "Whisper transcribes my mic locally" and ev.xp_delta == 50
    finally:
        db.close()


def test_ambiguous_titles_are_refused_not_guessed(db_factory):
    run({"action": "plan", "quests": [{"title": "Write CV"}, {"title": "Write cover letter"}]})
    out = run({"action": "start", "quest": "write"})
    assert out["exit_code"] == 1
    assert "matches several quests" in out["error"]
    assert all(q.status == "todo" for q in _quests(db_factory).values())


def test_drop_and_reopen_but_no_delete(db_factory):
    run({"action": "plan", "quests": [{"title": "Buy a drone ND filter"}]})
    assert run({"action": "drop", "quest": "ND filter"})["exit_code"] == 0
    assert _quests(db_factory)["Buy a drone ND filter"].status == "dropped"
    assert run({"action": "reopen", "quest": "ND filter"})["exit_code"] == 0
    out = run({"action": "delete", "quest": "ND filter"})
    assert out["exit_code"] == 1 and "Unknown action" in out["error"]
    assert "Buy a drone ND filter" in _quests(db_factory)


def test_owner_isolation(db_factory):
    run({"action": "plan", "campaign": "Private", "quests": [{"title": "Secret thing"}]}, owner="alice")
    board = run({"action": "board"})["results"]
    assert "Secret thing" not in board and "Private" not in board
    assert run({"action": "start", "quest": "Secret thing"})["exit_code"] == 1


def test_board_reports_level_and_campaigns(db_factory):
    run({"action": "plan", "campaign": "Career", "quests": [{"title": "Update LinkedIn", "effort": "small"}]})
    run({"action": "complete", "quest": "Update LinkedIn", "outcome": "Profile updated with Odysseus project"})
    board = run({"action": "board", "include_finished": True})["results"]
    assert board.startswith("Level 1 - 10/100 XP")
    assert "Campaign: Career" in board and "[x] Update LinkedIn" in board


def test_invalid_args_are_reported():
    out = asyncio.run(do_manage_quests("{not json", owner=OWNER))
    assert out["exit_code"] == 1


# --- wiring: gating, privacy, routing -------------------------------------

def test_capabilities_split_reads_and_writes():
    from src.tool_capabilities import ToolEffect, capabilities_for_action
    read = capabilities_for_action("manage_quests", {"action": "next"})
    assert read.effects == frozenset({ToolEffect.READ_PRIVATE})
    write = capabilities_for_action("manage_quests", {"action": "plan", "quests": []})
    assert ToolEffect.WRITE_PRIVATE in write.effects and ToolEffect.DESTRUCTIVE not in write.effects
    alias = capabilities_for_action("manage_quests", {"action": "done", "quest": "x", "outcome": "y"})
    assert ToolEffect.WRITE_PRIVATE in alias.effects
    unknown = capabilities_for_action("manage_quests", {"action": "explode"})
    assert {ToolEffect.READ_PRIVATE, ToolEffect.WRITE_PRIVATE} <= set(unknown.effects)


def test_post_external_gate_allows_only_quest_reads_and_creates():
    """Memories/skills arm the untrusted-context gate in most chats. Quest
    reads and inert creates may pass; anything that changes state still needs
    the user's approval, and other private tools are unaffected."""
    from src.tool_capabilities import ToolRunSecurityContext
    ctx = ToolRunSecurityContext(external_untrusted_context_seen=True)
    for args in ({"action": "next"}, {"action": "board"}, {"action": "history"},
                 {"action": "plan", "quests": [{"title": "x"}]},
                 {"action": "add_quest", "title": "x"}, {"action": "create", "title": "x"},
                 {"action": "add_campaign", "title": "x"}):
        assert ctx.decision_for("manage_quests", json.dumps(args)).allowed, args
    for args in ({"action": "complete", "quest": "x", "outcome": "y"}, {"action": "done", "quest": "x"},
                 {"action": "drop", "quest": "x"}, {"action": "reopen", "quest": "x"},
                 {"action": "update", "quest": "x", "title": "y"},
                 {"action": "update_campaign", "campaign": "x", "status": "done"},
                 {"action": "explode"}):
        assert not ctx.decision_for("manage_quests", json.dumps(args)).allowed, args
    assert not ctx.decision_for("manage_notes", json.dumps({"action": "add", "title": "x"})).allowed
    assert not ctx.decision_for("manage_notes", json.dumps({"action": "list"})).allowed
    # Memory: reads pass (user's choice), every write stays gated.
    assert ctx.decision_for("manage_memory", "search\nPC hardware specs").allowed
    assert ctx.decision_for("manage_memory", json.dumps({"action": "search", "query": "gpu"})).allowed
    assert ctx.decision_for("manage_memory", "list").allowed
    assert not ctx.decision_for("manage_memory", "add\nfact").allowed
    assert not ctx.decision_for("manage_memory", json.dumps({"action": "edit", "id": "x"})).allowed
    assert not ctx.decision_for("manage_memory", "delete\nabc").allowed
    # Without untrusted context nothing changes.
    assert ToolRunSecurityContext().decision_for("manage_quests", json.dumps({"action": "drop"})).allowed


def test_quests_never_go_to_cloud_models():
    from src.cloud_privacy import PRIVATE_DATA_TOOL_NAMES
    assert "manage_quests" in PRIVATE_DATA_TOOL_NAMES


def test_schema_and_dispatch_are_registered():
    from src.agent_loop import FUNCTION_TOOL_SCHEMAS
    names = {s["function"]["name"] for s in FUNCTION_TOOL_SCHEMAS if s.get("type") == "function"}
    assert "manage_quests" in names
    from src.tool_parsing import _TOOL_NAME_MAP
    assert _TOOL_NAME_MAP.get("quests") == "manage_quests"


@pytest.mark.parametrize("text", [
    "what should I do next?",
    "help me plan my week",
    "can you plan out upgrading my PC",
    "my main goal is to get a job in cybersecurity",
    "I finished installing the RAM",
    "show my quests",
    # Live regression 2026-10-23: goal stated before the planning verb.
    "I want to upgrade my PC so I can run bigger local models: 64GB RAM first, "
    "then a 24GB graphics card. Can you plan that out for me?",
])
def test_planning_talk_escalates_chat_to_quests(text):
    from src.action_intents import classify_tool_intent
    intent = classify_tool_intent(text)
    assert intent.needs_tools and intent.category == "quests", (text, intent)


def test_how_to_questions_do_not_escalate():
    from src.action_intents import classify_tool_intent
    assert not classify_tool_intent("how do I plan my week?").needs_tools


@pytest.mark.parametrize("text", [
    "what should I work on today?",
    "I want to upgrade my PC so I can run bigger local models: 64GB RAM first, "
    "then a 24GB graphics card. Can you plan that out for me?",
])
def test_agent_domain_seeds_manage_quests(text):
    from src.agent_loop import _DOMAIN_TOOL_MAP, _classify_agent_request
    msgs = [{"role": "user", "content": text}]
    intent = _classify_agent_request(msgs, text)
    assert "quests" in intent["domains"] and intent["low_signal"] is False
    assert "manage_quests" in _DOMAIN_TOOL_MAP["quests"]
