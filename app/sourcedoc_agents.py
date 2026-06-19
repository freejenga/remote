"""Agentic source-document generation (generation -> formatting -> critic).

This is the AI layer that sits *on top of* the deterministic ``assembler``. The
assembler always produces the authoritative skeleton (one form per visit, with
the record blanks a coordinator fills); these agents only propose *wording and
structure* -- worksheet fields, eligibility-checklist items, section layout --
and then verify that proposal against the raw protocol. The blanks themselves
are never invented by a model.

Three roles, mirroring ``graph.py``'s optional-LangGraph + linear-fallback shape:

  * **generation** -- given a visit's activities and the retrieved protocol
    passages (via ``docstore``), proposes fields / checklist items.
  * **formatting** -- normalizes the draft into the source-document template.
  * **critic** -- re-reads the protocol passages and checks the draft for
    misalignment (missing/extra activities, wrong conditional, wrong visit).
    If it isn't aligned, control loops back to generation (bounded by
    ``max_iterations``) with the critique as feedback.

Every LLM call goes through the single module-level ``_call_llm`` seam, so the
whole pipeline is unit-testable offline by monkeypatching it. Retrieval uses the
already-de-identified docstore corpus, so the de-identification boundary holds.
"""
import json
import os
import re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException

from .schemas import FinalSchedule
from . import assembler, audit

try:
    from langgraph.graph import StateGraph, END
    _LANGGRAPH_AVAILABLE = True
except ImportError:  # pragma: no cover - langgraph is optional
    _LANGGRAPH_AVAILABLE = False

router = APIRouter(prefix="/source-documents", tags=["source-documents"])

# Bound on the generation<->critic revision loop.
DEFAULT_MAX_ITERATIONS = 2

_GEN_SYSTEM = (
    "You are the GENERATION agent for clinical-trial source documents. Given a "
    "visit's scheduled activities and excerpts from the trial protocol, propose "
    "the fields a coordinator worksheet or eligibility checklist should contain. "
    "Stay strictly within what the protocol and activity list support; never "
    "invent procedures. Respond ONLY with JSON: "
    '{"fields":[{"label":str,"kind":"worksheet"|"checklist","basis":str}],'
    '"notes":str}.'
)
_FMT_SYSTEM = (
    "You are the FORMATTING agent for clinical-trial source documents. Normalize "
    "the draft fields into a clean source-document template. Respond ONLY with "
    'JSON: {"title":str,"sections":[{"heading":str,"items":[str]}]}.'
)
_CRITIC_SYSTEM = (
    "You are the CRITIC agent. Compare the formatted source document against the "
    "raw protocol excerpts and the authoritative activity list. Flag any "
    "misalignment: activities present in the protocol but missing from the doc, "
    "items in the doc not supported by the protocol, or wrong conditional/visit "
    "labelling. Respond ONLY with JSON: "
    '{"aligned":bool,"issues":[str],"missing_activities":[str],'
    '"extra_activities":[str]}.'
)


class SourceDocNotConfigured(RuntimeError):
    """Raised when the LLM backend (ANTHROPIC_API_KEY/anthropic) is unavailable."""


# ---------------------------------------------------------------------------
# LLM seam (monkeypatched in tests)
# ---------------------------------------------------------------------------
def _parse_json(text: str) -> dict:
    """Extract the first JSON object from a model response."""
    if not text:
        raise ValueError("empty LLM response")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise ValueError("no JSON object in LLM response")
        return json.loads(m.group(0))


def _call_llm(system: str, prompt: str) -> dict:
    """Call the Anthropic model and parse a JSON object from the reply.

    Reuses the chat module's client construction so configuration/credentials
    live in one place. Raises ``SourceDocNotConfigured`` when no key/package.
    """
    from .chat import _get_client, ChatNotConfigured
    try:
        client, model = _get_client()
    except ChatNotConfigured as exc:
        raise SourceDocNotConfigured(str(exc)) from exc
    import anthropic
    try:
        resp = client.messages.create(
            model=model, max_tokens=2000, system=system,
            messages=[{"role": "user", "content": prompt}])
    except anthropic.APIError as exc:
        raise SourceDocNotConfigured(f"AI provider error: {exc}") from exc
    text = "".join(b.text for b in resp.content if b.type == "text")
    return _parse_json(text)


# ---------------------------------------------------------------------------
# Retrieval + prompt builders
# ---------------------------------------------------------------------------
def _retrieve_context(visit: dict, study_id: Optional[str]) -> str:
    """Pull the protocol passages most relevant to this visit (de-identified)."""
    from . import docstore
    activities = [f.get("activity", "") for f in visit.get("fields", [])][:8]
    query = (visit.get("visit", "") + " " + " ".join(activities)).strip()
    if not query:
        return ""
    hits = docstore.search(query, study_id=study_id, limit=4)
    return "\n\n".join(
        f"[{h.get('section') or h.get('title')}] {h['content']}" for h in hits)


def _activity_list(visit: dict) -> List[str]:
    return [f.get("activity", "") for f in visit.get("fields", [])]


def _gen_prompt(visit: dict, context: str, feedback: dict) -> str:
    parts = [
        f"Visit: {visit.get('visit')}",
        f"Authoritative scheduled activities: {json.dumps(_activity_list(visit))}",
        f"Conditional activities: {json.dumps(visit.get('conditional_activities', []))}",
        "Protocol excerpts:\n" + (context or "(none available)"),
    ]
    if feedback and feedback.get("issues"):
        parts.append("Revise to fix these critic issues: "
                     + json.dumps(feedback.get("issues")))
    return "\n\n".join(parts)


def _fmt_prompt(visit: dict, draft: dict) -> str:
    return (f"Visit: {visit.get('visit')}\n\nDraft fields to format:\n"
            + json.dumps(draft, default=str))


def _critic_prompt(visit: dict, formatted: dict, context: str) -> str:
    return "\n\n".join([
        f"Visit: {visit.get('visit')}",
        f"Authoritative scheduled activities: {json.dumps(_activity_list(visit))}",
        "Formatted source document:\n" + json.dumps(formatted, default=str),
        "Protocol excerpts:\n" + (context or "(none available)"),
    ])


# ---------------------------------------------------------------------------
# Agents (pure given _call_llm / docstore)
# ---------------------------------------------------------------------------
def generation_agent(state: Dict[str, Any]) -> dict:
    visit = state["visit"]
    context = state.get("protocol_context")
    if context is None:
        context = _retrieve_context(visit, state.get("study_id"))
    draft = _call_llm(_GEN_SYSTEM, _gen_prompt(visit, context, state.get("critique") or {}))
    return {"draft": draft, "protocol_context": context,
            "iterations": state.get("iterations", 0) + 1}


def formatting_agent(state: Dict[str, Any]) -> dict:
    formatted = _call_llm(_FMT_SYSTEM, _fmt_prompt(state["visit"], state["draft"]))
    return {"formatted": formatted}


def critic_agent(state: Dict[str, Any]) -> dict:
    critique = _call_llm(
        _CRITIC_SYSTEM,
        _critic_prompt(state["visit"], state["formatted"],
                       state.get("protocol_context") or ""))
    critique["aligned"] = bool(critique.get("aligned"))
    return {"critique": critique}


def _route_after_critic(state: Dict[str, Any]) -> str:
    critique = state.get("critique") or {}
    if critique.get("aligned"):
        return "done"
    if state.get("iterations", 0) >= state.get("max_iterations", DEFAULT_MAX_ITERATIONS):
        return "done"
    return "revise"


# ---------------------------------------------------------------------------
# Pipeline runner (langgraph if present, else linear loop)
# ---------------------------------------------------------------------------
_graph_cache = None


def _build_graph():
    g = StateGraph(dict)
    g.add_node("generate", generation_agent)
    g.add_node("format", formatting_agent)
    g.add_node("critic", critic_agent)
    g.set_entry_point("generate")
    g.add_edge("generate", "format")
    g.add_edge("format", "critic")
    g.add_conditional_edges("critic", _route_after_critic,
                            {"revise": "generate", "done": END})
    return g.compile()


def _run_pipeline(state: Dict[str, Any]) -> Dict[str, Any]:
    if _LANGGRAPH_AVAILABLE:
        global _graph_cache
        if _graph_cache is None:
            _graph_cache = _build_graph()
        return dict(_graph_cache.invoke(state))
    # Linear fallback: same generate -> format -> critic -> (revise|done) loop.
    state = dict(state)
    while True:
        state.update(generation_agent(state))
        state.update(formatting_agent(state))
        state.update(critic_agent(state))
        if _route_after_critic(state) == "done":
            break
    return state


def generate_for_visit(visit_doc: dict, study_id: Optional[str],
                       max_iterations: int) -> dict:
    """Run the agent pipeline for one assembled visit skeleton."""
    final = _run_pipeline({
        "visit": visit_doc, "study_id": study_id,
        "iterations": 0, "max_iterations": max_iterations,
        "protocol_context": None,
    })
    critique = final.get("critique") or {}
    return {
        "visit": visit_doc.get("visit"),
        "skeleton": visit_doc,
        "generated": final.get("formatted") or final.get("draft"),
        "critique": critique,
        "iterations": final.get("iterations"),
        "approved": bool(critique.get("aligned")),
    }


def generate_source_documents(schedule: FinalSchedule, *,
                              subject_id: Optional[str] = None,
                              enrollment_date: Optional[str] = None,
                              study_id: Optional[str] = None,
                              visit_name: Optional[str] = None,
                              max_iterations: int = DEFAULT_MAX_ITERATIONS) -> dict:
    """Assemble deterministic skeletons, then critic-verify AI-generated content.

    The deterministic ``assembler`` provides the authoritative per-visit
    skeleton; the agent pipeline enriches and self-checks each one. Raises
    ValueError on a bad enrollment date or unknown visit, and
    ``SourceDocNotConfigured`` when the LLM backend isn't available.
    """
    assembled = assembler.build_source_documents(
        schedule, subject_id=subject_id, enrollment_date=enrollment_date,
        study_id=study_id)
    docs = assembled["documents"]
    if visit_name:
        docs = [d for d in docs if d["visit"] == visit_name]
        if not docs:
            raise ValueError(f"visit not found: {visit_name}")

    generated = [generate_for_visit(d, study_id, max_iterations) for d in docs]
    return {
        "subject_id": subject_id,
        "enrollment_date": enrollment_date,
        "document_count": len(generated),
        "approved_count": sum(1 for g in generated if g["approved"]),
        "documents": generated,
    }


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
@router.post("/generate")
def post_generate(schedule: FinalSchedule, subject_id: str = None,
                  enrollment_date: str = None, study_id: str = None,
                  visit_name: str = None,
                  max_iterations: int = DEFAULT_MAX_ITERATIONS):
    """Generate critic-verified source documents from a parsed schedule.

    Layers the generation/formatting/critic agents over the deterministic
    assembler. ``study_id`` scopes protocol retrieval; ``visit_name`` limits to
    a single visit. Returns 503 if the AI backend isn't configured.
    """
    try:
        result = generate_source_documents(
            schedule, subject_id=subject_id, enrollment_date=enrollment_date,
            study_id=study_id, visit_name=visit_name,
            max_iterations=max(1, min(max_iterations, 5)))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except SourceDocNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    audit.record("source_documents.generate",
                 {"subject_id": subject_id, "visits": result["document_count"],
                  "approved": result["approved_count"]})
    return result
