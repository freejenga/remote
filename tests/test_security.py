"""Tests for the security layer: de-identification, auth gate, memory hygiene."""
import os
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

os.environ['PLATFORM_DB'] = os.path.join(tempfile.gettempdir(), f'sec_{uuid.uuid4().hex}.db')

from app import deid, chat_memory  # noqa: E402
from app.store import init_db, get_conn  # noqa: E402

init_db()


# --- De-identification -----------------------------------------------------
def test_redact_obj_strips_identifiers_keeps_codes():
    record = {
        "subjectId": "SUBJ-0012",        # code -> kept
        "name": "Jane Doe",              # identifier -> redacted
        "dob": "1980-04-01",             # identifier -> redacted
        "status": "enrolled",            # clinical -> kept
        "trips": [
            {"pickup": "123 Main St", "dropoff": "710 N Euclid St",
             "amount": 130.5, "miles": 10},
        ],
    }
    out = deid.redact_obj(record)
    assert out["subjectId"] == "SUBJ-0012"
    assert out["status"] == "enrolled"
    assert out["name"] == deid.REDACTED
    assert out["dob"] == deid.REDACTED
    assert out["trips"][0]["pickup"] == deid.REDACTED
    assert out["trips"][0]["dropoff"] == deid.REDACTED
    assert out["trips"][0]["amount"] == 130.5  # clinical/financial kept


def test_scrub_text_redacts_contact_patterns():
    s = "Reach Jane at jane@example.com or 415-555-0199, SSN 123-45-6789."
    out = deid.scrub_text(s)
    assert "jane@example.com" not in out
    assert "123-45-6789" not in out
    assert "415-555-0199" not in out


def test_deid_can_be_disabled(monkeypatch):
    monkeypatch.setenv("CHAT_DEIDENTIFY", "0")
    assert deid.redact_obj({"name": "Jane"}) == {"name": "Jane"}


# --- Memory hygiene --------------------------------------------------------
def test_learning_is_scrubbed_before_store():
    rec = chat_memory.save_learning("email me at leak@example.com")
    stored = [m["content"] for m in chat_memory.recall(query="email")]
    assert any("[REDACTED:email]" in c for c in stored)
    assert all("leak@example.com" not in c for c in stored)
    chat_memory.forget(rec["id"])


def test_conversations_not_persisted_by_default():
    chat_memory.save_turn("user", "patient John Smith asked about visit", subject=None)
    with get_conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) c FROM chat_memory WHERE kind LIKE 'conversation%'"
        ).fetchone()["c"]
    assert n == 0  # off unless CHAT_LOG_CONVERSATIONS=1


def test_conversations_persisted_and_scrubbed_when_enabled(monkeypatch):
    monkeypatch.setenv("CHAT_LOG_CONVERSATIONS", "1")
    chat_memory.save_turn("user", "call me at 415-555-0199", subject=None)
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT content FROM chat_memory WHERE kind LIKE 'conversation%'"
        ).fetchall()
    assert rows and all("415-555-0199" not in r["content"] for r in rows)


# --- Auth gate -------------------------------------------------------------
def _client():
    from fastapi.testclient import TestClient
    from app.api import app
    return TestClient(app)


def test_open_when_no_token(monkeypatch):
    monkeypatch.delenv("PLATFORM_AUTH_TOKEN", raising=False)
    c = _client()
    assert c.get("/compliance/saved_subjects").status_code == 200
    assert c.get("/").json()["security"]["auth_required"] is False


def test_gated_when_token_set(monkeypatch):
    monkeypatch.setenv("PLATFORM_AUTH_TOKEN", "s3cret")
    c = _client()
    # No token -> 401
    assert c.get("/compliance/saved_subjects").status_code == 401
    # Wrong token -> 401
    bad = c.post("/auth/login", data={"token": "nope"})
    assert bad.status_code == 401
    # Correct token -> cookie set, subsequent calls allowed
    ok = c.post("/auth/login", data={"token": "s3cret"})
    assert ok.status_code == 200
    assert c.get("/compliance/saved_subjects").status_code == 200
    assert c.get("/").json()["security"]["auth_required"] is True


def test_bearer_token_accepted(monkeypatch):
    monkeypatch.setenv("PLATFORM_AUTH_TOKEN", "s3cret")
    c = _client()
    r = c.get("/dispatch/trips", headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200
