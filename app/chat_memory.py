"""Persistent learning + conversation memory for the AI chat.

Backed by the shared SQLite store (``chat_memory`` table) so everything the chat
learns survives restarts and is shared across sessions. Deliberately
dependency-free: ``recall`` ranks learnings with a simple keyword-overlap score,
which is plenty for this scale. The ``recall`` / ``save_learning`` interface is
the seam where a semantic vector backend (AgentDB / ReasoningBank) could later
drop in without touching ``chat.py``.
"""
import os
import re
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from .store import get_conn
from . import deid, crypto

_WORD = re.compile(r"[a-z0-9]+")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tokens(text: str) -> set:
    return set(_WORD.findall((text or "").lower()))


def save_learning(content: str, subject: Optional[str] = None) -> dict:
    """Store a durable fact/preference/correction the user taught the chat.

    Content is scrubbed of identifier patterns before persistence so the memory
    doesn't quietly accumulate PHI.
    """
    content = deid.scrub_text((content or "").strip())
    if not content:
        raise ValueError("content is required")
    record = {
        "id": f"mem_{uuid.uuid4().hex[:12]}",
        "subject": subject,
        "kind": "learning",
        "content": content,
        "created_at": _now(),
    }
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO chat_memory (id, subject, kind, content, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (record["id"], record["subject"], record["kind"],
             crypto.encrypt(record["content"]), record["created_at"]),
        )
    return record


def save_turn(role: str, content: str, subject: Optional[str] = None) -> None:
    """Append one conversation turn to the history log.

    Off by default for memory hygiene -- raw conversations can contain PHI, so we
    don't persist them unless ``CHAT_LOG_CONVERSATIONS=1`` is set, and even then
    the content is scrubbed of identifier patterns first.
    """
    if not content or os.environ.get("CHAT_LOG_CONVERSATIONS", "0") != "1":
        return
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO chat_memory (id, subject, kind, content, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (f"turn_{uuid.uuid4().hex[:12]}", subject, f"conversation:{role}",
             crypto.encrypt(deid.scrub_text(content)), _now()),
        )


def forget(memory_id: str) -> bool:
    """Delete a learning by id so a wrong/outdated fact doesn't persist."""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM chat_memory WHERE id = ? AND kind = 'learning'",
                           (memory_id,))
        return cur.rowcount > 0


def recall(subject: Optional[str] = None, query: Optional[str] = None,
           limit: int = 20) -> List[dict]:
    """Return the most relevant learnings for this turn.

    Ranking: subject-matched learnings first, then keyword overlap with the
    query, then recency. Conversation turns are not recalled (they're history,
    not knowledge).
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, subject, content, created_at FROM chat_memory "
            "WHERE kind = 'learning' ORDER BY created_at DESC"
        ).fetchall()

    # Decrypt content up front so ranking and output use plaintext.
    items = [
        {"id": r["id"], "subject": r["subject"],
         "content": crypto.decrypt(r["content"]), "created_at": r["created_at"]}
        for r in rows
    ]
    q_tokens = _tokens(query) if query else set()

    def score(item) -> tuple:
        subj_match = 1 if (subject and item["subject"] == subject) else 0
        overlap = len(q_tokens & _tokens(item["content"])) if q_tokens else 0
        return (subj_match, overlap, item["created_at"])

    return sorted(items, key=score, reverse=True)[:limit]
