"""Tests for the persistent multi-study + protocol-version workflow."""
import os
import tempfile

# Isolated throwaway DB before any app import.
os.environ['PLATFORM_DB'] = os.path.join(tempfile.gettempdir(), 'studies_test.db')
if os.path.exists(os.environ['PLATFORM_DB']):
    os.remove(os.environ['PLATFORM_DB'])

from fastapi.testclient import TestClient

from app.api import app

client = TestClient(app)

SAMPLE = open(
    os.path.join(os.path.dirname(__file__), '..', 'sample_protocol.txt'),
    encoding='utf-8',
).read()


def test_schema_version_is_current():
    from app.store import get_conn, _SCHEMA_VERSION
    assert _SCHEMA_VERSION == 6
    with get_conn() as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 6


def test_studies_tables_exist():
    from app.store import get_conn
    with get_conn() as conn:
        names = {r['name'] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {'studies', 'protocol_versions'} <= names


def test_create_and_list_study():
    r = client.post('/studies', json={'name': 'ABC-101', 'sponsor': 'ExampleBio'})
    assert r.status_code == 200
    sid = r.json()['id']
    assert r.json()['version_count'] == 0

    listed = client.get('/studies').json()['data']
    assert any(s['id'] == sid and s['name'] == 'ABC-101' for s in listed)


def test_create_study_empty_name_rejected():
    assert client.post('/studies', json={'name': '   '}).status_code == 422


def test_version_lifecycle_parses_and_persists():
    sid = client.post('/studies', json={'name': 'XYZ-200'}).json()['id']

    # Create a version -> parses the protocol and stores the schedule.
    cv = client.post(f'/studies/{sid}/versions',
                     json={'label': 'v1', 'protocol_text': SAMPLE})
    assert cv.status_code == 200
    vid = cv.json()['id']
    assert cv.json()['status'] == 'draft'

    # Study now reports one version with a conflict count.
    study = client.get(f'/studies/{sid}').json()
    assert len(study['versions']) == 1
    assert 'conflict_count' in study['versions'][0]

    # Full version fetch returns the persisted parsed schedule.
    full = client.get(f'/studies/{sid}/versions/{vid}').json()
    assert full['schedule']['flat_rows'], 'parsed schedule must persist'
    assert {v['name'] for v in full['schedule']['visits']} >= {'Screening'}


def test_version_empty_text_rejected():
    sid = client.post('/studies', json={'name': 'EMPTY-1'}).json()['id']
    r = client.post(f'/studies/{sid}/versions', json={'label': 'v1', 'protocol_text': ''})
    assert r.status_code == 422


def test_update_version_status():
    sid = client.post('/studies', json={'name': 'REV-1'}).json()['id']
    vid = client.post(f'/studies/{sid}/versions',
                      json={'label': 'v1', 'protocol_text': SAMPLE}).json()['id']

    upd = client.patch(f'/studies/{sid}/versions/{vid}',
                       json={'status': 'in_review', 'reviewer': 'dr.smith@example.com'})
    assert upd.status_code == 200
    assert upd.json()['status'] == 'in_review'

    assert client.patch(f'/studies/{sid}/versions/{vid}',
                        json={'status': 'bogus'}).status_code == 422


def test_delete_version_and_study():
    sid = client.post('/studies', json={'name': 'DEL-1'}).json()['id']
    vid = client.post(f'/studies/{sid}/versions',
                      json={'label': 'v1', 'protocol_text': SAMPLE}).json()['id']

    assert client.delete(f'/studies/{sid}/versions/{vid}').json()['success']
    assert client.get(f'/studies/{sid}').json()['versions'] == []

    assert client.delete(f'/studies/{sid}').json()['success']
    assert client.get(f'/studies/{sid}').status_code == 404


def test_unknown_study_404():
    assert client.get('/studies/nope').status_code == 404
    assert client.post('/studies/nope/versions',
                       json={'label': 'v1', 'protocol_text': SAMPLE}).status_code == 404


def test_study_mutations_are_audited():
    from app import audit
    sid = client.post('/studies', json={'name': 'AUD-1'}).json()['id']
    client.post(f'/studies/{sid}/versions', json={'label': 'v1', 'protocol_text': SAMPLE})
    actions = [e['action'] for e in audit.entries(100)]
    assert 'studies.create' in actions
    assert 'studies.create_version' in actions
