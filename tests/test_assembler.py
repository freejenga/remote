"""Tests for the deterministic source-document assembler."""
import os
import tempfile

# Isolated throwaway DB before any app import.
os.environ['PLATFORM_DB'] = os.path.join(tempfile.gettempdir(), 'assembler_test.db')
if os.path.exists(os.environ['PLATFORM_DB']):
    os.remove(os.environ['PLATFORM_DB'])

import pytest
from fastapi.testclient import TestClient

from app.api import app
from app import assembler
from app.schemas import (FinalSchedule, ExtractedVisit, ExtractedProcedure,
                         SourceSpan)

client = TestClient(app)


def _span():
    return SourceSpan(snippet="x", parser_route="soa")


def _schedule():
    return FinalSchedule(visits=[
        ExtractedVisit(name="Screening", day=-7, window="±3 days", procedures=[
            ExtractedProcedure(name="Informed Consent", source=_span()),
            ExtractedProcedure(name="Pregnancy Test",
                               conditional="females of childbearing potential",
                               source=_span()),
        ]),
        ExtractedVisit(name="Baseline", day=1, window="±0", procedures=[
            ExtractedProcedure(name="Vitals", source=_span()),
        ]),
    ])


def test_one_document_per_visit_with_blank_fields():
    out = assembler.build_source_documents(_schedule())
    assert out["document_count"] == 2
    screening = out["documents"][0]
    assert screening["visit"] == "Screening"
    assert screening["activity_count"] == 2
    # Each field has the blank record slots and no value pre-filled.
    rec = screening["fields"][0]["record"]
    assert set(rec) == {"result", "performed_by", "date", "time", "notes"}
    assert all(v == "" for v in rec.values())


def test_conditional_activities_flagged():
    out = assembler.build_source_documents(_schedule())
    screening = out["documents"][0]
    assert "Pregnancy Test" in screening["conditional_activities"]


def test_visit_dates_computed_from_anchor():
    # Day 1 == enrollment date; Day -7 is seven days earlier.
    out = assembler.build_source_documents(_schedule(), enrollment_date="2026-06-15")
    by_visit = {d["visit"]: d for d in out["documents"]}
    assert by_visit["Baseline"]["scheduled_date"] == "2026-06-15"
    assert by_visit["Screening"]["scheduled_date"] == "2026-06-07"


def test_determinism():
    a = assembler.build_source_documents(_schedule(), enrollment_date="2026-01-01")
    b = assembler.build_source_documents(_schedule(), enrollment_date="2026-01-01")
    assert a == b


def test_bad_enrollment_date_rejected():
    with pytest.raises(ValueError):
        assembler.build_source_documents(_schedule(), enrollment_date="June 1st")


def test_render_packet_is_fillable_text():
    out = assembler.build_source_documents(_schedule(), subject_id="SUBJ-0007",
                                           enrollment_date="2026-06-15")
    text = assembler.render_packet(out)
    assert "SUBJ-0007" in text
    assert "Screening" in text and "Baseline" in text
    assert "Result:" in text  # the blank to fill
    assert "[CONDITIONAL]" in text


def test_reference_hints_pull_from_docstore():
    from app import docstore
    docstore.add_document("Vitals SOP",
                          "Record blood pressure and heart rate as part of vitals.")
    out = assembler.build_source_documents(
        _schedule(), include_reference_hints=True)
    baseline = [d for d in out["documents"] if d["visit"] == "Baseline"][0]
    assert "reference_hints" in baseline
    # The vitals SOP should surface for the vitals visit.
    assert any("vitals" in h["title"].lower() for h in baseline["reference_hints"])


def test_endpoint_assembles_and_renders():
    sched = _schedule().model_dump()
    r = client.post('/source-documents',
                    params={'subject_id': 'SUBJ-0001',
                            'enrollment_date': '2026-06-15', 'render': True},
                    json=sched)
    assert r.status_code == 200
    body = r.json()
    assert body["document_count"] == 2
    assert "rendered" in body and "SOURCE DOCUMENT PACKET" in body["rendered"]


def test_endpoint_rejects_bad_date():
    r = client.post('/source-documents',
                    params={'enrollment_date': 'not-a-date'},
                    json=_schedule().model_dump())
    assert r.status_code == 400
