"""Read-only (plus learning) tools the AI chat can call.

Each tool is a plain function returning JSON-serializable data, backed by the
shared store and the existing platform modules. ``TOOLS`` is the Anthropic
tool-schema list and ``dispatch_tool`` routes a tool name to its function, so
``chat.py`` stays a thin agentic loop.
"""
from typing import Optional

from .store import get_conn, unpack
from .dispatch import calculate_cost, RATES, FEES
from . import chat_memory, docstore


# --- Platform data accessors ----------------------------------------------
def list_subjects() -> dict:
    """All known subject IDs across compliance records and dispatch trips."""
    with get_conn() as conn:
        subj_rows = conn.execute("SELECT subjectId FROM subjects").fetchall()
        trip_rows = conn.execute("SELECT data FROM trips").fetchall()
    ids = {r["subjectId"] for r in subj_rows if r["subjectId"]}
    for r in trip_rows:
        s = unpack(r["data"]).get("subject")
        if s:
            ids.add(s)
    return {"subjects": sorted(ids)}


def _trips_for(subject: Optional[str] = None):
    with get_conn() as conn:
        rows = conn.execute("SELECT data FROM trips ORDER BY created_at DESC").fetchall()
    trips = [unpack(r["data"]) for r in rows]
    if subject:
        trips = [t for t in trips if str(t.get("subject", "")).lower() == subject.lower()]
    return trips


def list_trips(subject: Optional[str] = None) -> dict:
    """Dispatch trips, optionally filtered to one subject."""
    return {"trips": _trips_for(subject)}


def get_subject(subject_id: str) -> dict:
    """Cross-module view of one subject: compliance + visit logs + trips + invoice.

    This is the platform's integration point -- protocol/compliance/dispatch all
    key off the same subject identifier.
    """
    with get_conn() as conn:
        srow = conn.execute("SELECT data FROM subjects WHERE subjectId = ?",
                            (subject_id,)).fetchone()
        log_rows = conn.execute("SELECT data FROM visit_logs ORDER BY rowid ASC").fetchall()
    compliance = unpack(srow["data"]) if srow else None
    visit_logs = [
        d for d in (unpack(r["data"]) for r in log_rows)
        if str(d.get("subjectId", "")).lower() == subject_id.lower()
    ]
    trips = _trips_for(subject_id)
    total_due = round(sum(float(t.get("amount", 0)) for t in trips), 2)
    total_miles = round(sum(float(t.get("miles", 0)) for t in trips), 1)
    return {
        "subject_id": subject_id,
        "compliance": compliance,
        "visit_logs": visit_logs,
        "trips": trips,
        "invoice": {
            "trip_count": len(trips),
            "total_miles": total_miles,
            "total_due": total_due,
        },
        "found": bool(compliance or visit_logs or trips),
    }


def get_dispatch_rates() -> dict:
    """The Serengeti dispatch pricing model (per-vehicle rates + fixed fees)."""
    return {
        "rates": RATES,
        "fees": FEES,
        "example": {
            "description": "Sedan, 10 miles, 5 min wait, one-way",
            "amount": calculate_cost(10, 5, "Sedan", False),
        },
    }


def parse_protocol(text: str) -> dict:
    """Parse protocol text into a visit schedule summary (visits + conflicts)."""
    from .graph import build_graph  # imported lazily; langgraph is heavy
    result = build_graph().invoke({"text": text})["result"]
    return {
        "visits": [
            {"name": v.name, "cycle_group": v.cycle_group,
             "activities": [p.name for p in v.procedures]}
            for v in result.visits
        ],
        "operational_row_count": len(result.flat_rows),
        "conflicts": [c.model_dump() for c in result.conflicts],
    }


# --- Learning tools --------------------------------------------------------
def remember(fact: str, subject: Optional[str] = None) -> dict:
    """Persist a durable fact/preference/correction for future conversations."""
    rec = chat_memory.save_learning(fact, subject)
    return {"saved": True, "id": rec["id"]}


def forget(memory_id: str) -> dict:
    """Delete a previously learned fact by id."""
    return {"forgotten": chat_memory.forget(memory_id)}


# --- Document knowledge-base tools (RAG) -----------------------------------
def search_documents(query: str, study_id: Optional[str] = None,
                     limit: int = 5) -> dict:
    """Search uploaded trial documents for passages relevant to a query."""
    hits = docstore.search(query, study_id=study_id, limit=limit)
    return {"query": query, "results": hits, "count": len(hits)}


def list_documents(study_id: Optional[str] = None) -> dict:
    """List the documents that have been uploaded to the knowledge base."""
    return {"documents": docstore.list_documents(study_id)}


# --- Tool registry ---------------------------------------------------------
TOOLS = [
    {
        "name": "list_subjects",
        "description": "List all known study subject IDs across compliance records and transport trips. Call this to discover who exists before answering about a specific subject.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_subject",
        "description": "Get the full cross-module picture for one subject: compliance record, visit logs, dispatch trips, and an invoice summary. Use this for any question about a specific subject's status.",
        "input_schema": {
            "type": "object",
            "properties": {"subject_id": {"type": "string", "description": "The subject/voucher ID, e.g. SUBJ-0012"}},
            "required": ["subject_id"],
        },
    },
    {
        "name": "list_trips",
        "description": "List transport (dispatch) trips, optionally filtered to one subject.",
        "input_schema": {
            "type": "object",
            "properties": {"subject": {"type": "string", "description": "Optional subject ID to filter by"}},
        },
    },
    {
        "name": "get_dispatch_rates",
        "description": "Get the transport pricing model: per-vehicle base/per-mile/wait rates and the fixed sanitation + scheduling fees.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "parse_protocol",
        "description": "Parse clinical-trial protocol text into a visit schedule: visits, per-visit activities, and conflicts between the Schedule of Activities table and the narrative.",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "The protocol text (including any SoA table)"}},
            "required": ["text"],
        },
    },
    {
        "name": "search_documents",
        "description": "Search the uploaded trial-document knowledge base (protocols, source-document templates, sponsor memos) for passages relevant to a question. Use this to ground answers in what the user has actually uploaded, or to find prior source-document examples before drafting a new one. Returns ranked excerpts with their source document title.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to look for, e.g. 'screening visit lab requirements'"},
                "study_id": {"type": "string", "description": "Optional study id to restrict the search to one study's documents"},
                "limit": {"type": "integer", "description": "Max passages to return (default 5)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_documents",
        "description": "List the documents currently in the knowledge base, optionally filtered to one study. Use this to see what source material is available before searching it.",
        "input_schema": {
            "type": "object",
            "properties": {"study_id": {"type": "string", "description": "Optional study id to filter by"}},
        },
    },
    {
        "name": "remember",
        "description": "Save a durable fact, preference, or correction the user gives you, so you recall it in future conversations. Use for things like 'always show times in 24h' or 'SUBJ-0012 is on the placebo arm'. Optionally scope to a subject.",
        "input_schema": {
            "type": "object",
            "properties": {
                "fact": {"type": "string", "description": "The thing to remember, stated concisely"},
                "subject": {"type": "string", "description": "Optional subject ID this fact is about"},
            },
            "required": ["fact"],
        },
    },
    {
        "name": "forget",
        "description": "Delete a previously learned fact by its memory id (shown in the 'Things you've learned' list) when it is wrong or outdated.",
        "input_schema": {
            "type": "object",
            "properties": {"memory_id": {"type": "string", "description": "The mem_... id of the learning to delete"}},
            "required": ["memory_id"],
        },
    },
]

_DISPATCH = {
    "list_subjects": list_subjects,
    "get_subject": get_subject,
    "list_trips": list_trips,
    "get_dispatch_rates": get_dispatch_rates,
    "parse_protocol": parse_protocol,
    "search_documents": search_documents,
    "list_documents": list_documents,
    "remember": remember,
    "forget": forget,
}


def dispatch_tool(name: str, tool_input: dict):
    """Execute a tool by name with the given input dict."""
    fn = _DISPATCH.get(name)
    if fn is None:
        return {"error": f"unknown tool: {name}"}
    try:
        return fn(**(tool_input or {}))
    except Exception as exc:  # surface tool errors back to the model
        return {"error": f"{type(exc).__name__}: {exc}"}
