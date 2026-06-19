"""Tests for encryption at rest and the tamper-evident audit trail."""
import os
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

os.environ['PLATFORM_DB'] = os.path.join(tempfile.gettempdir(), f'encaud_{uuid.uuid4().hex}.db')

from app import crypto, audit  # noqa: E402
from app.store import init_db, get_conn, pack, unpack  # noqa: E402

init_db()


def teardown_module(module):
    # Remove any encrypted rows this module wrote so later test files (which read
    # without the key) don't hit 'enc:' blobs. The shared store may be reused.
    with get_conn() as conn:
        conn.execute("DELETE FROM subjects")
        conn.execute("DELETE FROM visit_logs")
        conn.execute("DELETE FROM trips")
        conn.execute("DELETE FROM chat_memory")


# --- Encryption at rest ----------------------------------------------------
def test_pack_is_plaintext_without_key(monkeypatch):
    monkeypatch.delenv('PLATFORM_ENCRYPTION_KEY', raising=False)
    blob = pack({'a': 1})
    assert blob == '{"a": 1}'
    assert unpack(blob) == {'a': 1}


def test_pack_encrypts_with_key(monkeypatch):
    monkeypatch.setenv('PLATFORM_ENCRYPTION_KEY', 'a-strong-passphrase')
    blob = pack({'name': 'Jane Doe', 'subjectId': 'SUBJ-0012'})
    assert blob.startswith('enc:')
    assert 'Jane Doe' not in blob            # plaintext not present on disk
    assert 'SUBJ-0012' not in blob
    assert unpack(blob) == {'name': 'Jane Doe', 'subjectId': 'SUBJ-0012'}


def test_decrypt_without_key_raises(monkeypatch):
    monkeypatch.setenv('PLATFORM_ENCRYPTION_KEY', 'k1')
    token = crypto.encrypt('secret')
    monkeypatch.delenv('PLATFORM_ENCRYPTION_KEY', raising=False)
    try:
        crypto.decrypt(token)
        assert False, 'expected RuntimeError'
    except RuntimeError:
        pass


def test_db_blob_is_encrypted_end_to_end(monkeypatch):
    monkeypatch.setenv('PLATFORM_ENCRYPTION_KEY', 'study-key')
    from fastapi.testclient import TestClient
    from app.api import app
    c = TestClient(app)
    c.post('/compliance/saved_subjects',
           json=[{'subjectId': 'SUBJ-9001', 'name': 'Secret Patient'}])
    # Raw column on disk must not contain the plaintext name.
    with get_conn() as conn:
        raw = conn.execute(
            "SELECT data FROM subjects WHERE subjectId='SUBJ-9001'").fetchone()['data']
    assert raw.startswith('enc:')
    assert 'Secret Patient' not in raw
    # But the API decrypts transparently on read.
    got = c.get('/compliance/saved_subjects').json()['data']
    assert any(s.get('name') == 'Secret Patient' for s in got)


# --- Audit trail -----------------------------------------------------------
def test_audit_chain_and_verify():
    audit.record('test.alpha', {'n': 1})
    audit.record('test.beta', {'n': 2})
    v = audit.verify()
    assert v['ok'] is True and v['count'] >= 2
    actions = [e['action'] for e in audit.entries(50)]
    assert 'test.alpha' in actions and 'test.beta' in actions


def test_audit_detects_tampering():
    rec = audit.record('test.tamper', {'x': 'original'})
    with get_conn() as conn:
        conn.execute("UPDATE audit_log SET detail = ? WHERE id = ?",
                     ('{"x": "altered"}', rec['id']))
    v = audit.verify()
    assert v['ok'] is False
    assert v['broken_at'] == rec['id']


def test_data_mutation_is_audited(monkeypatch):
    monkeypatch.delenv('PLATFORM_ENCRYPTION_KEY', raising=False)
    # Rebuild a clean audit baseline check via the API.
    from fastapi.testclient import TestClient
    from app.api import app
    c = TestClient(app)
    c.post('/dispatch/trips', json={
        'type': 'Sedan', 'roundtrip': False, 'subject': 'SUBJ-7', 'voucher': 'V',
        'date': '2026-06-01', 'pickup': 'A', 'dropoff': 'B', 'miles': 5, 'wait': 0,
        'amount': 50,
    })
    actions = [e['action'] for e in audit.entries(50)]
    assert 'dispatch.create_trip' in actions
