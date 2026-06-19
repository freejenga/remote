"""Document knowledge base + retrieval (the platform's vector-search layer).

This is the retrieval-augmented-generation (RAG) backend that lets the AI answer
questions grounded in trial documents the user has uploaded -- protocols, source
-document templates, sponsor memos -- rather than only the structured records in
the other tables.

Flow (mirrors the architecture's hard de-identification boundary):

    raw text --> deid.scrub_text --> segment (section-aware) --> crypto.encrypt
              --> doc_chunks  (+ optional local embedding --> doc_vectors)

Nothing with a direct identifier is ever embedded or indexed: text is scrubbed
*before* it is chunked and stored, so the searchable corpus is de-identified at
rest, exactly like ``chat_memory``.

Two retrieval refinements over the original TF-IDF layer:

  * **Section-aware chunking** -- ``_segment_document`` splits on protocol
    headings (Inclusion Criteria, Exclusion Criteria, Schedule of Events, ...)
    and keeps tagged tables (``[[TABLE]]..[[/TABLE]]`` from ingestion) intact as
    single chunks, so a Schedule-of-Events matrix or a criteria list is never
    cut across a chunk boundary. Each chunk carries a ``section`` label.
  * **Hybrid retrieval** -- when local embeddings are enabled (see
    ``app.embeddings``), ``search`` fuses lexical TF-IDF ranking with semantic
    nearest-neighbour ranking via reciprocal-rank fusion. When embeddings are
    off/unavailable it transparently falls back to pure TF-IDF -- the original,
    dependency-free behaviour.

``_lexical_ranked`` / ``_rank`` remain the seam: the public
``add_document`` / ``search`` / ``list_documents`` interface and ``chat_tools``
are unchanged regardless of which retrieval path runs.
"""
import math
import re
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from .store import get_conn
from . import deid, crypto, embeddings, ingestion

# Chunking: pack narrative text into windows of ~CHUNK_WORDS words with
# CHUNK_OVERLAP words of overlap so a fact spanning a boundary is still
# recoverable in one chunk.
CHUNK_WORDS = 200
CHUNK_OVERLAP = 40

_WORD = re.compile(r"[a-z0-9]+")

# Headings that map to a canonical section label. Order matters: the specific
# inclusion/exclusion patterns must be tried before the generic "eligibility".
_SECTION_PATTERNS = [
    (re.compile(r"\binclusion\s+criteria\b", re.I), "Inclusion Criteria"),
    (re.compile(r"\bexclusion\s+criteria\b", re.I), "Exclusion Criteria"),
    (re.compile(r"\beligibility(\s+criteria)?\b", re.I), "Eligibility Criteria"),
    (re.compile(r"\bschedule\s+of\s+(events|activities|assessments)\b", re.I),
     "Schedule of Events"),
    (re.compile(r"\bstudy\s+(design|schema)\b", re.I), "Study Design"),
    (re.compile(r"\bobjectives?\b", re.I), "Objectives"),
    (re.compile(r"\bendpoints?\b", re.I), "Endpoints"),
    (re.compile(r"\b(dosing|study\s+drug|treatment)\b.*"
                r"\b(regimen|administration|schedule)\b", re.I),
     "Dosing & Administration"),
    (re.compile(r"\badverse\s+events?\b|\bsafety\s+(reporting|assessment)", re.I),
     "Safety / Adverse Events"),
    (re.compile(r"\bstatistical\b", re.I), "Statistical Considerations"),
]

_NUMBERED_HEADING = re.compile(r"^\d+(\.\d+)*\.?\s+\S")
_LEADING_NUMBER = re.compile(r"^\d+(\.\d+)*\.?\s+")
_TABLE_BLOCK = re.compile(
    re.escape(ingestion.TABLE_OPEN) + r"\s*(.*?)\s*" + re.escape(ingestion.TABLE_CLOSE),
    re.S,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tokens(text: str) -> List[str]:
    return _WORD.findall((text or "").lower())


def _chunk_text(text: str) -> List[str]:
    """Split text into overlapping word windows. Returns a list of chunk strings.

    The low-level windowing primitive, unchanged: section-aware segmentation
    (``_segment_document``) calls this for each narrative section's body.
    """
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


# --- Section-aware segmentation --------------------------------------------
def _looks_like_heading(s: str) -> bool:
    """Conservative generic-heading test for lines without a known keyword."""
    if s.endswith(".") or s.endswith(","):
        return False
    words = s.split()
    if not (1 <= len(words) <= 9):
        return False
    if _NUMBERED_HEADING.match(s):
        return True
    letters = [c for c in s if c.isalpha()]
    if letters and s == s.upper() and len(letters) >= 3:
        return True
    return False


def _clean_heading(s: str) -> str:
    cleaned = _LEADING_NUMBER.sub("", s).strip()
    return cleaned.title() if cleaned.isupper() else cleaned


def _detect_section(line: str) -> Optional[str]:
    """Return a section label if ``line`` reads like a heading, else None."""
    s = line.strip()
    if not s or len(s) > 90:
        return None
    for rx, label in _SECTION_PATTERNS:
        if rx.search(s) and len(s) <= 70:
            return label
    if _looks_like_heading(s):
        return _clean_heading(s)
    return None


def _guess_table_section(table_body: str) -> Optional[str]:
    head = table_body[:200].lower()
    if "screen" in head or "cycle" in head or "visit" in head or "day" in head:
        return "Schedule of Events"
    return "Table"


def _segment_narrative(text: str, current_section: Optional[str]
                       ) -> Tuple[Optional[str], List[dict]]:
    """Chunk a narrative span, tracking the active section across headings."""
    out: List[dict] = []
    if not text.strip():
        return current_section, out
    section = current_section
    buf: List[str] = []

    def flush():
        nonlocal buf
        body = "\n".join(buf).strip()
        buf = []
        if body:
            for window in _chunk_text(body):
                out.append({"content": window, "section": section})

    for line in text.split("\n"):
        hdr = _detect_section(line)
        if hdr:
            flush()
            section = hdr
        buf.append(line)
    flush()
    return section, out


def _segment_document(text: str) -> List[dict]:
    """Split a document into ``{content, section}`` chunks.

    Tagged tables become single atomic chunks; narrative spans are chunked
    section-by-section. Falls back to plain word-windowing (section ``None``)
    for documents without recognizable headings or tables.
    """
    segments: List[dict] = []
    section: Optional[str] = None
    pos = 0
    for m in _TABLE_BLOCK.finditer(text):
        section, narr = _segment_narrative(text[pos:m.start()], section)
        segments.extend(narr)
        body = m.group(1).strip()
        if body:
            segments.append({"content": body,
                             "section": section or _guess_table_section(body)})
        pos = m.end()
    section, narr = _segment_narrative(text[pos:], section)
    segments.extend(narr)
    return [s for s in segments if s["content"].strip()]


# --- Public write API ------------------------------------------------------
def add_document(title: str, text: str, *, study_id: Optional[str] = None,
                 source_type: Optional[str] = None) -> dict:
    """Ingest one document into the knowledge base.

    Text is de-identified, segmented (section-aware), and stored (encrypted at
    rest). When local embeddings are enabled, a vector per chunk is also stored
    for semantic search. Returns ``{id, title, chunk_count}``. Raises ValueError
    if there's no usable text.
    """
    title = (title or "").strip() or "Untitled document"
    scrubbed = deid.scrub_text(text or "")
    segments = _segment_document(scrubbed)
    if not segments:
        raise ValueError("document has no extractable text")

    doc_id = f"doc_{uuid.uuid4().hex[:12]}"
    now = _now()
    chunk_ids = [f"chk_{uuid.uuid4().hex[:12]}" for _ in segments]

    # Optional semantic vectors (over the already de-identified chunk text).
    vectors = None
    if embeddings.available():
        vectors = embeddings.embed([s["content"] for s in segments])

    with get_conn() as conn:
        conn.execute(
            "INSERT INTO documents (id, study_id, title, source_type, chunk_count, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (doc_id, study_id, title, source_type, len(segments), now),
        )
        conn.executemany(
            "INSERT INTO doc_chunks (id, doc_id, study_id, ordinal, content, section, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (cid, doc_id, study_id, i, crypto.encrypt(seg["content"]),
                 seg["section"], now)
                for i, (cid, seg) in enumerate(zip(chunk_ids, segments))
            ],
        )
        if vectors:
            conn.executemany(
                "INSERT INTO doc_vectors "
                "(chunk_id, doc_id, study_id, dim, model, vector, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (cid, doc_id, study_id, len(vec), embeddings._MODEL_NAME,
                     embeddings.to_blob(vec), now)
                    for cid, vec in zip(chunk_ids, vectors)
                ],
            )
    return {"id": doc_id, "title": title, "chunk_count": len(segments)}


def delete_document(doc_id: str) -> bool:
    """Remove a document and all its chunks/vectors. True if the doc existed."""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
        conn.execute("DELETE FROM doc_chunks WHERE doc_id = ?", (doc_id,))
        conn.execute("DELETE FROM doc_vectors WHERE doc_id = ?", (doc_id,))
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
                "SELECT c.id, c.doc_id, c.ordinal, c.section, c.content, d.title "
                "FROM doc_chunks c JOIN documents d ON d.id = c.doc_id "
                "WHERE c.study_id = ?",
                (study_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT c.id, c.doc_id, c.ordinal, c.section, c.content, d.title "
                "FROM doc_chunks c JOIN documents d ON d.id = c.doc_id"
            ).fetchall()
    return [
        {"id": r["id"], "doc_id": r["doc_id"], "ordinal": r["ordinal"],
         "section": r["section"], "title": r["title"],
         "content": crypto.decrypt(r["content"])}
        for r in rows
    ]


def _load_vectors(study_id: Optional[str] = None) -> dict:
    """Load stored embedding vectors in scope, keyed by chunk id."""
    with get_conn() as conn:
        if study_id:
            rows = conn.execute(
                "SELECT chunk_id, vector FROM doc_vectors WHERE study_id = ?",
                (study_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT chunk_id, vector FROM doc_vectors"
            ).fetchall()
    return {r["chunk_id"]: embeddings.from_blob(r["vector"])
            for r in rows if r["vector"]}


def _format_hit(chunk: dict, score: float) -> dict:
    return {"doc_id": chunk["doc_id"], "title": chunk["title"],
            "ordinal": chunk["ordinal"], "section": chunk.get("section"),
            "score": round(score, 4), "content": chunk["content"]}


def _lexical_ranked(query: str, chunks: List[dict]) -> List[Tuple[dict, float]]:
    """Rank chunks by TF-IDF cosine similarity. Returns (chunk, score) desc.

    Pure-Python, no external vector store. This is the lexical seam shared by the
    TF-IDF-only path (``_rank``) and the hybrid path (``_rank_hybrid``).
    """
    q_tokens = _tokens(query)
    if not q_tokens or not chunks:
        return []

    n_docs = len(chunks)
    df: Counter = Counter()
    chunk_tokens = []
    for ch in chunks:
        toks = _tokens(ch["content"])
        chunk_tokens.append(toks)
        for t in set(toks):
            df[t] += 1

    def idf(term: str) -> float:
        return math.log((n_docs + 1) / (df.get(term, 0) + 1)) + 1.0

    q_tf = Counter(q_tokens)
    q_vec = {t: tf * idf(t) for t, tf in q_tf.items()}
    q_norm = math.sqrt(sum(w * w for w in q_vec.values())) or 1.0

    scored: List[Tuple[dict, float]] = []
    for ch, toks in zip(chunks, chunk_tokens):
        if not toks:
            continue
        tf = Counter(toks)
        dot = sum(q_vec[t] * (tf[t] * idf(t)) for t in q_vec if t in tf)
        if dot <= 0:
            continue
        d_norm = math.sqrt(sum((c * idf(t)) ** 2 for t, c in tf.items())) or 1.0
        scored.append((ch, dot / (q_norm * d_norm)))

    scored.sort(key=lambda x: x[1], reverse=True)
    return scored


def _rank(query: str, chunks: List[dict], limit: int) -> List[dict]:
    """TF-IDF-only ranking (the dependency-free default path)."""
    return [_format_hit(ch, score)
            for ch, score in _lexical_ranked(query, chunks)[:limit]]


def _rank_hybrid(query: str, chunks: List[dict], study_id: Optional[str],
                 limit: int) -> List[dict]:
    """Fuse lexical TF-IDF and semantic embedding rankings via RRF.

    Reciprocal-rank fusion is robust to the two rankers' incomparable score
    scales. If the query can't be embedded, the semantic ranking is empty and
    this degrades to the lexical order.
    """
    if not chunks:
        return []
    K = 60
    lexical = _lexical_ranked(query, chunks)

    semantic: List[Tuple[dict, float]] = []
    qv = embeddings.embed_one(query)
    if qv:
        vectors = _load_vectors(study_id)
        for ch in chunks:
            v = vectors.get(ch["id"])
            if v:
                semantic.append((ch, embeddings.cosine(qv, v)))
        semantic.sort(key=lambda x: x[1], reverse=True)

    fused: dict = {}
    ref: dict = {}
    for ranking in (lexical, semantic):
        for rank, (ch, _s) in enumerate(ranking):
            cid = ch["id"]
            fused[cid] = fused.get(cid, 0.0) + 1.0 / (K + rank + 1)
            ref[cid] = ch

    if not fused:
        return []
    ordered = sorted(fused.items(), key=lambda x: x[1], reverse=True)[:limit]
    return [_format_hit(ref[cid], score) for cid, score in ordered]


def search(query: str, *, study_id: Optional[str] = None, limit: int = 5) -> List[dict]:
    """Return the most relevant document chunks for a query.

    Hybrid (lexical + semantic) when local embeddings are enabled; otherwise
    pure TF-IDF. Each hit is ``{doc_id, title, ordinal, section, score, content}``.
    Used by the chat's ``search_documents`` tool to ground answers in uploaded
    documents.
    """
    chunks = _load_chunks(study_id)
    if embeddings.available():
        return _rank_hybrid(query or "", chunks, study_id, limit)
    return _rank(query or "", chunks, limit)
