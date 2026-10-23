"""Quest log API: owner scoping, evidenced XP, append-only history, dependencies.

Uses the same harness as test_notes_fail_closed_auth.py: a temp SQLite DB,
configured auth, a non-loopback peer, and identity injected by a pure-ASGI
shim from the x-test-user header (no header => no identity).
"""
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb
from core.database import QuestEvent
import routes.quests.quest_routes as qr

_PEER = ("203.0.113.7", 54321)
ALICE = {"x-test-user": "alice"}
BOB = {"x-test-user": "bob"}


class _Identity:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            user = headers.get(b"x-test-user")
            if user:
                scope.setdefault("state", {})["current_user"] = user.decode()
        await self.app(scope, receive, send)


@pytest.fixture
def env(monkeypatch, tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'quests.db'}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    cdb.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(qr, "SessionLocal", factory)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.delenv("LOCALHOST_BYPASS", raising=False)

    app = FastAPI()
    app.state.auth_manager = SimpleNamespace(is_configured=True)
    app.include_router(qr.setup_quest_routes())
    return _Identity(app), factory


def _client(app):
    transport = httpx.ASGITransport(app=app, client=_PEER)
    return httpx.AsyncClient(transport=transport, base_url="http://quests.test")


async def _quest(c, headers, **body):
    r = await c.post("/api/quests", json={"title": "Quest", **body}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def test_no_identity_fails_closed(env):
    app, _ = env
    async with _client(app) as c:
        assert (await c.get("/api/quests/board")).status_code == 401
        assert (await c.get("/api/quests/history")).status_code == 401
        assert (await c.post("/api/quests", json={"title": "ghost"})).status_code == 401
        assert (await c.post("/api/quests/campaigns", json={"title": "ghost"})).status_code == 401


async def test_users_only_see_their_own_quests(env):
    app, _ = env
    async with _client(app) as c:
        q = await _quest(c, ALICE, title="Alice only")
        assert (await c.get(f"/api/quests/board", headers=BOB)).json()["quests"] == []
        assert (await c.post(f"/api/quests/{q['id']}/start", headers=BOB)).status_code == 404
        assert (await c.post(f"/api/quests/{q['id']}/complete", json={"outcome": "x"}, headers=BOB)).status_code == 404
        assert (await c.delete(f"/api/quests/{q['id']}", headers=BOB)).status_code == 404


async def test_completion_needs_an_outcome_and_awards_xp_once(env):
    app, _ = env
    async with _client(app) as c:
        q = await _quest(c, ALICE, title="Write roadmap", effort=3, skill="planning")
        r = await c.post(f"/api/quests/{q['id']}/complete", json={"outcome": "   "}, headers=ALICE)
        assert r.status_code == 422

        r = await c.post(f"/api/quests/{q['id']}/complete", json={"outcome": "Roadmap doc written"}, headers=ALICE)
        assert r.status_code == 200
        assert r.json()["xp_awarded"] == 50

        # A second completion is refused, so XP can't be farmed.
        r = await c.post(f"/api/quests/{q['id']}/complete", json={"outcome": "again"}, headers=ALICE)
        assert r.status_code == 409

        board = (await c.get("/api/quests/board", headers=ALICE)).json()
        assert board["stats"]["xp"] == 50
        assert board["stats"]["done_today"] == 1
        assert board["stats"]["skills"] == [{"skill": "planning", "xp": 50}]


async def test_reopen_reverses_xp_without_rewriting_history(env):
    app, factory = env
    async with _client(app) as c:
        q = await _quest(c, ALICE, effort=1)
        await c.post(f"/api/quests/{q['id']}/complete", json={"outcome": "done"}, headers=ALICE)
        r = await c.post(f"/api/quests/{q['id']}/reopen", headers=ALICE)
        assert r.status_code == 200 and r.json()["status"] == "todo"

        board = (await c.get("/api/quests/board", headers=ALICE)).json()
        assert board["stats"]["xp"] == 0

        kinds = [e["kind"] for e in (await c.get("/api/quests/history", headers=ALICE)).json()["events"]]
        assert kinds == ["reopened", "completed", "created"]

    db = factory()
    deltas = sorted(e.xp_delta for e in db.query(QuestEvent).all())
    db.close()
    assert deltas == [-10, 0, 10]


async def test_prerequisites_block_and_gate_recommendations(env):
    app, _ = env
    async with _client(app) as c:
        pre = await _quest(c, ALICE, title="Buy RAM")
        after = await _quest(c, ALICE, title="Benchmark 64GB", depends_on=[pre["id"]])

        assert after["blocked_by"] == [{"id": pre["id"], "title": "Buy RAM"}]
        assert (await c.post(f"/api/quests/{after['id']}/start", headers=ALICE)).status_code == 409
        r = await c.post(f"/api/quests/{after['id']}/complete", json={"outcome": "x"}, headers=ALICE)
        assert r.status_code == 409

        nxt = (await c.get("/api/quests/board", headers=ALICE)).json()["next"]
        assert [n["quest_id"] for n in nxt] == [pre["id"]]

        await c.post(f"/api/quests/{pre['id']}/complete", json={"outcome": "RAM installed"}, headers=ALICE)
        nxt = (await c.get("/api/quests/board", headers=ALICE)).json()["next"]
        assert [n["quest_id"] for n in nxt] == [after["id"]]


async def test_dependency_validation(env):
    app, _ = env
    async with _client(app) as c:
        a = await _quest(c, ALICE, title="A")
        b = await _quest(c, ALICE, title="B", depends_on=[a["id"]])
        r = await c.put(f"/api/quests/{a['id']}", json={"depends_on": [b["id"]]}, headers=ALICE)
        assert r.status_code == 422
        r = await c.put(f"/api/quests/{a['id']}", json={"depends_on": [a["id"]]}, headers=ALICE)
        assert r.status_code == 422
        bob_q = await _quest(c, BOB, title="Bob's")
        r = await c.post("/api/quests", json={"title": "x", "depends_on": [bob_q["id"]]}, headers=ALICE)
        assert r.status_code == 422


async def test_delete_rules(env):
    app, _ = env
    async with _client(app) as c:
        done = await _quest(c, ALICE, title="Done")
        await c.post(f"/api/quests/{done['id']}/complete", json={"outcome": "ok"}, headers=ALICE)
        assert (await c.delete(f"/api/quests/{done['id']}", headers=ALICE)).status_code == 409

        pre = await _quest(c, ALICE, title="Pre")
        after = await _quest(c, ALICE, title="After", depends_on=[pre["id"]])
        assert (await c.delete(f"/api/quests/{pre['id']}", headers=ALICE)).status_code == 200
        quests = {q["id"]: q for q in (await c.get("/api/quests/board", headers=ALICE)).json()["quests"]}
        assert quests[after["id"]]["depends_on"] == []
        assert quests[after["id"]]["blocked_by"] == []


async def test_campaigns_scope_quests_and_pausing_hides_them_from_next(env):
    app, _ = env
    async with _client(app) as c:
        camp = (await c.post("/api/quests/campaigns", json={"title": "Build Jarvis"}, headers=ALICE)).json()
        q = await _quest(c, ALICE, title="Quest log v1", campaign_id=camp["id"])

        r = await c.post("/api/quests", json={"title": "x", "campaign_id": camp["id"]}, headers=BOB)
        assert r.status_code == 404

        assert (await c.delete(f"/api/quests/campaigns/{camp['id']}", headers=ALICE)).status_code == 409

        await c.put(f"/api/quests/campaigns/{camp['id']}", json={"status": "paused"}, headers=ALICE)
        assert (await c.get("/api/quests/board", headers=ALICE)).json()["next"] == []

        await c.put(f"/api/quests/campaigns/{camp['id']}", json={"status": "active"}, headers=ALICE)
        await c.post(f"/api/quests/{q['id']}/complete", json={"outcome": "shipped"}, headers=ALICE)
        board = (await c.get("/api/quests/board", headers=ALICE)).json()
        c0 = board["campaigns"][0]
        assert (c0["quest_count"], c0["done_count"], c0["xp"]) == (1, 1, 25)


async def test_input_validation(env):
    app, _ = env
    async with _client(app) as c:
        assert (await c.post("/api/quests", json={"title": "  "}, headers=ALICE)).status_code == 422
        r = await c.post("/api/quests", json={"title": "x", "due_date": "next tuesday"}, headers=ALICE)
        assert r.status_code == 422
        q = await _quest(c, ALICE, impact=9, effort=-3, due_date="2026-11-01")
        assert (q["impact"], q["effort"], q["due_date"]) == (3, 1, "2026-11-01")
