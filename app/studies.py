"""Persistent multi-study + protocol-version workflow.

A *study* (e.g. "ABC-101", sponsor "ExampleBio") groups one or more protocol
*versions* (e.g. "v1", "Amendment 1"). Creating a version parses protocol text
through the same pipeline as ``/parse-text`` and persists the resulting
schedule, so the parse result survives restarts and can be reviewed/approved
over time. This is the stateful layer on top of the otherwise stateless parser.

Statuses follow a simple review lifecycle: draft -> in_review -> approved /
rejected. Every mutation is recorded in the tamper-evident audit log.
"""
import uuid
from datetime import datetime, timezone
from typing import List, Optional

import os

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel, field_validator

from .store import get_conn, pack, unpack
from .ingestion import ingest_uploaded_file
from .schemas import FinalSchedule
from .exporter import export_csv, export_xlsx
from . import audit

router = APIRouter(prefix='/studies', tags=['studies'])

VALID_STATUSES = {'draft', 'in_review', 'approved', 'rejected'}

# The parse pipeline (langgraph) is heavy to build, so construct it once on
# first use and reuse it across requests.
_graph = None


def _get_graph():
    global _graph
    if _graph is None:
        from .graph import build_graph  # lazy: langgraph import is expensive
        _graph = build_graph()
    return _graph


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class StudyIn(BaseModel):
    name: str
    sponsor: Optional[str] = None

    @field_validator('name')
    @classmethod
    def name_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError('name must be a non-empty string')
        return v.strip()


class VersionIn(BaseModel):
    label: str
    protocol_text: str
    source_type: Optional[str] = 'text'

    @field_validator('label')
    @classmethod
    def label_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError('label must be a non-empty string')
        return v.strip()

    @field_validator('protocol_text')
    @classmethod
    def text_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError('protocol_text must be a non-empty string')
        return v


class VersionUpdate(BaseModel):
    status: Optional[str] = None
    reviewer: Optional[str] = None
    comments: Optional[str] = None

    @field_validator('status')
    @classmethod
    def status_valid(cls, v):
        if v is not None and v not in VALID_STATUSES:
            raise ValueError(f'status must be one of {sorted(VALID_STATUSES)}')
        return v


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _study_row(conn, study_id: str):
    return conn.execute(
        'SELECT id, name, sponsor, created_at FROM studies WHERE id = ?', (study_id,)
    ).fetchone()


def _version_meta(row) -> dict:
    """Version without the (potentially large) parsed schedule."""
    return {
        'id': row['id'], 'study_id': row['study_id'], 'label': row['label'],
        'status': row['status'], 'source_type': row['source_type'],
        'reviewer': row['reviewer'], 'comments': row['comments'],
        'created_at': row['created_at'], 'updated_at': row['updated_at'],
    }


def _conflict_count(schedule: dict) -> int:
    return len(schedule.get('conflicts', [])) if isinstance(schedule, dict) else 0


# ---------------------------------------------------------------------------
# Studies
# ---------------------------------------------------------------------------
@router.get('')
def list_studies():
    with get_conn() as conn:
        studies = conn.execute(
            'SELECT id, name, sponsor, created_at FROM studies ORDER BY created_at ASC'
        ).fetchall()
        counts = dict(conn.execute(
            'SELECT study_id, COUNT(*) FROM protocol_versions GROUP BY study_id'
        ).fetchall())
    return {'data': [
        {'id': s['id'], 'name': s['name'], 'sponsor': s['sponsor'],
         'created_at': s['created_at'], 'version_count': counts.get(s['id'], 0)}
        for s in studies
    ]}


@router.post('')
def create_study(study: StudyIn):
    sid = f'study_{uuid.uuid4().hex[:12]}'
    created = _now()
    with get_conn() as conn:
        conn.execute(
            'INSERT INTO studies (id, name, sponsor, created_at) VALUES (?, ?, ?, ?)',
            (sid, study.name, study.sponsor, created),
        )
    audit.record('studies.create', {'id': sid, 'name': study.name})
    return {'id': sid, 'name': study.name, 'sponsor': study.sponsor,
            'created_at': created, 'version_count': 0}


@router.get('/{study_id}')
def get_study(study_id: str):
    with get_conn() as conn:
        s = _study_row(conn, study_id)
        if not s:
            raise HTTPException(status_code=404, detail='Study not found')
        vrows = conn.execute(
            'SELECT id, study_id, label, status, source_type, schedule, reviewer, '
            'comments, created_at, updated_at FROM protocol_versions '
            'WHERE study_id = ? ORDER BY created_at ASC', (study_id,)
        ).fetchall()
    versions = []
    for r in vrows:
        meta = _version_meta(r)
        try:
            meta['conflict_count'] = _conflict_count(unpack(r['schedule'])) if r['schedule'] else 0
        except Exception:
            meta['conflict_count'] = 0
        versions.append(meta)
    return {'id': s['id'], 'name': s['name'], 'sponsor': s['sponsor'],
            'created_at': s['created_at'], 'versions': versions}


@router.patch('/{study_id}')
def update_study(study_id: str, study: StudyIn):
    with get_conn() as conn:
        if not _study_row(conn, study_id):
            raise HTTPException(status_code=404, detail='Study not found')
        conn.execute('UPDATE studies SET name = ?, sponsor = ? WHERE id = ?',
                     (study.name, study.sponsor, study_id))
    audit.record('studies.update', {'id': study_id, 'name': study.name})
    return {'id': study_id, 'name': study.name, 'sponsor': study.sponsor}


@router.delete('/{study_id}')
def delete_study(study_id: str):
    with get_conn() as conn:
        if not _study_row(conn, study_id):
            raise HTTPException(status_code=404, detail='Study not found')
        n = conn.execute(
            'SELECT COUNT(*) FROM protocol_versions WHERE study_id = ?', (study_id,)
        ).fetchone()[0]
        conn.execute('DELETE FROM protocol_versions WHERE study_id = ?', (study_id,))
        conn.execute('DELETE FROM studies WHERE id = ?', (study_id,))
    audit.record('studies.delete', {'id': study_id, 'versions_removed': n})
    return {'success': True}


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------
def _persist_version(study_id: str, label: str, protocol_text: str,
                     source_type: str) -> dict:
    """Parse protocol text into a schedule and persist it as a new version.

    Shared by the JSON endpoint (pasted text) and the upload endpoint (file).
    """
    with get_conn() as conn:
        if not _study_row(conn, study_id):
            raise HTTPException(status_code=404, detail='Study not found')

    # Parse the protocol text into a schedule (same pipeline as /parse-text).
    schedule = _get_graph().invoke({'text': protocol_text})['result'].model_dump()

    vid = f'ver_{uuid.uuid4().hex[:12]}'
    ts = _now()
    with get_conn() as conn:
        conn.execute(
            'INSERT INTO protocol_versions '
            '(id, study_id, label, status, source_type, schedule, reviewer, '
            'comments, created_at, updated_at) '
            "VALUES (?, ?, ?, 'draft', ?, ?, NULL, NULL, ?, ?)",
            (vid, study_id, label, source_type, pack(schedule), ts, ts),
        )
    audit.record('studies.create_version',
                 {'study_id': study_id, 'id': vid, 'label': label,
                  'source_type': source_type,
                  'conflicts': _conflict_count(schedule)})
    return {'id': vid, 'study_id': study_id, 'label': label,
            'status': 'draft', 'source_type': source_type,
            'created_at': ts, 'updated_at': ts,
            'conflict_count': _conflict_count(schedule)}


@router.post('/{study_id}/versions')
def create_version(study_id: str, version: VersionIn):
    return _persist_version(study_id, version.label, version.protocol_text,
                            version.source_type or 'text')


@router.post('/{study_id}/versions/upload')
async def create_version_from_file(
    study_id: str,
    label: str = Form(...),
    file: UploadFile = File(...),
):
    """Create a version from an uploaded PDF / Word / Excel / text file."""
    if not label or not label.strip():
        raise HTTPException(status_code=400, detail='label must be a non-empty string')
    try:
        text, source_type = ingest_uploaded_file(file.filename, await file.read())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not text or not text.strip():
        raise HTTPException(
            status_code=400,
            detail='No readable text found in the uploaded file.',
        )
    return _persist_version(study_id, label.strip(), text, source_type)


@router.get('/{study_id}/versions/{version_id}')
def get_version(study_id: str, version_id: str):
    with get_conn() as conn:
        r = conn.execute(
            'SELECT id, study_id, label, status, source_type, schedule, reviewer, '
            'comments, created_at, updated_at FROM protocol_versions '
            'WHERE id = ? AND study_id = ?', (version_id, study_id)
        ).fetchone()
    if not r:
        raise HTTPException(status_code=404, detail='Version not found')
    meta = _version_meta(r)
    meta['schedule'] = unpack(r['schedule']) if r['schedule'] else None
    return meta


@router.get('/{study_id}/versions/{version_id}/export')
def export_version(study_id: str, version_id: str, fmt: str = 'xlsx'):
    """Export a stored version's parsed schedule to CSV or XLSX."""
    with get_conn() as conn:
        r = conn.execute(
            'SELECT label, schedule FROM protocol_versions '
            'WHERE id = ? AND study_id = ?', (version_id, study_id)
        ).fetchone()
    if not r:
        raise HTTPException(status_code=404, detail='Version not found')
    if not r['schedule']:
        raise HTTPException(status_code=400, detail='Version has no parsed schedule')

    schedule = FinalSchedule(**unpack(r['schedule']))
    if fmt.lower() == 'csv':
        path = export_csv(schedule)
        media = 'text/csv'
    else:
        path = export_xlsx(schedule)
        media = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    audit.record('studies.export_version',
                 {'study_id': study_id, 'id': version_id, 'fmt': fmt})
    return FileResponse(path=path, filename=os.path.basename(path), media_type=media)


@router.patch('/{study_id}/versions/{version_id}')
def update_version(study_id: str, version_id: str, upd: VersionUpdate):
    with get_conn() as conn:
        r = conn.execute(
            'SELECT status, reviewer, comments FROM protocol_versions '
            'WHERE id = ? AND study_id = ?', (version_id, study_id)
        ).fetchone()
        if not r:
            raise HTTPException(status_code=404, detail='Version not found')
        status = upd.status if upd.status is not None else r['status']
        reviewer = upd.reviewer if upd.reviewer is not None else r['reviewer']
        comments = upd.comments if upd.comments is not None else r['comments']
        ts = _now()
        conn.execute(
            'UPDATE protocol_versions SET status = ?, reviewer = ?, comments = ?, '
            'updated_at = ? WHERE id = ? AND study_id = ?',
            (status, reviewer, comments, ts, version_id, study_id),
        )
    audit.record('studies.update_version',
                 {'study_id': study_id, 'id': version_id, 'status': status})
    return {'id': version_id, 'study_id': study_id, 'status': status,
            'reviewer': reviewer, 'comments': comments, 'updated_at': ts}


@router.delete('/{study_id}/versions/{version_id}')
def delete_version(study_id: str, version_id: str):
    with get_conn() as conn:
        cur = conn.execute(
            'DELETE FROM protocol_versions WHERE id = ? AND study_id = ?',
            (version_id, study_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail='Version not found')
    audit.record('studies.delete_version', {'study_id': study_id, 'id': version_id})
    return {'success': True}
