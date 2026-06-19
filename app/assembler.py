"""Deterministic source-document assembler.

This is the "AI proposes, the deterministic core disposes" half of the platform:
given a parsed protocol schedule (``FinalSchedule``) it builds the per-visit
source documents a coordinator fills in at each visit -- entirely by code, with
no LLM in the path. The same inputs always produce the same forms, which is what
makes generated source documents defensible (reproducible + attributable).

What it does deterministically:
  * one source document per protocol visit
  * each Schedule-of-Activities procedure becomes a blank field to record
    (result / performed-by / date / time / notes), conditional steps flagged
  * if an enrollment (anchor) date is supplied, each visit's scheduled date is
    computed from the protocol day offset, with the window carried through

The optional ``include_reference_hints`` flag is the *only* place the AI/RAG
layer touches this: it attaches top retrieval hits from previously uploaded
documents as clearly-labelled *reference material*, never as generated content.
The blanks the coordinator fills are always produced by code, not a model.
"""
from datetime import date, timedelta
from typing import List, Optional

from .schemas import FinalSchedule, ExtractedVisit

# Per-procedure blanks a coordinator records at the visit. Kept as an ordered
# list so the rendered form is stable.
_RECORD_FIELDS = ["result", "performed_by", "date", "time", "notes"]


def _scheduled_date(anchor: Optional[date], day: Optional[int]) -> Optional[str]:
    """Compute a visit's calendar date from the enrollment anchor + day offset.

    Convention: protocol **Day 1 = the enrollment/anchor date** (so the offset
    applied is ``day - 1``). Days <= 0 (e.g. screening at Day -7) offset
    backwards naturally. Returns an ISO date string, or None if not computable.
    """
    if anchor is None or day is None:
        return None
    return (anchor + timedelta(days=day - 1)).isoformat()


def _visit_document(visit: ExtractedVisit, anchor: Optional[date]) -> dict:
    """Build one visit's source document (pure)."""
    fields = []
    conditional = []
    for proc in visit.procedures:
        entry = {
            "activity": proc.name,
            "timing": proc.timing,
            "condition": proc.conditional,
            "record": {k: "" for k in _RECORD_FIELDS},
        }
        fields.append(entry)
        if proc.conditional:
            conditional.append(proc.name)
    return {
        "visit": visit.name,
        "cycle_group": visit.cycle_group,
        "day": visit.day,
        "window": visit.window,
        "scheduled_date": _scheduled_date(anchor, visit.day),
        "activity_count": len(fields),
        "fields": fields,
        "conditional_activities": conditional,
    }


def build_source_documents(schedule: FinalSchedule, *,
                           subject_id: Optional[str] = None,
                           enrollment_date: Optional[str] = None,
                           include_reference_hints: bool = False,
                           study_id: Optional[str] = None) -> dict:
    """Assemble per-visit source documents from a parsed schedule.

    ``enrollment_date`` (ISO ``YYYY-MM-DD``) anchors visit-date computation.
    ``include_reference_hints`` attaches RAG excerpts from uploaded documents as
    reference material per visit (optional; requires the docstore).

    Returns ``{subject_id, enrollment_date, document_count, documents:[...]}``.
    Raises ValueError on a malformed ``enrollment_date``.
    """
    anchor = None
    if enrollment_date:
        try:
            anchor = date.fromisoformat(enrollment_date)
        except ValueError as exc:
            raise ValueError(
                f"enrollment_date must be ISO YYYY-MM-DD: {exc}") from exc

    documents = [_visit_document(v, anchor) for v in schedule.visits]

    if include_reference_hints:
        from . import docstore  # lazy: keep the core import-light + LLM-free
        for doc in documents:
            query = f"{doc['visit']} " + " ".join(
                f["activity"] for f in doc["fields"][:8])
            hits = docstore.search(query, study_id=study_id, limit=3)
            doc["reference_hints"] = [
                {"title": h["title"], "score": h["score"],
                 "excerpt": h["content"][:300]}
                for h in hits
            ]

    return {
        "subject_id": subject_id,
        "enrollment_date": enrollment_date,
        "document_count": len(documents),
        "documents": documents,
    }


def render_source_document(doc: dict) -> str:
    """Render one assembled visit document as a fillable plain-text form."""
    lines = [f"SOURCE DOCUMENT — {doc['visit']}"]
    meta = []
    if doc.get("day") is not None:
        meta.append(f"Day {doc['day']}")
    if doc.get("window"):
        meta.append(f"window {doc['window']}")
    if doc.get("scheduled_date"):
        meta.append(f"scheduled {doc['scheduled_date']}")
    if meta:
        lines.append("  (" + ", ".join(meta) + ")")
    lines.append("")
    for i, f in enumerate(doc["fields"], 1):
        tag = "  [CONDITIONAL]" if f.get("condition") else ""
        lines.append(f"{i}. {f['activity']}{tag}")
        if f.get("condition"):
            lines.append(f"     only if: {f['condition']}")
        lines.append("     Result: ______________________   Performed by: ____________")
        lines.append("     Date: __________  Time: ________  Notes: ____________________")
    return "\n".join(lines)


def render_packet(assembled: dict) -> str:
    """Render a full assembled packet (all visits) as one plain-text document."""
    header = ["SOURCE DOCUMENT PACKET"]
    if assembled.get("subject_id"):
        header.append(f"Subject: {assembled['subject_id']}")
    if assembled.get("enrollment_date"):
        header.append(f"Enrollment (Day 1): {assembled['enrollment_date']}")
    header.append(f"Visits: {assembled['document_count']}")
    body = [render_source_document(d) for d in assembled["documents"]]
    return "\n".join(header) + "\n\n" + ("\n\n" + ("-" * 60) + "\n\n").join(body)
