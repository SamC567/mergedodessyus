"""Tests for cloud_privacy.py — private data never reaches cloud endpoints."""

import pytest

import src.cloud_privacy as cloud_privacy
import src.model_context as model_context
from src.cloud_privacy import (
    PRIVATE_DATA_TOOL_NAMES,
    foreground_route_is_cloud,
    is_cloud_url,
    route_is_cloud,
)

LOCAL = "http://127.0.0.1:8080/v1"
CLOUD = "https://api.anthropic.com/v1"


@pytest.fixture(autouse=True)
def _guard_on_no_endpoint_db(monkeypatch):
    monkeypatch.setenv("ODYSSEUS_CLOUD_PRIVACY_GUARD", "1")
    # Classify by host only; don't touch the real endpoint table.
    monkeypatch.setattr(model_context, "_configured_endpoint_kind", lambda url: None)


class TestClassification:
    def test_local_and_cloud(self):
        assert is_cloud_url(LOCAL) is False
        assert is_cloud_url("http://localhost:11434/v1") is False
        assert is_cloud_url("http://192.168.1.20:8000/v1") is False
        assert is_cloud_url(CLOUD) is True
        assert is_cloud_url("https://api.openai.com/v1") is True

    def test_guard_can_be_switched_off(self, monkeypatch):
        monkeypatch.setenv("ODYSSEUS_CLOUD_PRIVACY_GUARD", "0")
        assert is_cloud_url(CLOUD) is False
        assert route_is_cloud(LOCAL, [(CLOUD, "c", {})]) is False

    def test_empty_url_reaches_nothing(self):
        assert is_cloud_url("") is False
        assert is_cloud_url(None) is False

    def test_unclassifiable_fails_closed(self, monkeypatch):
        def boom(url):
            raise RuntimeError("db down")
        monkeypatch.setattr(model_context, "is_local_endpoint", boom)
        assert is_cloud_url(LOCAL) is True

    def test_cloud_fallback_makes_route_cloud(self):
        assert route_is_cloud(LOCAL) is False
        assert route_is_cloud(LOCAL, [(LOCAL, "m", {})]) is False
        assert route_is_cloud(LOCAL, [(CLOUD, "claude", {})]) is True
        assert route_is_cloud(CLOUD) is True

    def test_foreground_route_reads_owner_fallbacks(self, monkeypatch):
        import src.foreground_model_routing as fmr
        monkeypatch.setattr(fmr, "resolve_foreground_fallback_candidates", lambda owner=None: [(CLOUD, "c", {})])
        assert foreground_route_is_cloud(LOCAL, "sam") is True
        monkeypatch.setattr(fmr, "resolve_foreground_fallback_candidates", lambda owner=None: [])
        assert foreground_route_is_cloud(LOCAL, "sam") is False


class TestToolList:
    def test_names_are_real_tools(self):
        from src.tool_policy import known_tool_names
        assert PRIVATE_DATA_TOOL_NAMES <= known_tool_names()

    def test_covers_memory_docs_files_and_mail(self):
        for name in ("manage_memory", "search_chats", "manage_documents", "read_file", "bash", "read_email", "vault_get"):
            assert name in PRIVATE_DATA_TOOL_NAMES
        assert "web_search" not in PRIVATE_DATA_TOOL_NAMES


class _Mem:
    def __init__(self):
        self.loads = 0

    def load(self, owner=None):
        self.loads += 1
        return [{"id": "1", "text": "secret fact", "pinned": True}]


class _Rag:
    def __init__(self):
        self.searches = 0

    def search(self, message, k=5, owner=None):
        self.searches += 1
        return [{"document": "private doc", "metadata": {"filename": "x.txt"}, "similarity": 1.0}]


class _Docs:
    def __init__(self):
        self.rag_manager = _Rag()


class _Session:
    def __init__(self, url):
        self.endpoint_url = url
        self.model = "m"
        self.headers = {}


class TestContextPreface:
    def _processor(self, monkeypatch):
        import src.foreground_model_routing as fmr
        monkeypatch.setattr(fmr, "resolve_foreground_fallback_candidates", lambda owner=None: [])
        from src.chat_processor import ChatProcessor
        return ChatProcessor(_Mem(), _Docs())

    def test_cloud_session_gets_no_memory_or_rag(self, monkeypatch):
        cp = self._processor(monkeypatch)
        preface, rag_sources, _ = cp.build_context_preface(
            "tell me the secret", _Session(CLOUD), use_memory=True, use_rag=True, use_skills=False,
        )
        assert cp.memory_manager.loads == 0
        assert cp.personal_docs_manager.rag_manager.searches == 0
        assert rag_sources == []
        assert "secret fact" not in str(preface) and "private doc" not in str(preface)

    def test_local_session_still_gets_rag(self, monkeypatch):
        cp = self._processor(monkeypatch)
        cp.build_context_preface(
            "tell me the secret", _Session(LOCAL), use_memory=True, use_rag=True, use_skills=False,
        )
        assert cp.memory_manager.loads == 1
        assert cp.personal_docs_manager.rag_manager.searches == 1
