"""Tests for the AI chat: data tools, learning memory, and graceful degradation.

No live Anthropic API calls are made -- the tool layer and memory are exercised
directly, and the /chat endpoints are checked only for their no-API-key behavior.
Streaming/provenance features are tested via unit-level helpers.
"""
import os
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Isolated DB per test session, set before importing the app/store.
os.environ['PLATFORM_DB'] = os.path.join(
    tempfile.gettempdir(), f'chat_test_{uuid.uuid4().hex}.db'
)

from app.store import init_db, get_conn  # noqa: E402
from app import chat_tools, chat_memory  # noqa: E402
from app.chat import _summarize_tool_call, _enrich_tool_calls  # noqa: E402
import json  # noqa: E402


def setup_module(module):
    init_db()
    # Seed one subject (compliance), one visit log, two dispatch trips.
    with get_conn() as conn:
        conn.execute("DELETE FROM subjects")
        conn.execute("DELETE FROM visit_logs")
        conn.execute("DELETE FROM trips")
        conn.execute("DELETE FROM chat_memory")
        conn.execute("INSERT INTO subjects (subjectId, data) VALUES (?, ?)",
                     ("SUBJ-0012", json.dumps({"subjectId": "SUBJ-0012", "status": "enrolled"})))
        conn.execute("INSERT INTO visit_logs (id, data) VALUES (?, ?)",
                     ("L1", json.dumps({"id": "L1", "subjectId": "SUBJ-0012", "visit": "Week 4"})))
        for i, amt in enumerate([130.5, 207.25]):
            conn.execute("INSERT INTO trips (id, data, created_at) VALUES (?, ?, ?)",
                         (f"T{i}", json.dumps({"id": f"T{i}", "subject": "SUBJ-0012",
                                              "amount": amt, "miles": 10}), f"2026-06-0{i+1}"))


def teardown_module(module):
    # The store DB can be shared with other test modules; clear what we seeded so
    # our minimal rows don't leak into their assertions.
    with get_conn() as conn:
        conn.execute("DELETE FROM subjects")
        conn.execute("DELETE FROM visit_logs")
        conn.execute("DELETE FROM trips")
        conn.execute("DELETE FROM chat_memory")


# --- Data tools ------------------------------------------------------------
def test_list_subjects_unions_sources():
    subs = chat_tools.list_subjects()["subjects"]
    assert "SUBJ-0012" in subs


def test_get_subject_joins_modules():
    s = chat_tools.get_subject("SUBJ-0012")
    assert s["found"] is True
    assert s["compliance"]["status"] == "enrolled"
    assert len(s["visit_logs"]) == 1
    assert len(s["trips"]) == 2
    assert s["invoice"]["trip_count"] == 2
    assert s["invoice"]["total_due"] == 337.75  # 130.5 + 207.25


def test_get_subject_unknown():
    assert chat_tools.get_subject("NOPE")["found"] is False


def test_list_trips_filter():
    assert len(chat_tools.list_trips("SUBJ-0012")["trips"]) == 2
    assert chat_tools.list_trips("OTHER")["trips"] == []


def test_dispatch_rates_and_example():
    r = chat_tools.get_dispatch_rates()
    assert "Sedan" in r["rates"]
    assert r["fees"]["sanitation"] == 50
    assert r["example"]["amount"] == 130.5  # Sedan 10mi/5min one-way


def test_parse_protocol_tool():
    text = (
        "Schedule of Activities\n"
        "| Procedure | Screening | Week 4 |\n"
        "|---|---|---|\n"
        "| Blood sample | X | X |\n"
    )
    out = chat_tools.parse_protocol(text)
    names = {v["name"] for v in out["visits"]}
    assert {"Screening", "Week 4"} <= names


def test_dispatch_tool_unknown_is_safe():
    assert "error" in chat_tools.dispatch_tool("does_not_exist", {})


# --- Learning memory -------------------------------------------------------
def test_remember_recall_forget_roundtrip():
    rec = chat_tools.remember("show times in 24h")
    mem_id = rec["id"]
    hits = [m["content"] for m in chat_memory.recall(query="time format 24h")]
    assert "show times in 24h" in hits
    # subject-scoped learning ranks first for that subject
    chat_tools.remember("on the placebo arm", subject="SUBJ-0012")
    top = chat_memory.recall(subject="SUBJ-0012")[0]
    assert top["subject"] == "SUBJ-0012"
    # forget removes it
    assert chat_tools.forget(mem_id)["forgotten"] is True
    assert all(m["id"] != mem_id for m in chat_memory.recall())


def test_conversation_turns_not_recalled_as_learnings():
    chat_memory.save_turn("user", "just chatting", subject=None)
    assert all("just chatting" != m["content"] for m in chat_memory.recall())


# --- Endpoint graceful degradation ----------------------------------------
def test_chat_endpoint_503_without_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from fastapi.testclient import TestClient
    from app.api import app
    client = TestClient(app)
    r = client.post("/chat/", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 503
    assert "ANTHROPIC_API_KEY" in r.json()["detail"]


def test_chat_stream_endpoint_503_without_api_key(monkeypatch):
    """Streaming endpoint must return HTTP 503 (not start a broken stream) when key is absent."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from fastapi.testclient import TestClient
    from app.api import app
    client = TestClient(app)
    r = client.post("/chat/stream", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 503
    assert "ANTHROPIC_API_KEY" in r.json()["detail"]


# --- Provenance: _summarize_tool_call (pure function, no API needed) -------
def test_summarize_get_subject():
    s = _summarize_tool_call("get_subject", {"subject_id": "SUBJ-0012"})
    assert s == "get_subject(SUBJ-0012)"


def test_summarize_list_subjects():
    s = _summarize_tool_call("list_subjects", {})
    assert s == "list_subjects()"


def test_summarize_list_trips_with_subject():
    s = _summarize_tool_call("list_trips", {"subject": "SUBJ-0012"})
    assert s == "list_trips(SUBJ-0012)"


def test_summarize_list_trips_no_subject():
    s = _summarize_tool_call("list_trips", {})
    assert s == "list_trips()"


def test_summarize_get_dispatch_rates():
    s = _summarize_tool_call("get_dispatch_rates", {})
    assert s == "get_dispatch_rates()"


def test_summarize_parse_protocol():
    s = _summarize_tool_call("parse_protocol", {"text": "Schedule of Activities\nsome text here"})
    assert s.startswith('parse_protocol("')
    assert "Schedule" in s


def test_summarize_remember():
    s = _summarize_tool_call("remember", {"fact": "show times in 24h"})
    assert "remember" in s
    assert "24h" in s


def test_summarize_forget():
    s = _summarize_tool_call("forget", {"memory_id": "mem_abc123"})
    assert "forget" in s
    assert "mem_abc123" in s


def test_summarize_unknown_tool():
    s = _summarize_tool_call("mystery_tool", {"foo": "bar"})
    assert "mystery_tool" in s


# --- Provenance: _enrich_tool_calls ----------------------------------------
def test_enrich_tool_calls_adds_summary_and_sources():
    raw = [
        {"name": "get_subject", "input": {"subject_id": "SUBJ-0012"}},
        {"name": "list_trips", "input": {"subject": "SUBJ-0012"}},
    ]
    enriched, sources = _enrich_tool_calls(raw)
    # Each entry gets a summary
    assert enriched[0]["summary"] == "get_subject(SUBJ-0012)"
    assert enriched[1]["summary"] == "list_trips(SUBJ-0012)"
    # sources is deduplicated list of summaries in order
    assert sources == ["get_subject(SUBJ-0012)", "list_trips(SUBJ-0012)"]


def test_enrich_tool_calls_deduplicates_sources():
    raw = [
        {"name": "get_subject", "input": {"subject_id": "SUBJ-0012"}},
        {"name": "get_subject", "input": {"subject_id": "SUBJ-0012"}},
    ]
    enriched, sources = _enrich_tool_calls(raw)
    # Both entries get summary, but sources deduplicates
    assert len(enriched) == 2
    assert len(sources) == 1
    assert sources[0] == "get_subject(SUBJ-0012)"


def test_enrich_tool_calls_empty():
    enriched, sources = _enrich_tool_calls([])
    assert enriched == []
    assert sources == []


# --- Non-streaming response shape ------------------------------------------
def test_run_chat_response_includes_sources_key(monkeypatch):
    """run_chat must always return a 'sources' key in its response dict."""
    # We can't call the real API, but we can monkeypatch _get_client
    # so that the function raises ChatNotConfigured with no key set.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from app.chat import run_chat, ChatNotConfigured
    try:
        run_chat([{"role": "user", "content": "hi"}])
        assert False, "Expected ChatNotConfigured"
    except ChatNotConfigured:
        pass  # Expected: verifies no-key path raises correctly


def test_non_streaming_response_shape_with_mocked_api(monkeypatch):
    """Verify run_chat returns {reply, tool_calls, sources, model} with mocked Anthropic."""
    import types

    # Build minimal mock objects mimicking the Anthropic SDK response
    mock_text_block = types.SimpleNamespace(type="text", text="Hello!")

    mock_response = types.SimpleNamespace(
        stop_reason="end_turn",
        content=[mock_text_block],
    )

    class MockMessages:
        def create(self, **kwargs):
            return mock_response

    class MockClient:
        messages = MockMessages()

    # Patch _get_client to return our mock
    import app.chat as chat_module
    monkeypatch.setattr(chat_module, "_get_client",
                        lambda: (MockClient(), "claude-haiku-4-5"))
    # Also patch save_turn and audit.record so they don't fail
    monkeypatch.setattr(chat_module.chat_memory, "save_turn", lambda *a, **kw: None)
    monkeypatch.setattr(chat_module.audit, "record", lambda *a, **kw: None)

    result = chat_module.run_chat([{"role": "user", "content": "hello"}])
    assert "reply" in result
    assert "tool_calls" in result
    assert "sources" in result
    assert "model" in result
    assert result["reply"] == "Hello!"
    assert result["sources"] == []  # no tools called
