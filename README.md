# Clinical Research Platform

A unified FastAPI app combining three modules around a shared study-subject key:

1. **Protocol agent** — turns clinical trial protocols into visit schedules,
   operational rows, SoA-vs-narrative conflict reports, and CTMS-ready exports.
2. **Compliance tracker** — subjects, visit logs, and study date (ported from
   the standalone compliance app).
3. **Serengeti dispatch** — non-emergency medical transport trips with the
   original pricing engine and invoice/receipt summaries.

The three modules tie together through the shared `subject` / `voucher` /
`subjectId` identifier, so a study subject's protocol visits, compliance status,
and transport trips line up.

## Modules & endpoints
| Module     | Prefix        | Key endpoints |
|------------|---------------|----------------|
| Protocol   | `/`           | `POST /parse-text`, `POST /parse-file`, `POST /export-file` |
| Compliance | `/compliance` | `GET/POST /saved_subjects`, `/visit_logs`, `/app_date`, `GET /integrity` |
| Dispatch   | `/dispatch`   | `GET /rates`, `POST /quote`, `GET/POST /trips`, `DELETE /trips/{id}`, `GET /invoice` |
| AI chat    | `/chat`       | `POST /chat/` (non-streaming), `POST /chat/stream` (SSE) |
| Auth/RBAC  | `/auth`, `/rbac` | token gate (`/auth/login`) + user accounts & roles (`/rbac/login`, `/rbac/me`, `/rbac/users`) |
| Audit      | `/audit`      | `GET /audit/entries`, `GET /audit/verify` (tamper-evident log) |

HTML UIs are served at `/ui/compliance.html`, `/ui/dispatch.html`, `/ui/chat.html`,
and `/ui/login.html`; all pages carry a floating AI-chat widget. Persistence uses
a shared SQLite file (`clinical_platform.db`, override with `PLATFORM_DB`), with
schema migrations tracked via `PRAGMA user_version`.

## Features
- Upload protocol text/TXT/PDF/DOCX (with optional OCR fallback for scanned PDFs)
- Parse Schedule of Activities (SoA) tables — multi-row headers, varied marks,
  lettered/dagger footnotes; empty cells preserved (correct column alignment)
- Parse narrative visit descriptions (multiple phrasings)
- Detect conflicts between SoA and narrative sources
- Expand cycle patterns and order visits chronologically
- Review and edit in a Streamlit UI **with an embedded, protocol-aware AI chat**
- Export operational rows to CSV/XLSX
- Serve a JSON API via FastAPI; `langgraph` is optional (linear fallback runner)
- **AI assistant** with tool-based data access, persistent learning, streaming
  replies, and answer provenance
- **Security**: de-identification, token + RBAC auth, encryption at rest,
  tamper-evident audit trail, memory hygiene (see Data security below)

## Quick start
```bash
pip install -r requirements.txt
python main.py
```

API docs: http://localhost:8000/docs (all three modules)
Module index: http://localhost:8000/

Run protocol review UI (with the embedded AI assistant):
```bash
streamlit run app/ui.py
```
The protocol page has a built-in AI chat that can see the protocol you've
parsed (answers about its visits/activities/conflicts directly) and can also
look up subjects, compliance, and trips, and remember preferences across
sessions. It needs `ANTHROPIC_API_KEY` set in the environment; without it the
chat shows a configuration notice and the rest of the page works normally.
Model defaults to `claude-haiku-4-5` (override with `CHAT_MODEL`).

On Windows (PowerShell), the same commands work:
```powershell
pip install -r requirements.txt
python main.py            # API on http://localhost:8000
streamlit run app/ui.py   # UI
```

## Data security

Configurable via environment variables (all default-safe):

| Variable | Default | Effect |
|----------|---------|--------|
| `CHAT_DEIDENTIFY` | `1` (on) | Redacts direct identifiers (names, DOB, addresses, email/phone/SSN) from everything sent to the LLM. The AI reasons over coded subject IDs + clinical facts, so identities never leave the machine. Set `0` only if you have a provider BAA + zero-retention. |
| `PLATFORM_AUTH_TOKEN` | unset (open) | When set, every data endpoint (`/compliance`, `/dispatch`, `/chat`, protocol parse/export) requires the token. Log in once at `/ui/login.html` (sets an HttpOnly cookie) or send `Authorization: Bearer <token>`. The Streamlit page prompts for the same token. |
| `CHAT_LOG_CONVERSATIONS` | `0` (off) | When `1`, conversation turns are persisted (scrubbed of identifiers). Off by default so chat history doesn't accumulate PHI. Learnings are always saved (also scrubbed). |
| `PLATFORM_ENCRYPTION_KEY` | unset (plaintext) | When set, sensitive JSON blobs (subjects, visit logs, trips, chat memory) are encrypted at rest with Fernet before they hit the `.db` file, so a copied database is unreadable without the key. Reads decrypt transparently. Any passphrase works (derived via SHA-256). |
| `RBAC_ADMIN_USER` / `RBAC_ADMIN_PASSWORD` | unset | If set (and no users exist yet), seeds an initial admin account. Users log in at `/rbac/login`; roles are `viewer` < `coordinator` < `admin`. A valid RBAC session also satisfies the token gate, and the logged-in username becomes the **actor** in the audit trail. |

**Audit trail.** Every data mutation, export, and chat query is recorded in an
append-only, hash-chained `audit_log` (`app/audit.py`). Each entry commits the
previous entry's hash, so any later edit/deletion breaks the chain.
- `GET /audit/entries?limit=N` — recent entries (who/what/when, code-level detail only).
- `GET /audit/verify` — recompute the chain; returns `{ok, count, broken_at}`.
Details are kept code-level (IDs, counts, subject codes, amounts) so the audit
log doesn't itself become a PHI store.

Architecture notes: the chat uses **tool-based least privilege** (it fetches only
the rows a question needs, never a bulk dump) and a **de-identification boundary**
(`app/deid.py`) so identifiers are stripped before transmission. Anything sent to
a cloud model still leaves your machine — for real PHI, pair de-identification
with an Anthropic BAA + zero-retention, or run a local model.

Example (secured) run:
```powershell
$env:CHAT_DEIDENTIFY = "1"
$env:PLATFORM_AUTH_TOKEN = "choose-a-strong-token"
$env:ANTHROPIC_API_KEY = "sk-ant-..."
python main.py    # then sign in at http://localhost:8000/ui/login.html
```

## Tests
```bash
pytest -q
```
The suite (**125 tests**) covers SoA parsing (multi-row headers, footnotes,
chronological ordering, langgraph-optional fallback), the unified platform
(compliance + dispatch pricing, schema migrations, validation, integrity),
the AI chat (tools, learning memory, streaming, provenance), RBAC (accounts,
roles, sessions), and the security layer (de-identification, auth gate,
encryption at rest, tamper-evident audit, memory hygiene).

### Optional OCR
For scanned PDFs, install the extras and the Tesseract binary:
```bash
pip install -r requirements-ocr.txt   # pytesseract, pdf2image, Pillow
```
Without them, scanned-PDF parsing degrades silently (returns no text) rather
than erroring.

### Docker / CI
A `Dockerfile` runs the API (`uvicorn app.api:app` on port 8000) and
`.github/workflows/ci.yml` runs `pytest` across Python 3.11–3.13 on every push.

## Notes
- This is a starter product, not validated GxP software.
- Complex scanned PDFs may need OCR before parsing.
- Complex SoA layouts and nuanced footnotes are only partly handled.
