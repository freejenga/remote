"""Tests for the agentic source-document pipeline (generation/format/critic).

All tests run offline by monkeypatching the single ``_call_llm`` seam, so no
API key or network is required.
"""
import os
import tempfile

os.environ['PLATFORM_DB'] = os.path.join(tempfile.gettempdir(), 'sourcedoc_test.db')
if os.path.exists(os.environ['PLATFORM_DB']):
    os.remove(os.environ['PLATFORM_DB'])

import pytest
from fastapi.testclient import TestClient

from app.api import app
from app import sourcedoc_agents as sda
from app.schemas import FinalSchedule, ExtractedVisit, ExtractedProcedure, SourceSpan

client = TestClient(app)


def _span():
    return SourceSpan(snippet="x", parser_route="soa")


def _schedule():
    return FinalSchedule(visits=[
        ExtractedVisit(name="Baseline", day=1, window="±0", procedures=[
            ExtractedProcedure(name="Vitals", source=_span()),
            ExtractedProcedure(name="ECG", source=_span()),
        ]),
    ])


def _fake_llm(critic_sequence):
    """Build a fake _call_llm that returns role-appropriate JSON.

    ``critic_sequence`` is the list of critic verdicts to return in order
    (the last one repeats if more critic calls happen).
    """
    state = {"critic": 0}

    def fake(system, prompt):
        if "CRITIC agent" in system:
            i = min(state["critic"], len(critic_sequence) - 1)
            state["critic"] += 1
            return critic_sequence[i]
        if "FORMATTING agent" in system:
            return {"title": "Baseline Worksheet",
                    "sections": [{"heading": "Vitals", "items": ["BP", "HR"]}]}
        # generation
        return {"fields": [{"label": "BP", "kind": "worksheet", "basis": "vitals"}],
                "notes": ""}

    return fake, state


def _skeleton():
    from app import assembler
    return assembler.build_source_documents(_schedule())["documents"][0]


def test_critic_loop_rejects_then_passes(monkeypatch):
    fake, state = _fake_llm([
        {"aligned": False, "issues": ["missing ECG"], "missing_activities": ["ECG"]},
        {"aligned": True, "issues": []},
    ])
    monkeypatch.setattr(sda, "_call_llm", fake)

    out = sda.generate_for_visit(_skeleton(), study_id=None, max_iterations=2)
    assert out["approved"] is True
    assert out["iterations"] == 2          # one revision happened
    assert state["critic"] == 2
    assert out["generated"]["title"] == "Baseline Worksheet"


def test_critic_loop_stops_at_max_iterations(monkeypatch):
    fake, _ = _fake_llm([{"aligned": False, "issues": ["still wrong"]}])
    monkeypatch.setattr(sda, "_call_llm", fake)

    out = sda.generate_for_visit(_skeleton(), study_id=None, max_iterations=2)
    assert out["approved"] is False
    assert out["iterations"] == 2          # bounded, didn't loop forever


def test_linear_fallback_without_langgraph(monkeypatch):
    fake, _ = _fake_llm([{"aligned": True, "issues": []}])
    monkeypatch.setattr(sda, "_call_llm", fake)
    monkeypatch.setattr(sda, "_LANGGRAPH_AVAILABLE", False)

    out = sda.generate_for_visit(_skeleton(), study_id=None, max_iterations=2)
    assert out["approved"] is True
    assert out["iterations"] == 1


def test_generate_source_documents_over_schedule(monkeypatch):
    fake, _ = _fake_llm([{"aligned": True, "issues": []}])
    monkeypatch.setattr(sda, "_call_llm", fake)

    result = sda.generate_source_documents(_schedule(), subject_id="SUBJ-9")
    assert result["document_count"] == 1
    assert result["approved_count"] == 1
    assert result["documents"][0]["visit"] == "Baseline"
    # The deterministic skeleton is carried alongside the generated content.
    assert result["documents"][0]["skeleton"]["activity_count"] == 2


def test_endpoint_generate(monkeypatch):
    fake, _ = _fake_llm([{"aligned": True, "issues": []}])
    monkeypatch.setattr(sda, "_call_llm", fake)

    r = client.post("/source-documents/generate",
                    params={"subject_id": "SUBJ-1", "enrollment_date": "2026-06-15"},
                    json=_schedule().model_dump())
    assert r.status_code == 200
    body = r.json()
    assert body["document_count"] == 1 and body["approved_count"] == 1


def test_endpoint_rejects_bad_date():
    # Bad date fails in the deterministic assembler before any LLM call.
    r = client.post("/source-documents/generate",
                    params={"enrollment_date": "not-a-date"},
                    json=_schedule().model_dump())
    assert r.status_code == 400


def test_endpoint_503_when_llm_unconfigured(monkeypatch):
    # No ANTHROPIC_API_KEY -> _call_llm raises SourceDocNotConfigured -> 503.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = client.post("/source-documents/generate",
                    json=_schedule().model_dump())
    assert r.status_code == 503


def test_unknown_visit_rejected(monkeypatch):
    fake, _ = _fake_llm([{"aligned": True}])
    monkeypatch.setattr(sda, "_call_llm", fake)
    with pytest.raises(ValueError):
        sda.generate_source_documents(_schedule(), visit_name="Nonexistent")


# --- robust JSON parsing + graceful degradation ----------------------------
def test_parse_json_plain():
    assert sda._parse_json('{"aligned": true}') == {"aligned": True}


def test_parse_json_strips_code_fence():
    assert sda._parse_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_parse_json_ignores_trailing_prose():
    out = sda._parse_json('Here is the result:\n{"a": 1, "b": [2, 3]}\nThanks!')
    assert out == {"a": 1, "b": [2, 3]}


def test_parse_json_truncated_raises_clean_error():
    # A reply cut off mid-object (the original "Expecting ',' delimiter" cause).
    with pytest.raises(ValueError):
        sda._parse_json('{"fields": [{"label": "BP", "kind": "workshe')


def test_generate_for_visit_degrades_on_bad_json(monkeypatch):
    def boom(system, prompt):
        raise sda.SourceDocError("model did not return valid JSON")
    monkeypatch.setattr(sda, "_call_llm", boom)

    out = sda.generate_for_visit(_skeleton(), study_id=None, max_iterations=2)
    assert out["approved"] is False
    assert out["generated"] is None
    assert "error" in out
    # The deterministic skeleton is still returned intact.
    assert out["skeleton"]["activity_count"] == 2
