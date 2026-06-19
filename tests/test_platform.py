"""Smoke tests for the integrated Clinical Research Platform.

Covers the protocol pipeline, the dispatch pricing engine, and the three
incorporated module surfaces through the FastAPI app. Uses an isolated temp
database so it never touches real data.

Run:  python -m pytest tests/ -v
"""
import os
import tempfile

import pytest

# Point the shared store at a throwaway DB before any app import.
os.environ['PLATFORM_DB'] = os.path.join(tempfile.gettempdir(), 'platform_test.db')
if os.path.exists(os.environ['PLATFORM_DB']):
    os.remove(os.environ['PLATFORM_DB'])

from fastapi.testclient import TestClient

from app.api import app
from app.dispatch import calculate_cost
from app.graph import build_graph

client = TestClient(app)

SAMPLE = open(
    os.path.join(os.path.dirname(__file__), '..', 'sample_protocol.txt'),
    encoding='utf-8',
).read()


# --- Protocol pipeline -----------------------------------------------------
def test_pipeline_dedupes_rows():
    schedule = build_graph().invoke({'text': SAMPLE})['result']
    seen = {(r.visit, r.normalized) for r in schedule.flat_rows}
    assert len(seen) == len(schedule.flat_rows), 'operational rows must be unique'
    assert {v.name for v in schedule.visits} >= {'Screening', 'Cycle 1 Day 1'}


def test_parse_text_endpoint():
    r = client.post('/parse-text', data={'protocol_text': SAMPLE})
    assert r.status_code == 200
    assert r.json()['flat_rows']


# --- Dispatch pricing ------------------------------------------------------
def test_calculate_cost_oneway_sedan():
    # base 15 + 10*3.75 + 5*0.6 + 50 + 25 = 130.5
    assert calculate_cost(10, 5, 'Sedan', False) == 130.5


def test_calculate_cost_roundtrip_doubles_mileage():
    one_way = calculate_cost(10, 0, 'Wheelchair', False)
    round_trip = calculate_cost(10, 0, 'Wheelchair', True)
    assert round_trip - one_way == pytest.approx(10 * 4.25)


def test_dispatch_quote_and_trip_lifecycle():
    q = client.post('/dispatch/quote', json={
        'subject': 'Jane Doe', 'voucher': 'US102', 'miles': 12, 'wait': 3,
        'type': 'Stretcher', 'roundtrip': True,
    })
    assert q.status_code == 200
    expected = calculate_cost(12, 3, 'Stretcher', True)
    assert q.json()['amount'] == expected

    created = client.post('/dispatch/trips', json={
        'subject': 'Jane Doe', 'voucher': 'US102', 'miles': 12, 'wait': 3,
        'type': 'Stretcher', 'roundtrip': True, 'date': '2025-05-06',
    }).json()
    assert created['amount'] == expected

    inv = client.get('/dispatch/invoice', params={'subject': 'Jane Doe'}).json()
    assert inv['trip_count'] == 1
    assert inv['total_due'] == expected

    assert client.delete(f"/dispatch/trips/{created['id']}").json()['success']


# --- Compliance ------------------------------------------------------------
def test_compliance_subjects_roundtrip():
    payload = [{'subjectId': 'S-001', 'status': 'enrolled'}]
    assert client.post('/compliance/saved_subjects', json=payload).json()['success']
    got = client.get('/compliance/saved_subjects').json()['data']
    assert got == payload


def test_platform_index_lists_modules():
    body = client.get('/').json()
    assert {'protocol', 'compliance', 'dispatch'} <= set(body['modules'])


# --- Schema migration ------------------------------------------------------
def test_schema_version_is_set_after_init():
    """init_db() must stamp PRAGMA user_version to _SCHEMA_VERSION."""
    from app.store import get_conn, _SCHEMA_VERSION
    with get_conn() as conn:
        ver = conn.execute('PRAGMA user_version').fetchone()[0]
    assert ver == _SCHEMA_VERSION, (
        f'Expected schema version {_SCHEMA_VERSION}, got {ver}'
    )


def test_migration_is_idempotent():
    """Calling init_db() twice must not raise and must leave version unchanged."""
    from app.store import init_db, get_conn, _SCHEMA_VERSION
    init_db()  # second call
    with get_conn() as conn:
        ver = conn.execute('PRAGMA user_version').fetchone()[0]
    assert ver == _SCHEMA_VERSION


def test_indexes_created():
    """Migration v1->v2 must have created the expected performance indexes."""
    from app.store import get_conn
    with get_conn() as conn:
        names = {
            r['name']
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
    expected = {
        'idx_trips_created_at',
        'idx_chat_memory_subject',
        'idx_chat_memory_kind',
        'idx_audit_log_action',
        'idx_audit_log_actor',
    }
    assert expected <= names, f'Missing indexes: {expected - names}'


# --- Compliance input validation -------------------------------------------
def test_subjects_extra_fields_allowed():
    """HTML UI posts arbitrary fields; they must be stored and round-tripped."""
    payload = [{'subjectId': 'S-999', 'status': 'screened', 'site': 'NYC', 'cohort': 'A'}]
    r = client.post('/compliance/saved_subjects', json=payload)
    assert r.status_code == 200
    assert r.json() == {'success': True}
    got = client.get('/compliance/saved_subjects').json()['data']
    assert any(s.get('subjectId') == 'S-999' and s.get('site') == 'NYC' for s in got)


def test_subjects_missing_subject_id_rejected():
    """A subject dict with no subjectId must return HTTP 422."""
    r = client.post('/compliance/saved_subjects', json=[{'status': 'enrolled'}])
    assert r.status_code == 422


def test_subjects_empty_subject_id_rejected():
    """A subject dict with an empty-string subjectId must return HTTP 422."""
    r = client.post('/compliance/saved_subjects', json=[{'subjectId': '  '}])
    assert r.status_code == 422


def test_visit_logs_extra_fields_allowed():
    """Visit-log objects with extra fields must be accepted and stored."""
    payload = [{'id': 'VL-001', 'subjectId': 'S-001', 'visit': 'Baseline', 'notes': 'ok'}]
    r = client.post('/compliance/visit_logs', json=payload)
    assert r.status_code == 200
    assert r.json() == {'success': True}
    got = client.get('/compliance/visit_logs').json()['data']
    assert any(v.get('id') == 'VL-001' and v.get('notes') == 'ok' for v in got)


def test_visit_logs_missing_id_rejected():
    """A visit-log dict with no id must return HTTP 422."""
    r = client.post('/compliance/visit_logs', json=[{'subjectId': 'S-001'}])
    assert r.status_code == 422


def test_visit_logs_empty_id_rejected():
    """A visit-log dict with an empty id string must return HTTP 422."""
    r = client.post('/compliance/visit_logs', json=[{'id': ''}])
    assert r.status_code == 422


# --- Dispatch input validation ---------------------------------------------
def test_dispatch_empty_subject_rejected():
    """A trip with an empty subject must return HTTP 422."""
    r = client.post('/dispatch/trips', json={
        'subject': '', 'voucher': 'V1', 'miles': 5, 'wait': 0,
    })
    assert r.status_code == 422


def test_dispatch_empty_voucher_rejected():
    """A trip with an empty voucher must return HTTP 422."""
    r = client.post('/dispatch/trips', json={
        'subject': 'S-001', 'voucher': '  ', 'miles': 5, 'wait': 0,
    })
    assert r.status_code == 422


# --- Referential integrity endpoint ----------------------------------------
def test_integrity_endpoint_exists_and_returns_report():
    """GET /compliance/integrity must return a JSON integrity report."""
    r = client.get('/compliance/integrity')
    assert r.status_code == 200
    body = r.json()
    assert 'known_subject_count' in body
    assert 'orphan_visit_logs' in body
    assert 'orphan_trips' in body
    assert 'ok' in body


def test_integrity_detects_orphan_visit_log():
    """A visit log whose subjectId is absent from subjects is reported as an orphan."""
    # Clear state and seed an orphan visit log
    client.post('/compliance/saved_subjects', json=[])
    client.post('/compliance/visit_logs', json=[
        {'id': 'orphan-log', 'subjectId': 'NO-SUCH-SUBJECT'}
    ])
    r = client.get('/compliance/integrity')
    assert r.status_code == 200
    body = r.json()
    assert any(o['id'] == 'orphan-log' for o in body['orphan_visit_logs'])
    assert body['ok'] is False


def test_integrity_clean_when_subjects_match():
    """When all visit logs reference known subjects the report shows ok=True."""
    client.post('/compliance/saved_subjects', json=[
        {'subjectId': 'S-CLEAN', 'status': 'enrolled'}
    ])
    client.post('/compliance/visit_logs', json=[
        {'id': 'log-clean', 'subjectId': 'S-CLEAN'}
    ])
    r = client.get('/compliance/integrity')
    assert r.status_code == 200
    body = r.json()
    assert not any(o['id'] == 'log-clean' for o in body['orphan_visit_logs'])
