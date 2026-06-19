"""Tiny shared SQLite store for the integrated platform modules.

The clinical protocol agent itself is stateless (parse in -> schedule out).
The incorporated Compliance and Dispatch modules need persistence, so they
share one SQLite database created here. A single connection-per-call model is
used for simplicity and thread-safety under uvicorn's default worker.

Schema versioning
-----------------
``PRAGMA user_version`` tracks the applied migration level.  ``init_db()``
applies each migration exactly once, in order, making it safe to call on
every startup against both a fresh and an existing database.

Current versions
~~~~~~~~~~~~~~~~
  0 -> 1  Create all core tables (idempotent via CREATE TABLE IF NOT EXISTS)
  1 -> 2  Add performance indexes
"""
import json
import os
import sqlite3
from contextlib import contextmanager

from . import crypto

DB_FILE = os.environ.get('PLATFORM_DB', 'clinical_platform.db')

# Increment this constant whenever a new migration step is added below.
_SCHEMA_VERSION = 5


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def pack(obj) -> str:
    """Serialize + (optionally) encrypt an object for a `data`/`content` column."""
    return crypto.encrypt(json.dumps(obj))


def unpack(blob: str):
    """Inverse of `pack`: decrypt (if needed) + deserialize."""
    return json.loads(crypto.decrypt(blob))


def _get_user_version(conn) -> int:
    return conn.execute('PRAGMA user_version').fetchone()[0]


def _set_user_version(conn, version: int) -> None:
    # PRAGMA user_version does not accept bound parameters; the value is an int.
    conn.execute(f'PRAGMA user_version = {int(version)}')


def _migrate_v0_to_v1(c) -> None:
    """Create all core tables (idempotent)."""
    # Compliance module
    c.execute('CREATE TABLE IF NOT EXISTS subjects (subjectId TEXT PRIMARY KEY, data TEXT)')
    c.execute('CREATE TABLE IF NOT EXISTS visit_logs (id TEXT PRIMARY KEY, data TEXT)')
    c.execute('CREATE TABLE IF NOT EXISTS app_state (key TEXT PRIMARY KEY, value TEXT)')
    # Dispatch module
    c.execute('CREATE TABLE IF NOT EXISTS trips (id TEXT PRIMARY KEY, data TEXT, created_at TEXT)')
    # Chat module — persistent learning + conversation log
    c.execute(
        'CREATE TABLE IF NOT EXISTS chat_memory '
        '(id TEXT PRIMARY KEY, subject TEXT, kind TEXT, content TEXT, created_at TEXT)'
    )
    # Audit module — append-only, hash-chained tamper-evident log
    c.execute(
        'CREATE TABLE IF NOT EXISTS audit_log '
        '(rowid INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT, ts TEXT, actor TEXT, '
        'action TEXT, detail TEXT, prev_hash TEXT, hash TEXT)'
    )


def _migrate_v1_to_v2(c) -> None:
    """Add performance indexes (all idempotent via CREATE INDEX IF NOT EXISTS)."""
    # Trips ordered by creation time (used by list_trips and invoice)
    c.execute(
        'CREATE INDEX IF NOT EXISTS idx_trips_created_at ON trips (created_at)'
    )
    # Chat memory: look up learnings by subject
    c.execute(
        'CREATE INDEX IF NOT EXISTS idx_chat_memory_subject ON chat_memory (subject)'
    )
    # Chat memory: filter by kind (learning vs conversation)
    c.execute(
        'CREATE INDEX IF NOT EXISTS idx_chat_memory_kind ON chat_memory (kind)'
    )
    # Audit log: filter by action and actor
    c.execute(
        'CREATE INDEX IF NOT EXISTS idx_audit_log_action ON audit_log (action)'
    )
    c.execute(
        'CREATE INDEX IF NOT EXISTS idx_audit_log_actor ON audit_log (actor)'
    )


def _migrate_v2_to_v3(c) -> None:
    """Add the studies registry and persisted protocol versions.

    A *study* groups protocol *versions*; each version stores the parsed
    schedule (packed/encrypted JSON) plus its review status. This turns the
    previously stateless parser into a persistent, multi-study workflow.
    """
    c.execute(
        'CREATE TABLE IF NOT EXISTS studies '
        '(id TEXT PRIMARY KEY, name TEXT NOT NULL, sponsor TEXT, created_at TEXT)'
    )
    c.execute(
        'CREATE TABLE IF NOT EXISTS protocol_versions '
        '(id TEXT PRIMARY KEY, study_id TEXT NOT NULL, label TEXT NOT NULL, '
        "status TEXT NOT NULL DEFAULT 'draft', source_type TEXT, schedule TEXT, "
        'reviewer TEXT, comments TEXT, created_at TEXT, updated_at TEXT)'
    )
    # Versions are always listed/filtered by their parent study.
    c.execute(
        'CREATE INDEX IF NOT EXISTS idx_versions_study '
        'ON protocol_versions (study_id)'
    )


def _migrate_v3_to_v4(c) -> None:
    """Add the document knowledge base for retrieval-augmented Q&A.

    Uploaded trial documents (protocols, source-doc templates, memos) are split
    into chunks for semantic retrieval. ``documents`` is the per-file record;
    ``doc_chunks`` holds the (de-identified, encrypted) text the AI searches.
    Keeping chunks in their own table lets retrieval rank at chunk granularity
    while still grouping results back to their source document.
    """
    c.execute(
        'CREATE TABLE IF NOT EXISTS documents '
        '(id TEXT PRIMARY KEY, study_id TEXT, title TEXT NOT NULL, '
        'source_type TEXT, chunk_count INTEGER, created_at TEXT)'
    )
    c.execute(
        'CREATE TABLE IF NOT EXISTS doc_chunks '
        '(id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, study_id TEXT, '
        'ordinal INTEGER, content TEXT, created_at TEXT)'
    )
    # Chunks are always fetched/deleted by their parent document.
    c.execute(
        'CREATE INDEX IF NOT EXISTS idx_doc_chunks_doc ON doc_chunks (doc_id)'
    )
    # Documents and chunks are filtered to a study during scoped retrieval.
    c.execute(
        'CREATE INDEX IF NOT EXISTS idx_documents_study ON documents (study_id)'
    )
    c.execute(
        'CREATE INDEX IF NOT EXISTS idx_doc_chunks_study ON doc_chunks (study_id)'
    )


def _migrate_v4_to_v5(c) -> None:
    """Upgrade the knowledge base for table-aware, hybrid retrieval.

    Two additions, both backward-compatible with the v4 RAG layer:

    * ``doc_chunks.section`` — a label (e.g. "Inclusion Criteria", "Schedule of
      Events") attached at ingest by section-aware chunking, so retrieval can
      surface *which* part of a protocol a passage came from. Existing rows keep
      NULL and behave exactly as before.
    * ``doc_vectors`` — optional per-chunk embedding vectors (float32 blob) for
      semantic search. Populated only when local embeddings are enabled; when
      empty, retrieval transparently falls back to TF-IDF. Vectors are computed
      over the *already de-identified* chunk text, so the boundary is unchanged.
    """
    # Idempotent ADD COLUMN: tolerate re-runs / partial upgrades.
    cols = {r[1] for r in c.execute('PRAGMA table_info(doc_chunks)').fetchall()}
    if 'section' not in cols:
        c.execute('ALTER TABLE doc_chunks ADD COLUMN section TEXT')
    c.execute(
        'CREATE TABLE IF NOT EXISTS doc_vectors '
        '(chunk_id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, study_id TEXT, '
        'dim INTEGER, model TEXT, vector BLOB, created_at TEXT)'
    )
    # Vectors are loaded/deleted alongside their parent document.
    c.execute(
        'CREATE INDEX IF NOT EXISTS idx_doc_vectors_doc ON doc_vectors (doc_id)'
    )
    c.execute(
        'CREATE INDEX IF NOT EXISTS idx_doc_vectors_study ON doc_vectors (study_id)'
    )


# Ordered list of (from_version, migration_function) pairs.
_MIGRATIONS = [
    (0, _migrate_v0_to_v1),
    (1, _migrate_v1_to_v2),
    (2, _migrate_v2_to_v3),
    (3, _migrate_v3_to_v4),
    (4, _migrate_v4_to_v5),
]


def init_db():
    """Apply all pending schema migrations idempotently."""
    with get_conn() as conn:
        current = _get_user_version(conn)
        for from_ver, migrate_fn in _MIGRATIONS:
            if current == from_ver:
                migrate_fn(conn.cursor())
                current = from_ver + 1
                _set_user_version(conn, current)
