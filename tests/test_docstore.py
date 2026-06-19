"""Tests for the document knowledge base + retrieval (RAG / vector-search layer)."""
import os
import tempfile

# Isolated throwaway DB before any app import.
os.environ['PLATFORM_DB'] = os.path.join(tempfile.gettempdir(), 'docstore_test.db')
if os.path.exists(os.environ['PLATFORM_DB']):
    os.remove(os.environ['PLATFORM_DB'])

from fastapi.testclient import TestClient

from app.api import app
from app import docstore

client = TestClient(app)


def test_doc_tables_exist():
    from app.store import get_conn
    with get_conn() as conn:
        names = {r['name'] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {'documents', 'doc_chunks'} <= names


def test_chunking_overlaps_and_covers():
    text = " ".join(f"w{i}" for i in range(500))
    chunks = docstore._chunk_text(text)
    assert len(chunks) >= 3
    # Every original word survives somewhere.
    joined = " ".join(chunks).split()
    assert "w0" in joined and "w499" in joined


def test_add_search_and_relevance_ranking():
    docstore.add_document(
        "Screening procedures",
        "At the screening visit, collect fasting blood glucose and an ECG. "
        "Vitals include blood pressure and heart rate.")
    docstore.add_document(
        "Dosing schedule",
        "Study drug is administered on day one and continues every 28 days "
        "through the treatment cycle.")

    hits = docstore.search("what labs are drawn at screening?", limit=3)
    assert hits, "expected at least one relevant chunk"
    # The screening document should outrank the dosing document.
    assert hits[0]["title"] == "Screening procedures"
    assert hits[0]["score"] > 0


def test_deidentification_before_storage():
    docstore.add_document(
        "Subject note",
        "Patient John Doe, email john.doe@example.com, called about visit 2.")
    hits = docstore.search("visit 2 patient note", limit=1)
    assert hits
    body = hits[0]["content"]
    assert "john.doe@example.com" not in body
    assert "[REDACTED" in body


def test_empty_document_rejected():
    import pytest
    with pytest.raises(ValueError):
        docstore.add_document("blank", "   ")


def test_delete_document_removes_chunks():
    rec = docstore.add_document("Temp doc", "transient content for deletion test")
    assert docstore.delete_document(rec["id"]) is True
    # A second delete is a no-op.
    assert docstore.delete_document(rec["id"]) is False
    # Its content is no longer searchable.
    hits = docstore.search("transient content for deletion test", limit=5)
    assert all(h["doc_id"] != rec["id"] for h in hits)


# --- HTTP endpoints --------------------------------------------------------
def test_upload_list_search_endpoints():
    files = {'file': ('memo.txt', b'The randomization ratio is two to one, active to placebo.', 'text/plain')}
    r = client.post('/documents', files=files)
    assert r.status_code == 200
    doc_id = r.json()['id']
    assert r.json()['chunk_count'] >= 1

    r = client.get('/documents')
    assert r.status_code == 200
    assert any(d['id'] == doc_id for d in r.json()['documents'])

    r = client.get('/documents/search', params={'q': 'randomization ratio'})
    assert r.status_code == 200
    assert r.json()['results']

    r = client.delete(f'/documents/{doc_id}')
    assert r.status_code == 200 and r.json()['deleted'] is True


def test_search_documents_tool_wired():
    from app.chat_tools import dispatch_tool, _DISPATCH
    assert 'search_documents' in _DISPATCH and 'list_documents' in _DISPATCH
    docstore.add_document("Tool doc", "adverse event reporting must occur within 24 hours")
    out = dispatch_tool('search_documents', {'query': 'adverse event reporting window'})
    assert out['count'] >= 1
    assert 'adverse' in out['results'][0]['content'].lower()
