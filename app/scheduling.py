"""Deterministic visit scheduling, windows, and compliance.

This is the time-math half of the platform's "AI proposes, the deterministic
core disposes" design. **No LLM is ever involved in date or compliance
calculation** -- every function here is pure code, so a subject's visit calendar
and its compliance verdicts are reproducible and auditable (21 CFR Part 11).

Two layers:

  * **Pure algorithms** -- ``parse_window``, ``visit_window``,
    ``compliance_status``, ``next_due``. No I/O; trivially testable.
  * **Persistence + API** -- materialize a parsed protocol's Schedule of Events
    into ``soe_events``; generate a subject's visit calendar into
    ``subject_visits``; record actual visit dates; report compliance and the
    next-due visit. All mutations are recorded in the tamper-evident audit log.

Convention (shared with ``assembler``): protocol **Day 1 = the enrollment
(anchor) date**, so a visit at protocol day ``d`` targets ``anchor + (d - 1)``.
"""
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional, Tuple

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator

from .store import get_conn, unpack
from .cycle_engine import infer_day
from . import audit

router = APIRouter(prefix='/scheduling', tags=['scheduling'])


# ---------------------------------------------------------------------------
# Pure algorithms (no I/O)
# ---------------------------------------------------------------------------
def parse_window(window: Optional[str]) -> Tuple[int, int]:
    """Parse a protocol visit window into ``(days_before, days_after)``.

    Handles the common clinical spellings, e.g.::

        "±3 days" -> (3, 3)      "+3/-2"   -> (2, 3)
        "+7 days" -> (0, 7)      "-2 days" -> (2, 0)
        "-2 to +3"-> (2, 3)      "±0"/None -> (0, 0)

    Returns ``(0, 0)`` for anything it can't interpret (a hard, on-day visit).
    """
    if not window:
        return (0, 0)
    s = str(window).strip().lower().replace('−', '-').replace('–', '-').replace('—', '-')

    if '±' in s or '+/-' in s or '+ /-' in s:
        m = re.search(r'(\d+)', s)
        n = int(m.group(1)) if m else 0
        return (n, n)

    plus = re.search(r'\+\s*(\d+)', s)
    minus = re.search(r'(?<!\d)-\s*(\d+)', s)
    if plus or minus:
        return (int(minus.group(1)) if minus else 0,
                int(plus.group(1)) if plus else 0)

    m = re.search(r'(\d+)\s*to\s*(\d+)', s)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        return (min(a, b), max(a, b))

    m = re.search(r'(\d+)', s)
    if m:
        n = int(m.group(1))
        return (n, n)
    return (0, 0)


def _as_date(value) -> Optional[date]:
    if value is None or value == '':
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def visit_window(anchor, day: Optional[int], window: Optional[str]) -> dict:
    """Compute a visit's target/earliest/latest dates from the enrollment anchor.

    ``anchor`` is the enrollment date (Day 1). Returns ISO date strings, or all
    ``None`` when the day offset is unknown. Raises ValueError on a bad anchor.
    """
    anchor_d = _as_date(anchor)
    if anchor_d is None or day is None:
        return {'target_date': None, 'earliest_date': None, 'latest_date': None}
    target = anchor_d + timedelta(days=day - 1)
    before, after = parse_window(window)
    return {
        'target_date': target.isoformat(),
        'earliest_date': (target - timedelta(days=before)).isoformat(),
        'latest_date': (target + timedelta(days=after)).isoformat(),
    }


def compliance_status(window: dict, actual=None, today=None) -> str:
    """Classify a visit's compliance.

    With an ``actual`` date: ``on-time`` / ``early`` / ``late`` relative to the
    window. Without one: ``upcoming`` / ``due`` / ``missed`` relative to
    ``today``. Returns ``unknown`` if the window has no computable dates.
    """
    earliest = _as_date(window.get('earliest_date'))
    latest = _as_date(window.get('latest_date'))
    if earliest is None or latest is None:
        return 'unknown'

    actual_d = _as_date(actual)
    if actual_d is not None:
        if actual_d < earliest:
            return 'early'
        if actual_d > latest:
            return 'late'
        return 'on-time'

    today_d = _as_date(today) or date.today()
    if today_d > latest:
        return 'missed'
    if today_d >= earliest:
        return 'due'
    return 'upcoming'


def next_due(visits: List[dict], today=None) -> Optional[dict]:
    """Return the soonest not-yet-completed visit (by target date), or None.

    ``visits`` are dicts with ``target_date``/``earliest_date``/``latest_date``
    and optional ``actual_date``. Completed visits (actual recorded) and missed
    visits are skipped; the remaining ``upcoming``/``due`` visit with the
    earliest target date wins.
    """
    candidates = []
    for v in visits:
        if _as_date(v.get('actual_date')) is not None:
            continue
        status = compliance_status(v, actual=None, today=today)
        if status in ('upcoming', 'due'):
            target = _as_date(v.get('target_date'))
            if target is not None:
                candidates.append((target, status, v))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    target, status, v = candidates[0]
    out = dict(v)
    out['status'] = status
    return out


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_default() -> Optional[str]:
    """The compliance module's shared app_date, if one is set (else None)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM app_state WHERE key='app_date'").fetchone()
    return row['value'] if row and row['value'] else None


def _load_version_schedule(conn, study_id: str,
                           version_id: Optional[str]) -> Tuple[str, dict]:
    """Return ``(version_id, schedule_dict)`` for a study, newest if unspecified."""
    if version_id:
        row = conn.execute(
            'SELECT id, schedule FROM protocol_versions '
            'WHERE id = ? AND study_id = ?', (version_id, study_id)).fetchone()
    else:
        row = conn.execute(
            'SELECT id, schedule FROM protocol_versions '
            'WHERE study_id = ? ORDER BY created_at DESC LIMIT 1',
            (study_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail='No protocol version found')
    if not row['schedule']:
        raise HTTPException(status_code=400, detail='Version has no parsed schedule')
    return row['id'], unpack(row['schedule'])


def materialize_soe(study_id: str, version_id: Optional[str] = None) -> dict:
    """Flatten a parsed protocol version's schedule into ``soe_events`` rows.

    One row per visit/activity. Replaces any existing rows for that version so
    re-running is idempotent. Visit day is taken from the parsed visit or, if
    absent, inferred from the visit name (``cycle_engine.infer_day``).
    """
    now = _now()
    with get_conn() as conn:
        vid, schedule = _load_version_schedule(conn, study_id, version_id)
        rows = []
        ordinal = 0
        for visit in schedule.get('visits', []):
            day = visit.get('day')
            if day is None:
                day = infer_day(visit.get('name', '') or '')
            window = visit.get('window')
            procedures = visit.get('procedures') or [{}]
            for proc in procedures:
                rows.append((
                    f'soe_{uuid.uuid4().hex[:12]}', study_id, vid, ordinal,
                    visit.get('name'), day, window,
                    proc.get('name'), proc.get('conditional'), now,
                ))
                ordinal += 1
        conn.execute('DELETE FROM soe_events WHERE version_id = ?', (vid,))
        conn.executemany(
            'INSERT INTO soe_events (id, study_id, version_id, ordinal, '
            'visit_name, day, window, activity, conditional, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', rows)
    audit.record('scheduling.materialize_soe',
                 {'study_id': study_id, 'version_id': vid, 'rows': len(rows)})
    return {'study_id': study_id, 'version_id': vid, 'event_count': len(rows)}


def _distinct_visits(conn, study_id: str, version_id: str) -> List[dict]:
    """Distinct visits (name/day/window) for a version, in schedule order."""
    rows = conn.execute(
        'SELECT visit_name, day, window, MIN(ordinal) AS ord FROM soe_events '
        'WHERE version_id = ? GROUP BY visit_name, day, window ORDER BY ord',
        (version_id,)).fetchall()
    return [{'visit_name': r['visit_name'], 'day': r['day'],
             'window': r['window']} for r in rows]


def generate_calendar(subject_id: str, study_id: str, enrollment_date: str,
                      version_id: Optional[str] = None) -> dict:
    """Create a subject's visit calendar from a study's SoE.

    Computes each visit's window from the enrollment anchor and stores one
    ``subject_visits`` row per visit. Replaces any existing calendar for that
    subject+study. Raises ValueError on a malformed ``enrollment_date``.
    """
    try:
        anchor = date.fromisoformat(enrollment_date)
    except (ValueError, TypeError) as exc:
        raise ValueError('enrollment_date must be ISO YYYY-MM-DD') from exc

    now = _now()
    with get_conn() as conn:
        vid, _schedule = _load_version_schedule(conn, study_id, version_id)
        visits = _distinct_visits(conn, study_id, vid)
        if not visits:
            raise HTTPException(
                status_code=400,
                detail='No Schedule of Events materialized for this version; '
                       'POST the SoE first.')
        rows = []
        for v in visits:
            w = visit_window(anchor, v['day'], v['window'])
            rows.append((
                f'sv_{uuid.uuid4().hex[:12]}', subject_id, study_id, vid,
                v['visit_name'], v['day'], v['window'],
                w['target_date'], w['earliest_date'], w['latest_date'],
                None, now, now,
            ))
        conn.execute(
            'DELETE FROM subject_visits WHERE subject_id = ? AND study_id = ?',
            (subject_id, study_id))
        conn.executemany(
            'INSERT INTO subject_visits (id, subject_id, study_id, version_id, '
            'visit_name, day, window, target_date, earliest_date, latest_date, '
            'actual_date, created_at, updated_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', rows)
    audit.record('scheduling.generate_calendar',
                 {'subject_id': subject_id, 'study_id': study_id,
                  'version_id': vid, 'visits': len(rows)})
    return {'subject_id': subject_id, 'study_id': study_id, 'version_id': vid,
            'enrollment_date': enrollment_date, 'visit_count': len(rows)}


def record_actual(visit_id: str, actual_date: str) -> dict:
    """Record the actual date a subject completed a scheduled visit."""
    try:
        date.fromisoformat(actual_date)
    except (ValueError, TypeError) as exc:
        raise ValueError('actual_date must be ISO YYYY-MM-DD') from exc
    with get_conn() as conn:
        cur = conn.execute(
            'UPDATE subject_visits SET actual_date = ?, updated_at = ? WHERE id = ?',
            (actual_date, _now(), visit_id))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail='Visit not found')
    audit.record('scheduling.record_actual',
                 {'visit_id': visit_id, 'actual_date': actual_date})
    return {'id': visit_id, 'actual_date': actual_date}


def _visit_rows(conn, study_id: Optional[str] = None,
                subject_id: Optional[str] = None) -> List[dict]:
    q = ('SELECT id, subject_id, study_id, version_id, visit_name, day, window, '
         'target_date, earliest_date, latest_date, actual_date FROM subject_visits')
    clauses, params = [], []
    if study_id:
        clauses.append('study_id = ?')
        params.append(study_id)
    if subject_id:
        clauses.append('subject_id = ?')
        params.append(subject_id)
    if clauses:
        q += ' WHERE ' + ' AND '.join(clauses)
    q += ' ORDER BY subject_id, target_date'
    return [dict(r) for r in conn.execute(q, params).fetchall()]


def compliance_report(study_id: Optional[str] = None,
                      subject_id: Optional[str] = None,
                      today: Optional[str] = None) -> dict:
    """Compute a live compliance report over stored subject visits."""
    today = today or _today_default()
    with get_conn() as conn:
        rows = _visit_rows(conn, study_id=study_id, subject_id=subject_id)
    summary: dict = {}
    for r in rows:
        status = compliance_status(r, actual=r.get('actual_date'), today=today)
        r['status'] = status
        summary[status] = summary.get(status, 0) + 1
    return {'study_id': study_id, 'subject_id': subject_id, 'today': today,
            'total': len(rows), 'summary': summary, 'visits': rows}


def next_due_report(study_id: Optional[str] = None,
                    today: Optional[str] = None) -> dict:
    """Soonest upcoming/due visit per subject, plus the single nearest overall."""
    today = today or _today_default()
    with get_conn() as conn:
        rows = _visit_rows(conn, study_id=study_id)
    by_subject: dict = {}
    for r in rows:
        by_subject.setdefault(r['subject_id'], []).append(r)
    per_subject = []
    for sid, visits in by_subject.items():
        nxt = next_due(visits, today=today)
        if nxt:
            per_subject.append(nxt)
    per_subject.sort(key=lambda v: v['target_date'])
    return {'study_id': study_id, 'today': today,
            'next_overall': per_subject[0] if per_subject else None,
            'by_subject': per_subject}


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
class CalendarIn(BaseModel):
    study_id: str
    enrollment_date: str
    version_id: Optional[str] = None

    @field_validator('study_id', 'enrollment_date')
    @classmethod
    def _not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError('must be a non-empty string')
        return v.strip()


class ActualIn(BaseModel):
    actual_date: str


@router.post('/studies/{study_id}/soe')
def post_materialize_soe(study_id: str, version_id: Optional[str] = None):
    return materialize_soe(study_id, version_id)


@router.get('/studies/{study_id}/soe')
def get_soe(study_id: str, version_id: Optional[str] = None):
    with get_conn() as conn:
        if version_id:
            rows = conn.execute(
                'SELECT visit_name, day, window, activity, conditional FROM soe_events '
                'WHERE study_id = ? AND version_id = ? ORDER BY ordinal',
                (study_id, version_id)).fetchall()
        else:
            rows = conn.execute(
                'SELECT visit_name, day, window, activity, conditional FROM soe_events '
                'WHERE study_id = ? ORDER BY ordinal', (study_id,)).fetchall()
    return {'study_id': study_id, 'events': [dict(r) for r in rows]}


@router.post('/subjects/{subject_id}/calendar')
def post_calendar(subject_id: str, body: CalendarIn):
    try:
        return generate_calendar(subject_id, body.study_id, body.enrollment_date,
                                 body.version_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get('/subjects/{subject_id}/calendar')
def get_calendar(subject_id: str, today: Optional[str] = None):
    return compliance_report(subject_id=subject_id, today=today)


@router.patch('/visits/{visit_id}/actual')
def patch_actual(visit_id: str, body: ActualIn):
    try:
        return record_actual(visit_id, body.actual_date)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get('/compliance')
def get_compliance(study_id: Optional[str] = None, today: Optional[str] = None):
    return compliance_report(study_id=study_id, today=today)


@router.get('/next-due')
def get_next_due(study_id: Optional[str] = None, today: Optional[str] = None):
    return next_due_report(study_id=study_id, today=today)
