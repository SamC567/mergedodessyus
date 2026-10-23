"""Quest-log rules: XP, levels, dependency blocking and next-action ranking."""
from datetime import date

from services.quests import engine
from services.quests.engine import QuestView

TODAY = date(2026, 10, 23)


def test_xp_scales_with_effort_and_boss():
    assert engine.xp_for(1, False) == 10
    assert engine.xp_for(2, False) == 25
    assert engine.xp_for(3, False) == 50
    assert engine.xp_for(3, True) == 150
    # Out-of-range input is clamped, not trusted.
    assert engine.xp_for(99, False) == 50
    assert engine.xp_for("junk", False) == 25


def test_level_thresholds_are_exact():
    assert engine.level_for(0)["level"] == 1
    assert engine.level_for(99)["level"] == 1
    assert engine.level_for(100)["level"] == 2
    assert engine.level_for(299)["level"] == 2
    info = engine.level_for(300)
    assert info["level"] == 3
    assert (info["level_floor"], info["next_level_xp"]) == (300, 600)
    # Negative totals (only possible via reversals) floor at level 1.
    assert engine.level_for(-40)["level"] == 1


def test_open_prerequisite_blocks_but_done_or_dropped_does_not():
    a = QuestView(id="a", title="A")
    b = QuestView(id="b", title="B", depends_on=("a", "ghost"))
    by_id = {"a": a, "b": b}
    assert [q.id for q in engine.blockers(b, by_id)] == ["a"]
    a.status = "done"
    assert engine.blockers(b, by_id) == []
    a.status = "dropped"
    assert engine.blockers(b, by_id) == []


def test_cycle_detection():
    deps = {"a": ["b"], "b": ["c"], "c": []}
    assert engine.would_create_cycle("c", ["a"], deps) is True
    assert engine.would_create_cycle("c", ["c"], deps) is True
    assert engine.would_create_cycle("a", ["c"], deps) is False


def test_recommend_prefers_overdue_and_explains_why():
    quests = [
        QuestView(id="big", title="Big impact", impact=3, created_order=0),
        QuestView(id="late", title="Overdue admin", impact=1, due_date="2026-10-20", created_order=1),
    ]
    out = engine.recommend(quests, today=TODAY)
    assert [r["quest_id"] for r in out] == ["late", "big"]
    assert out[0]["reasons"][0] == "overdue by 3 days"


def test_recommend_skips_blocked_inactive_and_finished():
    quests = [
        QuestView(id="pre", title="Prerequisite", created_order=0),
        QuestView(id="after", title="Needs pre", depends_on=("pre",), created_order=1),
        QuestView(id="paused", title="Paused campaign", campaign_id="c-paused", created_order=2),
        QuestView(id="done", title="Done", status="done", created_order=3),
    ]
    out = engine.recommend(quests, inactive_campaigns=["c-paused"], today=TODAY)
    assert [r["quest_id"] for r in out] == ["pre"]
    assert "unblocks 1 other quest" in out[0]["reasons"]


def test_recommend_rewards_finishing_what_was_started():
    quests = [
        QuestView(id="new", title="New", impact=2, created_order=0),
        QuestView(id="wip", title="In progress", impact=2, status="doing", created_order=1),
    ]
    out = engine.recommend(quests, today=TODAY)
    assert out[0]["quest_id"] == "wip"
    assert "already in progress" in out[0]["reasons"]
