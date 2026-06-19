from typing import TypedDict, Any

try:
    from langgraph.graph import StateGraph, END
    _LANGGRAPH_AVAILABLE = True
except ImportError:  # pragma: no cover – langgraph is optional
    _LANGGRAPH_AVAILABLE = False

from .soa_parser import parse_soa
from .narrative import parse_narrative
from .conflicts import detect_conflicts
from .cycle_engine import expand_cycles, infer_day
from .merge import merge_visits_by_name
from .ops_builder import build_operational_rows
from .schemas import ProtocolExtraction, FinalSchedule


class State(TypedDict, total=False):
    text: str
    soa: Any
    nar: Any
    extraction: Any
    result: Any


# ---------------------------------------------------------------------------
# Individual pipeline steps (each takes a State dict, returns a partial State)
# ---------------------------------------------------------------------------

def run_soa(state: State) -> dict:
    return {'soa': parse_soa(state['text'])}


def run_narrative(state: State) -> dict:
    return {'nar': parse_narrative(state['text'])}


def merge(state: State) -> dict:
    visits = []
    if state.get('soa'):
        visits.extend(state['soa'].visits)
    if state.get('nar'):
        visits.extend(state['nar'].visits)
    return {'extraction': ProtocolExtraction(visits=visits)}


def _sort_visits_chronologically(visits):
    """Return a new list sorted by inferred day, preserving original order for ties."""
    _FALLBACK = 50000  # Unknown visits sort near the end but before Follow-up (99999)

    def _sort_key(v):
        day = v.day if v.day is not None else infer_day(v.name)
        return day if day is not None else _FALLBACK

    return sorted(visits, key=_sort_key)


def finalize(state: State) -> dict:
    extraction = state['extraction']
    expanded_visits = expand_cycles(extraction.visits)

    # Populate .day on any visit that doesn't already have one
    for v in expanded_visits:
        if v.day is None:
            inferred = infer_day(v.name)
            if inferred is not None:
                v.day = inferred

    # Conflicts are detected on the full, source-tagged extraction so that we
    # can still see what each parser route (SoA vs narrative) reported.
    conflicts = detect_conflicts(ProtocolExtraction(visits=expanded_visits))

    # Display + operational rows use a single de-duplicated visit list so the
    # same activity isn't emitted once per source.
    merged_visits = merge_visits_by_name(expanded_visits)

    # Sort chronologically: Screening first, Follow-up last
    merged_visits = _sort_visits_chronologically(merged_visits)

    rows = build_operational_rows(merged_visits)
    schedule = FinalSchedule(visits=merged_visits, flat_rows=rows, conflicts=conflicts)
    return {'result': schedule}


# ---------------------------------------------------------------------------
# Fallback linear runner (used when langgraph is not installed)
# ---------------------------------------------------------------------------

class _LinearGraph:
    """Minimal drop-in replacement that runs the pipeline sequentially."""

    def invoke(self, state: dict) -> dict:
        state = dict(state)
        for step in (run_soa, run_narrative, merge, finalize):
            state.update(step(state))  # type: ignore[arg-type]
        return state


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------

def build_graph():
    """Return a compiled graph (langgraph) or a lightweight linear runner.

    Both expose exactly the same interface:
        build_graph().invoke({'text': text})['result']  -> FinalSchedule
    """
    if _LANGGRAPH_AVAILABLE:
        g = StateGraph(State)
        g.add_node('soa', run_soa)
        g.add_node('nar', run_narrative)
        g.add_node('merge', merge)
        g.add_node('finalize', finalize)
        g.set_entry_point('soa')
        g.add_edge('soa', 'nar')
        g.add_edge('nar', 'merge')
        g.add_edge('merge', 'finalize')
        g.add_edge('finalize', END)
        return g.compile()
    # langgraph not available – use simple linear runner
    return _LinearGraph()
