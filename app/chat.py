"""AI chat for the Clinical Research Platform.

`run_chat()` is a thin manual agentic loop over the Anthropic Messages API:
Claude answers questions about subjects by calling the read-only tools in
``chat_tools`` and learns durable facts via the ``remember`` / ``forget`` tools,
with prior learnings recalled into the system prompt each turn. It can also be
given a ``protocol_context`` so it can answer about a protocol the user has
loaded in the protocol-agent page.

The FastAPI ``router`` exposes this at POST /chat/ (non-streaming) and
POST /chat/stream (Server-Sent Events streaming). The Streamlit protocol page
imports ``run_chat`` directly (in-process, no HTTP needed).
"""
import json
import os
from typing import Generator, List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .chat_tools import TOOLS, dispatch_tool
from . import chat_memory, deid, audit

router = APIRouter(prefix="/chat", tags=["chat"])

MODEL = os.environ.get("CHAT_MODEL", "claude-haiku-4-5")
MAX_TOOL_ITERATIONS = 6

# Only these models accept adaptive thinking; Haiku 4.5 / Sonnet 4.5 / older 400
# if `thinking` is sent. Keep this in sync if CHAT_MODEL points elsewhere.
_ADAPTIVE_THINKING_PREFIXES = (
    "claude-opus-4-5", "claude-opus-4-6", "claude-opus-4-7", "claude-opus-4-8",
    "claude-sonnet-4-6",
)


def _supports_adaptive_thinking(model: str) -> bool:
    return model.startswith(_ADAPTIVE_THINKING_PREFIXES)


_BASE_SYSTEM = """You are the assistant for the Clinical Research Platform, a system that unifies three modules around a shared study-subject identifier:
- Protocol: clinical-trial protocols parsed into visit schedules (visits, activities, SoA-vs-narrative conflicts).
- Compliance: study subjects, their visit logs, and the study date.
- Dispatch: non-emergency medical transport trips with a pricing engine and invoices.

A subject's protocol visits, compliance status, and transport trips all line up by the same subject ID, so to answer questions about a subject, call get_subject for the full picture. Use the other tools as needed; don't guess data you can look up.

Learning: when the user states a durable preference, correction, or fact (e.g. a default format, or something true about a subject), call the remember tool so you recall it later. If a remembered fact is wrong, use forget.

This is demonstration software, not validated GxP software; don't present outputs as clinically authoritative."""


class ChatNotConfigured(RuntimeError):
    """Raised when ANTHROPIC_API_KEY (or the anthropic package) is missing."""


class ChatProviderError(RuntimeError):
    """Raised when the Anthropic API call fails."""


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: List[ChatMessage]
    subject: Optional[str] = None
    protocol_context: Optional[str] = None


def _build_system(subject: Optional[str], last_user: Optional[str],
                  protocol_context: Optional[str]) -> str:
    learnings = chat_memory.recall(subject=subject, query=last_user, limit=20)
    parts = [_BASE_SYSTEM]
    if protocol_context:
        parts.append(
            "\nThe user currently has this protocol loaded in the protocol agent. "
            "Answer questions about it directly from this parsed summary "
            "(you do not need to call parse_protocol for it):\n"
            + deid.scrub_text(protocol_context))
    if subject:
        parts.append(f"\nThe user is currently focused on subject: {subject}. "
                     f"Call get_subject('{subject}') if their question is about it.")
    if learnings:
        lines = "\n".join(
            f"- [{m['id']}]" + (f" (subject {m['subject']})" if m['subject'] else "")
            + f" {m['content']}"
            for m in learnings
        )
        parts.append("\nThings you've learned (recall and apply these):\n" + lines)
    return "\n".join(parts)


def _to_text(result) -> str:
    if isinstance(result, str):
        return result
    return json.dumps(result, default=str)


# ---------------------------------------------------------------------------
# Provenance / answer-source summarisation
# ---------------------------------------------------------------------------

def _summarize_tool_call(name: str, tool_input: dict) -> str:
    """Return a short human-readable summary of one tool call for provenance.

    This function is pure (no I/O) so it can be unit-tested without a live API.
    """
    if name == "get_subject":
        sid = tool_input.get("subject_id", "?")
        return f"get_subject({sid})"
    if name == "list_subjects":
        return "list_subjects()"
    if name == "list_trips":
        subj = tool_input.get("subject")
        return f"list_trips({subj})" if subj else "list_trips()"
    if name == "get_dispatch_rates":
        return "get_dispatch_rates()"
    if name == "parse_protocol":
        text = tool_input.get("text", "")
        preview = text[:40].replace("\n", " ").strip()
        if len(text) > 40:
            preview += "..."
        return f"parse_protocol(\"{preview}\")"
    if name == "search_documents":
        q = tool_input.get("query", "")[:40]
        return f"search_documents(\"{q}\")"
    if name == "list_documents":
        sid = tool_input.get("study_id")
        return f"list_documents({sid})" if sid else "list_documents()"
    if name == "remember":
        fact = tool_input.get("fact", "")[:50]
        return f"remember(\"{fact}\")"
    if name == "forget":
        mid = tool_input.get("memory_id", "?")
        return f"forget({mid})"
    # Fallback for any future tools
    keys = list(tool_input.keys())[:2]
    preview = ", ".join(f"{k}={tool_input[k]!r}" for k in keys) if keys else ""
    return f"{name}({preview})"


def _enrich_tool_calls(raw_tool_calls: list) -> tuple:
    """Enrich tool_calls with a 'summary' field and return (enriched, sources).

    ``sources`` is a deduplicated list of human summaries for the UI provenance
    line.  ``raw_tool_calls`` is mutated in-place (adds 'summary' key to each
    entry) and also returned for convenience.
    """
    seen: set = set()
    sources: list = []
    for tc in raw_tool_calls:
        summary = _summarize_tool_call(tc["name"], tc.get("input") or {})
        tc["summary"] = summary
        if summary not in seen:
            seen.add(summary)
            sources.append(summary)
    return raw_tool_calls, sources


# ---------------------------------------------------------------------------
# Shared pre-flight: key check + client construction
# ---------------------------------------------------------------------------

def _get_client():
    """Return (anthropic_client, model_str).  Raises ChatNotConfigured if unready."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ChatNotConfigured(
            "AI chat not configured: set ANTHROPIC_API_KEY in the environment.")
    try:
        import anthropic
    except ImportError as exc:
        raise ChatNotConfigured(
            "AI chat unavailable: the 'anthropic' package is not installed.") from exc
    return anthropic.Anthropic(api_key=api_key), MODEL


# ---------------------------------------------------------------------------
# Non-streaming path (public API, keep signature stable)
# ---------------------------------------------------------------------------

def run_chat(messages: List[dict], subject: Optional[str] = None,
             protocol_context: Optional[str] = None) -> dict:
    """Run one chat turn (with its internal tool loop). Reusable by API + UI.

    ``messages`` is a list of {role, content} dicts (the conversation so far).
    Returns {reply, tool_calls, sources, model}. Raises ChatNotConfigured /
    ChatProviderError.
    """
    client, model = _get_client()
    import anthropic  # already imported inside _get_client; safe to import again

    last_user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), None)
    system = _build_system(subject, last_user, protocol_context)
    convo = [{"role": m["role"], "content": m["content"]} for m in messages]
    if last_user:
        chat_memory.save_turn("user", last_user, subject)

    create_kwargs = dict(model=model, max_tokens=16000, system=system, tools=TOOLS)
    if _supports_adaptive_thinking(model):
        create_kwargs["thinking"] = {"type": "adaptive"}

    tool_calls: list = []
    try:
        for _ in range(MAX_TOOL_ITERATIONS):
            response = client.messages.create(messages=convo, **create_kwargs)
            if response.stop_reason != "tool_use":
                break
            convo.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = dispatch_tool(block.name, block.input)
                    tool_calls.append({"name": block.name, "input": block.input})
                    # De-identify tool output before it goes back to the model.
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": _to_text(deid.redact_obj(result)),
                    })
            convo.append({"role": "user", "content": tool_results})
    except anthropic.APIError as exc:
        raise ChatProviderError(f"AI provider error: {exc}") from exc

    reply = "".join(b.text for b in response.content if b.type == "text").strip()
    if reply:
        chat_memory.save_turn("assistant", reply, subject)

    # Enrich tool_calls with provenance summaries.
    tool_calls, sources = _enrich_tool_calls(tool_calls)

    # Audit the query (codes + tools only, never the conversation content).
    audit.record("chat.query",
                 {"subject": subject, "tools": [t["name"] for t in tool_calls]})
    return {"reply": reply, "tool_calls": tool_calls, "sources": sources, "model": model}


# ---------------------------------------------------------------------------
# Streaming path
# ---------------------------------------------------------------------------

def run_chat_stream(messages: List[dict], subject: Optional[str] = None,
                    protocol_context: Optional[str] = None) -> Generator[str, None, None]:
    """Run one chat turn with streaming text output.

    Yields Server-Sent Event strings:
      - ``data: {"token": "..."}\\n\\n``  for each text delta
      - ``data: {"done": true, "tool_calls": [...], "sources": [...], "model": "..."}\\n\\n``
        as the final event

    Raises ChatNotConfigured / ChatProviderError (before the first yield) if the
    API key is missing or the provider call fails.
    """
    client, model = _get_client()
    import anthropic  # already imported inside _get_client; safe to import again

    last_user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), None)
    system = _build_system(subject, last_user, protocol_context)
    convo = [{"role": m["role"], "content": m["content"]} for m in messages]
    if last_user:
        chat_memory.save_turn("user", last_user, subject)

    create_kwargs = dict(model=model, max_tokens=16000, system=system, tools=TOOLS)
    if _supports_adaptive_thinking(model):
        create_kwargs["thinking"] = {"type": "adaptive"}

    tool_calls: list = []
    full_reply_parts: list = []

    try:
        for iteration in range(MAX_TOOL_ITERATIONS):
            # On the last iteration before the final answer, stream text deltas.
            # On intermediate tool-use iterations, we use the non-streaming path
            # since the output will be consumed internally.
            with client.messages.stream(messages=convo, **create_kwargs) as stream:
                # Collect the full message (needed for tool use and final metadata)
                response = stream.get_final_message()

                if response.stop_reason == "tool_use":
                    # Tool use turn: process tools, don't yield tokens yet
                    convo.append({"role": "assistant", "content": response.content})
                    tool_results = []
                    for block in response.content:
                        if block.type == "tool_use":
                            result = dispatch_tool(block.name, block.input)
                            tool_calls.append({"name": block.name, "input": block.input})
                            # De-identify tool output before it goes back to the model.
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": _to_text(deid.redact_obj(result)),
                            })
                    convo.append({"role": "user", "content": tool_results})
                else:
                    # Final answer turn: stream text deltas to the caller
                    for text_delta in stream.text_stream:
                        full_reply_parts.append(text_delta)
                        payload = json.dumps({"token": text_delta}, ensure_ascii=False)
                        yield f"data: {payload}\n\n"
                    break

    except anthropic.APIError as exc:
        raise ChatProviderError(f"AI provider error: {exc}") from exc

    reply = "".join(full_reply_parts).strip()
    if not reply:
        # If the final response had no text blocks streamed (e.g. it was all
        # tool_use with no follow-up), fall back to extracting text from response
        reply = "".join(b.text for b in response.content if b.type == "text").strip()

    if reply:
        chat_memory.save_turn("assistant", reply, subject)

    # Enrich tool_calls with provenance summaries.
    tool_calls, sources = _enrich_tool_calls(tool_calls)

    # Audit
    audit.record("chat.query",
                 {"subject": subject, "tools": [t["name"] for t in tool_calls]})

    # Final SSE event with metadata
    final_payload = json.dumps(
        {"done": True, "tool_calls": tool_calls, "sources": sources, "model": model},
        ensure_ascii=False, default=str
    )
    yield f"data: {final_payload}\n\n"


# ---------------------------------------------------------------------------
# FastAPI endpoints
# ---------------------------------------------------------------------------

@router.post("/")
def chat(req: ChatRequest):
    try:
        return run_chat([m.model_dump() for m in req.messages], req.subject,
                        req.protocol_context)
    except ChatNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except ChatProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/stream")
def chat_stream(req: ChatRequest):
    """Streaming chat endpoint returning Server-Sent Events.

    Each event is ``data: {token}\\n\\n`` for text deltas, followed by a final
    ``data: {done, tool_calls, sources, model}\\n\\n`` event.

    Returns HTTP 503 immediately (before streaming starts) when the API key is
    not configured, so the UI can fall back to the non-streaming endpoint.
    """
    # Validate configuration eagerly so we can return a proper HTTP error code
    # instead of starting a stream that immediately errors.
    try:
        _get_client()
    except ChatNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    def _generate():
        try:
            yield from run_chat_stream(
                [m.model_dump() for m in req.messages],
                req.subject,
                req.protocol_context,
            )
        except ChatNotConfigured as exc:
            err = json.dumps({"error": str(exc)})
            yield f"data: {err}\n\n"
        except ChatProviderError as exc:
            err = json.dumps({"error": str(exc)})
            yield f"data: {err}\n\n"

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
