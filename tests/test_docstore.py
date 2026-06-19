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


# --- Workstream 1: table-aware + section-aware + hybrid ---------------------
def test_schema_v5_columns_present():
    """Migration v5 adds doc_chunks.section and the doc_vectors table."""
    from app.store import get_conn
    with get_conn() as conn:
        cols = {r[1] for r in conn.execute('PRAGMA table_info(doc_chunks)').fetchall()}
        assert 'section' in cols
        names = {r['name'] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert 'doc_vectors' in names


def test_table_block_stays_intact_through_chunking():
    """A tagged Schedule-of-Events table is kept whole as one chunk."""
    from app.ingestion import TABLE_OPEN, TABLE_CLOSE
    rows = "\n".join(f"Visit{i}\tProcedure{i}\tDay {i}" for i in range(30))
    text = (f"Schedule of Events\n{TABLE_OPEN}\n{rows}\n{TABLE_CLOSE}\n"
            "Some trailing narrative about the study.")
    segments = docstore._segment_document(text)
    table_chunks = [s for s in segments if "Visit0\t" in s["content"]]
    assert len(table_chunks) == 1, "table must be a single atomic chunk"
    # The whole matrix survived in that one chunk.
    assert "Visit29" in table_chunks[0]["content"]
    assert table_chunks[0]["section"] == "Schedule of Events"


def test_section_labels_populated():
    text = (
        "Inclusion Criteria\n"
        "Adults aged 18 years or older with confirmed Type 2 diabetes mellitus.\n"
        "Exclusion Criteria\n"
        "Pregnant or breastfeeding women are not eligible to participate.\n"
    )
    segments = docstore._segment_document(text)
    sections = {s["section"] for s in segments}
    assert "Inclusion Criteria" in sections
    assert "Exclusion Criteria" in sections


def test_inclusion_exclusion_retrieval_hits_right_section():
    rec = docstore.add_document(
        "Eligibility protocol",
        "Inclusion Criteria\n"
        "Adults aged 18 years or older with confirmed Type 2 diabetes mellitus "
        "and a body mass index between 25 and 40.\n"
        "Exclusion Criteria\n"
        "History of pancreatitis, or current use of insulin, excludes the subject.\n")
    hits = docstore.search("which patients can be enrolled with diabetes?", limit=3)
    assert hits
    top = hits[0]
    assert top["doc_id"] == rec["id"]
    # The matching passage should be the inclusion section, not exclusion.
    assert top["section"] == "Inclusion Criteria"
    assert "section" in top  # hit shape carries the section label


def test_search_works_with_embeddings_disabled_by_default():
    """Default install path is lexical-only: embeddings opt-in and off here."""
    from app import embeddings
    assert embeddings.enabled() is False
    assert embeddings.available() is False
    docstore.add_document("Vitals SOP",
                          "Record blood pressure and heart rate at every visit.")
    hits = docstore.search("blood pressure measurement", limit=2)
    assert hits and hits[0]["score"] > 0


def _fake_vector(text, dim=24):
    """Deterministic hashed bag-of-tokens vector — stands in for a real model."""
    vec = [0.0] * dim
    for tok in docstore._tokens(text):
        vec[hash(tok) % dim] += 1.0
    return vec


def test_hybrid_path_runs_with_a_fake_embedder(monkeypatch):
    """Exercise the semantic-fusion branch without loading torch/a real model."""
    from app import embeddings
    monkeypatch.setattr(embeddings, "available", lambda: True)
    monkeypatch.setattr(embeddings, "embed",
                        lambda texts: [_fake_vector(t) for t in texts])
    monkeypatch.setattr(embeddings, "embed_one", lambda t: _fake_vector(t))

    # add_document now stores vectors (via the fake); search fuses lexical+semantic.
    rec = docstore.add_document(
        "Hybrid doc",
        "Pharmacokinetic sampling is collected at multiple timepoints post dose.")
    docstore.add_document("Distractor", "Unrelated content about site monitoring.")

    hits = docstore.search("when are PK samples drawn", limit=3)
    assert hits, "hybrid retrieval returned nothing"
    assert any(h["doc_id"] == rec["id"] for h in hits)
    # Vectors were actually persisted for the fused ranking.
    vecs = docstore._load_vectors()
    assert vecs, "expected stored embedding vectors"
