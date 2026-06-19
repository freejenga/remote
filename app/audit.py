"""Append-only, hash-chained audit trail.

Every data mutation, export, and chat query records an entry whose ``hash`` is
``sha256(prev_hash + ts + actor + action + detail)``. Because each entry commits
the previous entry's hash, any later edit/deletion of a row breaks the chain
from that point on -- ``verify()`` recomputes the chain and reports the first
break. This is the tamper-evident "who did what, when" log that 21 CFR Part 11 /
HIPAA require.

Details are kept code-level (IDs, counts, subject codes, amounts) -- not raw
identifiers -- so the audit log itself doesn't become a PHI store.
"""
import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter

from .store import get_conn
from . import context as _ctx

_GENESIS = "GENESIS"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(prev: str, ts: str, actor: str, action: str, detail: str) -> str:
    return hashlib.sha256(
        (prev + "|" + ts + "|" + actor + "|" + action + "|" + detail).encode()
    ).hexdigest()


def record(action: str, detail: Optional[dict] = None,
           actor: Optional[str] = None) -> dict:
    """Append a tamper-evident audit entry. Never raises into the caller."""
    ts = _now()
    actor = actor or _ctx.get_actor() or "system"
    detail_s = json.dumps(detail, default=str, sort_keys=True) if detail is not None else ""
    try:
        with get_conn() as conn:
            last = conn.execute(
                "SELECT hash FROM audit_log ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            prev = last["hash"] if last else _GENESIS
            h = _hash(prev, ts, actor, action, detail_s)
            entry_id = f"aud_{uuid.uuid4().hex[:12]}"
            conn.execute(
                "INSERT INTO audit_log (id, ts, actor, action, detail, prev_hash, hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (entry_id, ts, actor, action, detail_s, prev, h),
            )
        return {"id": entry_id, "ts": ts, "actor": actor, "action": action, "hash": h}
    except Exception:
        # Auditing must never break the underlying operation.
        return {}


def entries(limit: int = 100) -> List[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, ts, actor, action, detail, hash FROM audit_log "
            "ORDER BY rowid DESC LIMIT ?", (limit,)
        ).fetchall()
    out = []
    for r in rows:
        out.append({
            "id": r["id"], "ts": r["ts"], "actor": r["actor"],
            "action": r["action"],
            "detail": json.loads(r["detail"]) if r["detail"] else None,
            "hash": r["hash"],
        })
    return out


def verify() -> dict:
    """Recompute the hash chain; report integrity and the first broken entry."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, ts, actor, action, detail, prev_hash, hash FROM audit_log "
            "ORDER BY rowid ASC"
        ).fetchall()
    prev = _GENESIS
    for r in rows:
        expected = _hash(prev, r["ts"], r["actor"], r["action"], r["detail"] or "")
        if r["prev_hash"] != prev or r["hash"] != expected:
            return {"ok": False, "count": len(rows), "broken_at": r["id"]}
        prev = r["hash"]
    return {"ok": True, "count": len(rows), "broken_at": None}


router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("/entries")
def list_entries(limit: int = 100):
    return {"entries": entries(limit)}


@router.get("/verify")
def verify_chain():
    return verify()
