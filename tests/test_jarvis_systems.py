"""JARVIS systems: daily command centre, Life Ledger, resources, action
history, permissions, system health and their agent tools.

Uses a temp SQLite DB, configured auth, a non-loopback peer and identity from
the x-test-user header (same harness as the quest route tests).
"""
import asyncio
import json
from datetime import date, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb
from core.database import ActionLog, LedgerEntry
import routes.jarvis.jarvis_routes as jr
import routes.quests.quest_routes as qr
from services.quests import engine
from services.quests.engine import QuestView

_PEER = ("203.0.113.7", 54321)
SAM = {"x-test-user": "sam"}
EVE = {"x-test-user": "eve"}
TODAY = date.today()


class _Identity:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            user = dict(scope.get("headers") or []).get(b"x-test-user")
            if user:
                scope.setdefault("state", {})["current_user"] = user.decode()
        await self.app(scope, receive, send)


@pytest.fixture
def env(monkeypatch, tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'jarvis.db'}",
                        connect_args={"check_same_thread": False}, poolclass=NullPool)
    cdb.Base.metadata.create_all(eng)
    factory = sessionmaker(bind=eng)
    for mod in (cdb, jr, qr):
        monkeypatch.setattr(mod, "SessionLocal", factory)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.delenv("LOCALHOST_BYPASS", raising=False)
    app = FastAPI()
    app.state.auth_manager = SimpleNamespace(is_configured=True)
    app.include_router(qr.setup_quest_routes())
    app.include_router(jr.setup_jarvis_routes())
    return _Identity(app), factory


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=_PEER), base_url="http://j.test")


async def _quest(c, **body):
    r = await c.post("/api/quests", json={"title": "Q", **body}, headers=SAM)
    assert r.status_code == 200, r.text
    return r.json()


# --- daily missions engine ------------------------------------------------

def test_missions_respect_time_budget_and_energy():
    qs = [
        QuestView(id="big", title="Big", impact=3, effort=3, created_order=0),
        QuestView(id="mid", title="Mid", impact=2, effort=2, est_minutes=60, created_order=1),
        QuestView(id="small", title="Small", impact=1, effort=1, created_order=2),
    ]
    picked = engine.pick_missions(qs, minutes=100, today=TODAY)
    assert [m["quest_id"] for m in picked] == ["mid", "small"]  # big (240 min) doesn't fit
    low = engine.pick_missions(qs, energy="low", today=TODAY)
    assert "big" not in [m["quest_id"] for m in low]
    urgent = [QuestView(id="big", title="Big", effort=3, due_date=TODAY.isoformat())]
    assert engine.pick_missions(urgent, energy="low", today=TODAY)[0]["quest_id"] == "big"


def test_missions_fallback_and_locked_chain():
    qs = [QuestView(id="huge", title="Huge", effort=3)]
    picked = engine.pick_missions(qs, minutes=20, today=TODAY)
    assert picked[0]["quest_id"] == "huge" and picked[0]["fits"] is False
    chain = [QuestView(id="a", title="A", effort=1, created_order=0),
             QuestView(id="b", title="B", effort=1, depends_on=("a",), created_order=1)]
    picked = engine.pick_missions(chain, today=TODAY)
    assert [(m["quest_id"], m["state"]) for m in picked] == [("a", "ready"), ("b", "locked")]


# --- command centre -------------------------------------------------------

async def test_today_plans_missions_and_tracks_states(env):
    app, _ = env
    async with client(app) as c:
        a = await _quest(c, title="Order RAM", effort=1, impact=3)
        b = await _quest(c, title="Install RAM", effort=1, depends_on=[a["id"]])
        late = await _quest(c, title="Pay bill", effort=1, due_date=(TODAY - timedelta(days=2)).isoformat())
        soon = await _quest(c, title="Book test", effort=2, due_date=(TODAY + timedelta(days=2)).isoformat())

        t = (await c.get("/api/jarvis/today", headers=SAM)).json()
        assert t["date"] == TODAY.isoformat()
        ids = [m["id"] for m in t["missions"]]
        assert late["id"] == ids[0]  # overdue ranks first
        assert [q["id"] for q in t["overdue"]] == [late["id"]]
        assert [q["id"] for q in t["due_soon"]] == [soon["id"]]
        assert [q["id"] for q in t["blocked"]] == [b["id"]]

        await c.post(f"/api/quests/{late['id']}/complete", json={"outcome": "Paid online"}, headers=SAM)
        t2 = (await c.get("/api/jarvis/today", headers=SAM)).json()
        states = {m["id"]: m["state"] for m in t2["missions"]}
        assert states[late["id"]] == "done"  # missions stay stable through the day

        t3 = (await c.post("/api/jarvis/today", json={"energy": "low", "minutes_available": 45},
                           headers=SAM)).json()
        assert t3["plan"] == {"energy": "low", "minutes_available": 45,
                              "minutes_planned": t3["plan"]["minutes_planned"]}
        assert t3["plan"]["minutes_planned"] <= 45
        assert (await c.post("/api/jarvis/today", json={"energy": "wired"}, headers=SAM)).status_code == 422


async def test_set_missions_only_own_quests(env):
    app, _ = env
    async with client(app) as c:
        mine = await _quest(c, title="Mine")
        r = await c.post("/api/quests", json={"title": "Eve's"}, headers=EVE)
        assert (await c.put("/api/jarvis/today/missions", json={"quest_ids": [r.json()["id"]]},
                            headers=SAM)).status_code == 404
        t = (await c.put("/api/jarvis/today/missions", json={"quest_ids": [mine["id"]]}, headers=SAM)).json()
        assert [m["id"] for m in t["missions"]] == [mine["id"]]


async def test_quick_capture(env):
    app, _ = env
    async with client(app) as c:
        q = (await c.post("/api/jarvis/capture", json={"text": "Look into Tailscale"}, headers=SAM)).json()
        assert q["type"] == "quest" and q["quest"]["title"] == "Look into Tailscale"
        led = (await c.post("/api/jarvis/capture", json={"text": "I prefer AMD GPUs", "kind": "ledger"},
                            headers=SAM)).json()
        assert led["type"] == "ledger" and led["entry"]["status"] == "pending"


async def test_fail_closed_without_identity(env):
    app, _ = env
    async with client(app) as c:
        for path in ("/api/jarvis/today", "/api/jarvis/ledger", "/api/jarvis/resources",
                     "/api/jarvis/actions", "/api/jarvis/permissions", "/api/jarvis/health"):
            assert (await c.get(path)).status_code == 401, path


# --- Life Ledger ----------------------------------------------------------

async def _run_ledger_tool(args, owner="sam"):
    from src.tools.jarvis_tools import do_manage_ledger
    return await do_manage_ledger(json.dumps(args), owner=owner)


async def test_ledger_proposals_need_human_review(env):
    app, factory = env
    out = await _run_ledger_tool({"action": "propose", "text": "Buy a used RTX 3090 for the 24 GB upgrade",
                            "kind": "decision", "source_ref": "chat 23 Oct"})
    assert out["exit_code"] == 0 and "Review Inbox" in out["results"]
    # The agent can neither approve nor see unapproved entries as facts.
    assert (await _run_ledger_tool({"action": "approve", "text": "x"}))["exit_code"] == 1
    assert "No approved" in (await _run_ledger_tool({"action": "search", "query": "3090"}))["results"]
    # Duplicate proposals are not re-added.
    again = await _run_ledger_tool({"action": "propose", "text": "buy a used RTX 3090 for the 24 GB upgrade"})
    assert "Already in the Life Ledger" in again["results"]

    async with client(app) as c:
        inbox = (await c.get("/api/jarvis/ledger?status=pending", headers=SAM)).json()
        assert inbox["pending"] == 1
        entry = inbox["entries"][0]
        r = await c.post(f"/api/jarvis/ledger/{entry['id']}/review",
                         json={"decision": "approve", "text": "Buy a used RTX 3090 (24 GB) next"}, headers=SAM)
        assert r.status_code == 200 and r.json()["status"] == "approved"
        assert (await c.post(f"/api/jarvis/ledger/{entry['id']}/review", json={"decision": "approve"},
                             headers=SAM)).status_code == 409
        assert (await c.get("/api/jarvis/ledger", headers=EVE)).json()["entries"] == []
    assert "Buy a used RTX 3090 (24 GB) next" in (await _run_ledger_tool({"action": "search", "query": "3090"}))["results"]


async def test_ledger_supersede_merge_correct_and_expiry(env):
    app, factory = env
    async with client(app) as c:
        old = (await c.post("/api/jarvis/ledger", json={"text": "Budget for GPU is 600 euro", "kind": "decision"},
                            headers=SAM)).json()
        assert old["status"] == "approved"  # user-entered: approved directly

        await _run_ledger_tool({"action": "propose", "text": "Budget for GPU is now 800 euro",
                          "supersedes": "Budget for GPU", "kind": "decision"})
        pend = (await c.get("/api/jarvis/ledger?status=pending", headers=SAM)).json()["entries"][0]
        assert pend["supersedes_id"] == old["id"]
        assert pend["conflicts"] and pend["conflicts"][0]["id"] == old["id"]
        await c.post(f"/api/jarvis/ledger/{pend['id']}/review", json={"decision": "approve"}, headers=SAM)
        rows = {e["id"]: e for e in (await c.get("/api/jarvis/ledger", headers=SAM)).json()["entries"]}
        assert rows[old["id"]]["status"] == "superseded" and rows[old["id"]]["superseded_by_id"] == pend["id"]

        corrected = (await c.post(f"/api/jarvis/ledger/{pend['id']}/correct",
                                  json={"text": "Budget for GPU is 750 euro"}, headers=SAM)).json()
        assert corrected["status"] == "approved" and corrected["supersedes_id"] == pend["id"]

        await _run_ledger_tool({"action": "propose", "text": "GPU budget 750 euro confirmed by Sam"})
        dup = (await c.get("/api/jarvis/ledger?status=pending", headers=SAM)).json()["entries"][0]
        merged = (await c.post(f"/api/jarvis/ledger/{dup['id']}/merge", json={"into_id": corrected["id"]},
                               headers=SAM)).json()
        assert merged["id"] == corrected["id"]

        yesterday = (TODAY - timedelta(days=1)).isoformat()
        await c.post("/api/jarvis/ledger", json={"text": "Visiting family this weekend", "kind": "reminder",
                                                 "relevant_until": yesterday}, headers=SAM)
    assert "No approved" in (await _run_ledger_tool({"action": "search", "query": "family weekend"}))["results"]


# --- resources ------------------------------------------------------------

async def test_resources_budget_pressure(env):
    app, _ = env
    async with client(app) as c:
        camp = (await c.post("/api/quests/campaigns", json={"title": "PC Upgrade"}, headers=SAM)).json()
        await _quest(c, title="Buy RAM", campaign_id=camp["id"], est_cost=180)
        await _quest(c, title="Buy GPU", campaign_id=camp["id"], est_cost=650)
        await _quest(c, title="Benchmark", campaign_id=camp["id"])
        r = await c.post("/api/jarvis/resources", json={"kind": "budget", "name": "PC fund", "amount": 700,
                                                        "unit": "EUR", "campaign_id": camp["id"]}, headers=SAM)
        assert r.status_code == 200
        await c.post("/api/jarvis/resources", json={"kind": "time", "name": "Evenings", "amount": 10}, headers=SAM)
        s = (await c.get("/api/jarvis/resources", headers=SAM)).json()
        b = s["budgets"][0]
        assert (b["planned_spend"], b["remaining"], b["over"], b["uncosted_quests"]) == (830, -130, True, 1)
        assert s["weekly_hours"] == 10
        assert (await c.post("/api/jarvis/resources", json={"kind": "budget", "name": "x", "amount": -5},
                             headers=SAM)).status_code == 422
        assert (await c.post("/api/jarvis/resources", json={"kind": "lottery", "name": "x"},
                             headers=SAM)).status_code == 422
        assert (await c.get("/api/jarvis/resources", headers=EVE)).json()["resources"] == []


# --- action history -------------------------------------------------------

def test_every_tool_call_is_recorded_and_secrets_redacted(env):
    _app, factory = env
    from src.agent_tools import ToolBlock
    from src.tool_execution import NO_TOOL_SECURITY_CONTEXT, execute_tool_block
    from src.tool_capabilities import ToolRunSecurityContext

    async def go():
        await execute_tool_block(ToolBlock("manage_quests", json.dumps(
            {"action": "plan", "quests": [{"title": "Rotate api_key=abc123 secret"}], "token": "s3cr3t"})),
            owner="sam", security_context=NO_TOOL_SECURITY_CONTEXT)
        await execute_tool_block(ToolBlock("manage_quests", json.dumps({"action": "complete", "quest": "nope"})),
                                 owner="sam", security_context=NO_TOOL_SECURITY_CONTEXT)
        armed = ToolRunSecurityContext(external_untrusted_context_seen=True)
        await execute_tool_block(ToolBlock("manage_notes", json.dumps({"action": "add", "title": "x"})),
                                 owner="sam", security_context=armed)
    asyncio.run(go())

    db = factory()
    rows = db.query(ActionLog).order_by(ActionLog.created_at.asc()).all()
    db.close()
    assert [(r.tool, r.action, r.status) for r in rows] == [
        ("manage_quests", "plan", "ok"), ("manage_quests", "complete", "error"), ("manage_notes", "add", "blocked")]
    assert "s3cr3t" not in rows[0].summary and "abc123" not in rows[0].summary
    assert "[redacted]" in rows[0].summary
    assert rows[0].owner == "sam" and rows[0].duration_ms is not None


async def test_action_history_is_read_only(env):
    app, _ = env
    from core.database import SessionLocal  # noqa: F401  (patched factory)
    from services.jarvis.audit import record_tool_call
    record_tool_call("sam", "s1", "manage_quests", '{"action":"next"}', {"results": "x", "exit_code": 0})
    record_tool_call("eve", "s2", "manage_quests", '{"action":"next"}', {"results": "x", "exit_code": 0})
    async with client(app) as c:
        rows = (await c.get("/api/jarvis/actions", headers=SAM)).json()["actions"]
        assert len(rows) == 1 and rows[0]["tool"] == "manage_quests"
        rid = rows[0]["id"]
        for method in ("put", "patch", "delete"):
            r = await getattr(c, method)(f"/api/jarvis/actions/{rid}", headers=SAM)
            assert r.status_code in (404, 405)
        assert (await c.get("/api/jarvis/actions?status=bogus", headers=SAM)).status_code == 422


# --- permissions, health --------------------------------------------------

async def test_permissions_snapshot_reflects_policy(env):
    app, _ = env
    async with client(app) as c:
        p = (await c.get("/api/jarvis/permissions", headers=SAM)).json()
    pol = p["approval_policy"]["tools"]
    assert "complete" in pol["manage_quests"]["writes"] and "complete" not in pol["manage_quests"]["no_prompt"]
    assert pol["manage_ledger"]["no_prompt"] == ["propose", "search"]
    assert "manage_ledger" in p["cloud_private_tools"] and "manage_quests" in p["cloud_private_tools"]


def test_health_text_flags_problems(monkeypatch):
    from services.jarvis import health
    monkeypatch.setattr(health, "model_endpoints", lambda owner: [
        {"name": "127.0.0.1:8080", "url": "http://127.0.0.1:8080/v1", "ok": True, "latency_ms": 12,
         "models": ["unsloth/Qwen3.6-35B-A3B-GGUF"], "n_ctx": 16384, "busy_slots": 0}])
    monkeypatch.setattr(health, "chroma", lambda: {"ok": False, "url": "http://127.0.0.1:8100"})
    monkeypatch.setattr(health, "memory", lambda: {"ok": False, "total_gb": 31.9, "free_gb": 1.8, "used_pct": 94})
    monkeypatch.setattr(health, "disk", lambda: {"ok": True, "total_gb": 900, "free_gb": 400})
    monkeypatch.setattr(health, "vram", lambda: {"ok": None, "used_mib": 6700})
    snap = health.snapshot("sam")
    assert not snap["ok"]
    assert any("ChromaDB" in p for p in snap["problems"]) and any("low RAM" in p for p in snap["problems"])
    text = health.as_text(snap)
    assert "ctx 16384" in text and "ChromaDB: DOWN" in text and "1.8 GB free" in text


def test_real_memory_and_disk_probes_return_numbers():
    from services.jarvis import health
    assert health.memory().get("total_gb", 1) > 0
    assert health.disk().get("total_gb", 1) > 0


def test_system_status_tool(monkeypatch):
    from services.jarvis import health
    from src.tools.jarvis_tools import do_system_status
    monkeypatch.setattr(health, "snapshot", lambda owner: {
        "ok": True, "problems": [], "llm": [], "chroma": {"ok": True}, "ram": {}, "disk": {}, "vram": {},
        "routing": {"model": "m"}})
    out = asyncio.run(do_system_status(None, owner="sam"))
    assert out["exit_code"] == 0 and out["results"].startswith("All systems OK.")


# --- agent tools, gating and routing --------------------------------------

def test_quest_tool_today_and_progress(env):
    from src.tools.quests import do_manage_quests
    run = lambda a: asyncio.run(do_manage_quests(json.dumps(a), owner="sam"))  # noqa: E731
    run({"action": "plan", "campaign": "Build Jarvis", "quests": [
        {"title": "Draft the dashboard", "effort": "small", "minutes": 45},
        {"title": "Ship the dashboard", "after": ["Draft the dashboard"], "cost": 0}]})
    t = run({"action": "today", "energy": "high", "minutes_available": 120})["results"]
    assert "energy high" in t and "Draft the dashboard" in t and "~45 min" in t
    assert run({"action": "log_progress", "quest": "Draft the dashboard", "note": "Wireframe done"})["exit_code"] == 0
    assert run({"action": "log_progress", "quest": "Draft the dashboard"})["exit_code"] == 1
    hist = run({"action": "history"})["results"]
    assert "progress: Draft the dashboard" in hist


def test_gate_policy_for_new_tools():
    from src.tool_capabilities import ToolRunSecurityContext
    ctx = ToolRunSecurityContext(external_untrusted_context_seen=True)
    ok = [("manage_ledger", {"action": "propose", "text": "x"}), ("manage_ledger", {"action": "search"}),
          ("manage_ledger", {"action": "remember", "text": "x"}), ("system_status", {}),
          ("manage_quests", {"action": "today"}), ("manage_quests", {"action": "log_progress", "quest": "x"})]
    for tool, args in ok:
        assert ctx.decision_for(tool, json.dumps(args)).allowed, (tool, args)
    for tool, args in [("manage_ledger", {"action": "approve"}), ("manage_quests", {"action": "complete"})]:
        assert not ctx.decision_for(tool, json.dumps(args)).allowed, (tool, args)


@pytest.mark.parametrize("text,category", [
    ("status report", "status"),
    ("is the model up?", "status"),
    ("how much RAM is free right now", "status"),
    ("plan my day", "quests"),
    ("I've decided to buy a used 3090", "quests"),
    ("what's on today?", "quests"),
])
def test_new_intents(text, category):
    from src.action_intents import classify_tool_intent
    intent = classify_tool_intent(text)
    assert intent.needs_tools and intent.category == category, (text, intent)


def test_status_domain_seeds_tool():
    from src.agent_loop import _DOMAIN_TOOL_MAP, _classify_agent_request
    msgs = [{"role": "user", "content": "give me a status report"}]
    assert "status" in _classify_agent_request(msgs, msgs[-1]["content"])["domains"]
    assert "system_status" in _DOMAIN_TOOL_MAP["status"]
    assert "manage_ledger" in _DOMAIN_TOOL_MAP["quests"]


def test_new_tools_registered():
    from src.agent_loop import FUNCTION_TOOL_SCHEMAS
    from src.cloud_privacy import PRIVATE_DATA_TOOL_NAMES
    names = {s["function"]["name"] for s in FUNCTION_TOOL_SCHEMAS if s.get("type") == "function"}
    assert {"manage_ledger", "system_status"} <= names
    assert "manage_ledger" in PRIVATE_DATA_TOOL_NAMES
    led = next(s for s in FUNCTION_TOOL_SCHEMAS if s["function"]["name"] == "manage_ledger")
    assert led["function"]["parameters"]["properties"]["action"]["enum"] == ["propose", "search"]
