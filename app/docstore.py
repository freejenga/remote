"""Document knowledge base + retrieval (the platform's vector-search layer).

This is the retrieval-augmented-generation (RAG) backend that lets the AI answer
questions grounded in trial documents the user has uploaded -- protocols, source
-document templates, sponsor memos -- rather than only the structured records in
the other tables.

Flow (mirrors the architecture's hard de-identification boundary):

    raw text --> deid.scrub_text --> chunk --> crypto.encrypt --> doc_chunks

Nothing with a direct identifier is ever embedded or indexed: text is scrubbed
*before* it is chunked and stored, so the searchable corpus is de-identified at
rest, exactly like ``chat_memory``.

Retrieval is intentionally dependency-free: ``search`` ranks chunks with TF-IDF
cosine similarity computed in pure Python, which is accurate enough at this scale
and keeps the install lightweight (no faiss/chroma/embedding service). The
``_rank`` function is the single seam where a real embedding/vector backend
(AgentDB, pgvector, ReasoningBank) can later drop in without touching the public
``add_document`` / ``search`` / ``list_documents`` interface or ``chat_tools``.
"""
import math
import re
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import List, Optional

from .store import get_conn
from . import deid, crypto

# Chunking: pack text into windows of ~CHUNK_WORDS words with CHUNK_OVERLAP words
# of overlap so a fact spanning a boundary is still recoverable in one chunk.
CHUNK_WORDS = 200
CHUNK_OVERLAP = 40

_WORD = re.compile(r"[a-z0-9]+")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tokens(text: str) -> List[str]:
    return _WORD.findall((text or "").lower())


def _chunk_text(text: str) -> List[str]:
    """Split text into overlapping word windows. Returns a list of chunk strings."""
    words = (text or "").split()
    if not words:
        return []
    step = max(1, CHUNK_WORDS - CHUNK_OVERLAP)
    chunks = []
    for start in range(0, len(words), step):
        window = words[start:start + CHUNK_WORDS]
        if window:
            chunks.append(" ".join(window))
        if start + CHUNK_WORDS >= len(words):
            break
    return chunks


# --- Public write API ------------------------------------------------------
def add_document(title: str, text: str, *, study_id: Optional[str] = None,
                 source_type: Optional[str] = None) -> dict:
    """Ingest one document into the knowledge base.

    Text is de-identified, chunked, and stored (encrypted at rest). Returns
    ``{id, title, chunk_count}``. Raises ValueError if there's no usable text.
    """
    title = (title or "").strip() or "Untitled document"
    scrubbed = deid.scrub_text(text or "")
    chunks = _chunk_text(scrubbed)
    if not chunks:
        raise ValueError("document has no extractable text")

    doc_id = f"doc_{uuid.uuid4().hex[:12]}"
    now = _now()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO documents (id, study_id, title, source_type, chunk_count, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (doc_id, study_id, title, source_type, len(chunks), now),
        )
        conn.executemany(
            "INSERT INTO doc_chunks (id, doc_id, study_id, ordinal, content, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (f"chk_{uuid.uuid4().hex[:12]}", doc_id, study_id, i,
                 crypto.encrypt(chunk), now)
                for i, chunk in enumerate(chunks)
            ],
        )
    return {"id": doc_id, "title": title, "chunk_count": len(chunks)}


def delete_document(doc_id: str) -> bool:
    """Remove a document and all its chunks. Returns True if the doc existed."""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
        conn.execute("DELETE FROM doc_chunks WHERE doc_id = ?", (doc_id,))
        return cur.rowcount > 0


# --- Public read API -------------------------------------------------------
def list_documents(study_id: Optional[str] = None) -> List[dict]:
    """List uploaded documents (newest first), optionally scoped to a study."""
    with get_conn() as conn:
        if study_id:
            rows = conn.execute(
                "SELECT id, study_id, title, source_type, chunk_count, created_at "
                "FROM documents WHERE study_id = ? ORDER BY created_at DESC",
                (study_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, study_id, title, source_type, chunk_count, created_at "
                "FROM documents ORDER BY created_at DESC"
            ).fetchall()
    return [dict(r) for r in rows]


def _load_chunks(study_id: Optional[str] = None) -> List[dict]:
    """Load (and decrypt) all chunks in scope, joined to their document title."""
    with get_conn() as conn:
        if study_id:
            rows = conn.execute(
                "SELECT c.id, c.doc_id, c.ordinal, c.content, d.title "
                "FROM doc_chunks c JOIN documents d ON d.id = c.doc_id "
                "WHERE c.study_id = ?",
                (study_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT c.id, c.doc_id, c.ordinal, c.content, d.title "
                "FROM doc_chunks c JOIN documents d ON d.id = c.doc_id"
            ).fetchall()
    return [
        {"id": r["id"], "doc_id": r["doc_id"], "ordinal": r["ordinal"],
         "title": r["title"], "content": crypto.decrypt(r["content"])}
        for r in rows
    ]


def _rank(query: str, chunks: List[dict], limit: int) -> List[dict]:
    """Rank chunks against the query by TF-IDF cosine similarity.

    Pure-Python, no external vector store. This is the seam: swap this body for
    an embedding-based nearest-neighbour search and the public API is unchanged.
    """
    q_tokens = _tokens(query)
    if not q_tokens or not chunks:
        return []

    # Document frequency across the in-scope corpus.
    n_docs = len(chunks)
    df: Counter = Counter()
    chunk_tokens = []
    for ch in chunks:
        toks = _tokens(ch["content"])
        chunk_tokens.append(toks)
        for t in set(toks):
            df[t] += 1

    def idf(term: str) -> float:
        # Smoothed idf; terms absent from the corpus contribute nothing.
        return math.log((n_docs + 1) / (df.get(term, 0) + 1)) + 1.0

    # Query TF-IDF vector.
    q_tf = Counter(q_tokens)
    q_vec = {t: tf * idf(t) for t, tf in q_tf.items()}
    q_norm = math.sqrt(sum(w * w for w in q_vec.values())) or 1.0

    scored = []
    for ch, toks in zip(chunks, chunk_tokens):
        if not toks:
            continue
        tf = Counter(toks)
        # Only terms shared with the query contribute to the dot product.
        dot = sum(q_vec[t] * (tf[t] * idf(t)) for t in q_vec if t in tf)
        if dot <= 0:
            continue
        d_norm = math.sqrt(sum((c * idf(t)) ** 2 for t, c in tf.items())) or 1.0
        score = dot / (q_norm * d_norm)
        scored.append((score, ch))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [
        {"doc_id": ch["doc_id"], "title": ch["title"], "ordinal": ch["ordinal"],
         "score": round(score, 4), "content": ch["content"]}
        for score, ch in scored[:limit]
    ]


def search(query: str, *, study_id: Optional[str] = None, limit: int = 5) -> List[dict]:
    """Return the most relevant document chunks for a query.

    Each hit is ``{doc_id, title, ordinal, score, content}``. Used by the chat's
    ``search_documents`` tool to ground answers in uploaded documents.
    """
    return _rank(query or "", _load_chunks(study_id), limit)
