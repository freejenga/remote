"""Compliance Tracker module, incorporated into the clinical agent.

Ported from compliance_tracker/app.py. Tracks study subjects, their visit
logs, and a shared app date. Subjects key off `subjectId`, which lines up with
the dispatch module's `subject`/`voucher` and the protocol agent's subjects --
the shared identifier that ties the three modules together.
"""
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator, model_validator

from .store import get_conn, pack, unpack
from . import audit

router = APIRouter(prefix='/compliance', tags=['compliance'])


class AppDate(BaseModel):
    date: Optional[str] = None


# ---------------------------------------------------------------------------
# Light validation models — extra fields are explicitly allowed so that the
# HTML UI can post arbitrary subject/visit-log objects without being rejected.
# Only clearly invalid inputs (missing/empty key fields) raise HTTP 422.
# ---------------------------------------------------------------------------

class SubjectRecord(BaseModel):
    """Permissive subject record: requires a non-empty subjectId, allows anything else."""
    model_config = {'extra': 'allow'}

    subjectId: str

    @field_validator('subjectId')
    @classmethod
    def subject_id_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError('subjectId must be a non-empty string')
        return v


class VisitLogRecord(BaseModel):
    """Permissive visit-log record: requires a non-empty id, allows anything else."""
    model_config = {'extra': 'allow'}

    id: str

    @field_validator('id')
    @classmethod
    def id_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError('id must be a non-empty string')
        return v


def _validate_subjects(raw: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Validate each subject dict; raises HTTPException(422) on first failure."""
    validated = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise HTTPException(status_code=422, detail=f'subjects[{i}] must be an object')
        try:
            rec = SubjectRecord(**item)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f'subjects[{i}]: {exc}') from exc
        # model_dump(mode='python') with extra='allow' returns all fields including extras
        validated.append(rec.model_dump(mode='python'))
    return validated


def _validate_visit_logs(raw: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Validate each visit-log dict; raises HTTPException(422) on first failure."""
    validated = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise HTTPException(status_code=422, detail=f'logs[{i}] must be an object')
        try:
            rec = VisitLogRecord(**item)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f'logs[{i}]: {exc}') from exc
        validated.append(rec.model_dump(mode='python'))
    return validated


@router.get('/saved_subjects')
def get_subjects():
    with get_conn() as conn:
        rows = conn.execute('SELECT data FROM subjects').fetchall()
    return {'data': [unpack(r['data']) for r in rows]}


@router.post('/saved_subjects')
def save_subjects(subjects: List[dict]):
    validated = _validate_subjects(subjects)
    with get_conn() as conn:
        conn.execute('DELETE FROM subjects')
        conn.executemany(
            'INSERT INTO subjects (subjectId, data) VALUES (?, ?)',
            [(s.get('subjectId'), pack(s)) for s in validated],
        )
    audit.record('compliance.save_subjects', {'count': len(validated)})
    return {'success': True}


@router.get('/visit_logs')
def get_visit_logs():
    with get_conn() as conn:
        rows = conn.execute('SELECT data FROM visit_logs ORDER BY rowid ASC').fetchall()
    return {'data': [unpack(r['data']) for r in rows]}


@router.post('/visit_logs')
def save_visit_logs(logs: List[dict]):
    validated = _validate_visit_logs(logs)
    with get_conn() as conn:
        conn.execute('DELETE FROM visit_logs')
        conn.executemany(
            'INSERT INTO visit_logs (id, data) VALUES (?, ?)',
            [(log.get('id', 'temp_id'), pack(log)) for log in validated],
        )
    audit.record('compliance.save_visit_logs', {'count': len(validated)})
    return {'success': True}


@router.get('/app_date')
def get_app_date():
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM app_state WHERE key='app_date'").fetchone()
    return {'data': row['value'] if row else None}


@router.post('/app_date')
def save_app_date(data: AppDate):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO app_state (key, value) VALUES ('app_date', ?)",
            (data.date,),
        )
    audit.record('compliance.set_app_date', {'date': data.date})
    return {'success': True}


# ---------------------------------------------------------------------------
# Referential integrity report (read-only, no hard foreign-key enforcement)
# ---------------------------------------------------------------------------

@router.get('/integrity')
def integrity_report():
    """Return a soft referential-integrity report.

    Checks:
    - visit_logs whose subjectId does not match any compliance subject
    - dispatch trips whose subject does not match any compliance subject

    No data is modified; creation order is not enforced.
    """
    with get_conn() as conn:
        subject_rows = conn.execute('SELECT subjectId FROM subjects').fetchall()
        log_rows = conn.execute('SELECT id, data FROM visit_logs').fetchall()
        trip_rows = conn.execute('SELECT id, data FROM trips').fetchall()

    known_subjects = {r['subjectId'] for r in subject_rows if r['subjectId']}

    orphan_logs = []
    for row in log_rows:
        try:
            d = unpack(row['data'])
        except Exception:
            d = {}
        sid = d.get('subjectId') or d.get('subject')
        if sid not in known_subjects:
            orphan_logs.append({'id': row['id'], 'subjectId': sid})

    orphan_trips = []
    for row in trip_rows:
        try:
            d = unpack(row['data'])
        except Exception:
            d = {}
        subject = d.get('subject')
        if subject not in known_subjects:
            orphan_trips.append({'id': row['id'], 'subject': subject})

    return {
        'known_subject_count': len(known_subjects),
        'orphan_visit_logs': orphan_logs,
        'orphan_trips': orphan_trips,
        'ok': not orphan_logs and not orphan_trips,
    }
