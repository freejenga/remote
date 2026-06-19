# Clinical Research Platform

A unified FastAPI app combining three core modules around a shared study-subject key:

1. **Protocol agent** — turns clinical trial protocols into visit schedules,
   operational rows, SoA-vs-narrative conflict reports, and CTMS-ready exports.
2. **Compliance tracker** — subjects, visit logs, and study date (ported from
   the standalone compliance app).
3. **Serengeti dispatch** — non-emergency medical transport trips with the
   original pricing engine and invoice/receipt summaries.

The modules tie together through the shared `subject` / `voucher` /
`subjectId` identifier, so a study subject's protocol visits, compliance status,
and transport trips line up.

Layered on top are four additional capabilities that turn a parsed protocol into
an operable trial: a **document knowledge base (RAG)** for grounded Q&A, a
**deterministic scheduling engine** (visit windows + compliance, no AI in the
time math), an **agentic source-document generator** (generation → formatting →
critic, layered on a deterministic skeleton), and a **financial reconciliation**
layer (Schedule-of-Events activities ↔ study budget).

## Modules & endpoints
| Module     | Prefix        | Key endpoints |
|------------|---------------|----------------|
| Protocol   | `/`           | `POST /parse-text`, `POST /parse-file`, `POST /export-file`, `POST /export-schedule` |
| Studies    | `/studies`    | study/version registry; `POST /studies`, `/{id}/versions[/upload]`, version review + export |
| Scheduling | `/scheduling` | `POST /studies/{id}/soe` (materialize SoE), `/subjects/{id}/calendar`, `PATCH /visits/{id}/actual`, `GET /compliance`, `/next-due` |
| Documents (RAG) | `/documents` | `POST /documents` (upload/index), `GET /documents/search`, `GET /documents`, `DELETE /documents/{id}` |
| Source docs | `/source-documents` | `POST /source-documents` (deterministic packet), `POST /source-documents/generate` (AI generation→critic) |
| Billing    | `/billing`    | `POST /studies/{id}/budget[/upload]`, `POST /map/auto`, `PUT /map`, `GET /reconcile`, `GET /reconcile/export` |
| Compliance | `/compliance` | `GET/POST /saved_subjects`, `/visit_logs`, `/app_date`, `GET /integrity` |
| Dispatch   | `/dispatch`   | `GET /rates`, `POST /quote`, `GET/POST /trips`, `DELETE /trips/{id}`, `GET /invoice` |
| AI chat    | `/chat`       | `POST /chat/` (non-streaming), `POST /chat/stream` (SSE) |
| Auth/RBAC  | `/auth`, `/rbac` | token gate (`/auth/login`) + user accounts & roles (`/rbac/login`, `/rbac/me`, `/rbac/users`) |
| Audit      | `/audit`      | `GET /audit/entries`, `GET /audit/verify` (tamper-evident log) |

HTML UIs are served at `/ui/compliance.html`, `/ui/dispatch.html`, `/ui/chat.html`,
and `/ui/login.html`; all pages carry a floating AI-chat widget. Persistence uses
a shared SQLite file (`clinical_platform.db`, override with `PLATFORM_DB`), with
schema migrations tracked via `PRAGMA user_version`. An **optional PostgreSQL
backend** can be selected with `PLATFORM_DB_URL` (see Data security below).

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
- **Document knowledge base (RAG)** — upload protocols/SOPs/memos; structure-aware
  PDF table extraction (pdfplumber), section-aware chunking (tables kept intact),
  and TF-IDF retrieval with optional local semantic embeddings (hybrid RRF). The
  AI assistant can search it via `search_documents`.
- **Deterministic scheduling** — materialize a protocol's Schedule of Events,
  generate per-subject visit calendars, and compute visit windows + compliance
  (on-time/early/late/upcoming/due/missed) and the next-due visit — **no LLM in
  the time math**, so results are reproducible.
- **Agentic source-document generation** — a generation → formatting → critic
  loop layered on a deterministic assembler skeleton; the critic re-checks the
  draft against the protocol and revises until aligned.
- **Financial reconciliation** — map Schedule-of-Events activities to study
  budget items, reconcile completed/missed/pending visits, and export an
  accounting-ready CSV/XLSX.
- **Security**: de-identification, token + RBAC auth, encryption at rest,
  tamper-evident audit trail, memory hygiene (see Data security below)

## Quick start
```bash
pip install -r requirements.txt
python main.py
```

API docs: http://localhost:8000/docs (all three modules)
Module index: http://localhost:8000/

Run the Streamlit UI:
```bash
streamlit run app/ui.py
```
The UI has four tabs:
1. **Protocol & Chat** — upload/parse a protocol, review visits/conflicts/rows,
   export, and a built-in AI chat that sees the parsed protocol and can search
   uploaded documents and look up subjects/compliance/trips.
2. **Documents (RAG)** — index documents and run de-identified semantic search.
3. **Source Documents** — download the deterministic fillable packet, or run the
   AI generation/critic pipeline (per-visit approval badges).
4. **Scheduling & Billing** — save a parsed protocol as a study, materialize the
   SoE, build a subject calendar, view compliance + next-due, and manage the
   budget + run/export reconciliation.

The AI features need `ANTHROPIC_API_KEY` set in the environment; without it the
chat and AI source-doc generation show a configuration notice and everything
deterministic (parsing, scheduling, the source-doc packet, billing) works
normally. Chat model defaults to `claude-haiku-4-5` (override with `CHAT_MODEL`).

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
| `ANTHROPIC_API_KEY` | unset | Enables the AI chat and AI source-document generation. Without it, those features return a clear configuration notice and all deterministic features still work. Required for `/chat` and `POST /source-documents/generate`. |
| `CHAT_MODEL` | `claude-haiku-4-5` | Anthropic model used by the chat + agent pipeline. |
| `PLATFORM_EMBEDDINGS` | `0` (off) | When `1`, document retrieval becomes **hybrid** (TF-IDF + local semantic embeddings, fused via reciprocal-rank). Requires `requirements-embeddings.txt`; the model runs locally (nothing leaves the box). Off → TF-IDF only. |
| `PLATFORM_EMBED_MODEL` | `BAAI/bge-small-en-v1.5` | Local sentence-transformer model used when embeddings are enabled. |
| `PLATFORM_DB_URL` | unset (SQLite) | When set to `postgresql://user:pw@host/db`, the shared store uses **PostgreSQL** instead of the SQLite file (requires `requirements-postgres.txt`). Unset → SQLite (`PLATFORM_DB`). |

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
The suite (**192 tests**) covers SoA parsing (multi-row headers, footnotes,
chronological ordering, langgraph-optional fallback), the unified platform
(compliance + dispatch pricing, schema migrations, validation, integrity),
the AI chat (tools, learning memory, streaming, provenance), RBAC (accounts,
roles, sessions), the security layer (de-identification, auth gate, encryption
at rest, tamper-evident audit, memory hygiene), the document knowledge base
(table-aware chunking, section labels, hybrid retrieval), deterministic
scheduling (window parsing/math, compliance status, next-due), the agentic
source-doc pipeline (critic reject→revise→pass, fallbacks, offline via a stubbed
LLM), financial reconciliation, and the Postgres dialect adapter.

> One test (`tests/test_pipeline.py::test_ingestion_ocr_fallback_called_on_empty_pdf`)
> can fail on Windows with a `tmp_path` permission error owned by a different OS
> user — a pre-existing environment issue, not a code defect. Deselect it:
> `pytest -q --deselect "tests/test_pipeline.py::test_ingestion_ocr_fallback_called_on_empty_pdf"`.

### Optional extras
```bash
pip install -r requirements-ocr.txt        # scanned-PDF OCR (needs Tesseract binary)
pip install -r requirements-embeddings.txt # local semantic search (PLATFORM_EMBEDDINGS=1)
pip install -r requirements-postgres.txt   # PostgreSQL backend (PLATFORM_DB_URL=postgresql://…)
```
Each is optional: without OCR, scanned-PDF parsing degrades silently (returns no
text); without embeddings, retrieval is TF-IDF only; without psycopg, the app
uses SQLite. None of these block the default install.

### Docker / CI
A `Dockerfile` runs the API (`uvicorn app.api:app` on port 8000) and
`.github/workflows/ci.yml` runs `pytest` across Python 3.11–3.13 on every push.

## Notes
- This is a starter product, not validated GxP software.
- Complex scanned PDFs may need OCR before parsing.
- Complex SoA layouts and nuanced footnotes are only partly handled.

## Developer setup (quick)
These steps get a developer up and running locally (venv, dev deps, linters, tests, frontend).

### Prereqs
- Python 3.12+ (3.12 recommended)
- Node.js 18+ (for the web UI)
- git

### Windows (PowerShell)
```powershell
# create & activate venv
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install -r requirements-dev.txt
# install pre-commit and enable hooks
pip install pre-commit
pre-commit install
# frontend
cd web
npm ci
cd ..
```

### macOS / Linux
```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install -r requirements-dev.txt
pip install pre-commit
pre-commit install
cd web
npm ci
cd ..
```

### Common commands
- Format & lint (Python): `./format.ps1` (Windows) or `./format.sh` (Unix)
- Lint & fix (web): `npm run lint --prefix web` and `npm run lint:fix --prefix web`
- Run Python tests: `pytest -q`
- Run frontend tests: `npm test --prefix web`
- Run API: `python main.py` (then visit http://localhost:8000/docs)
- Run UI: `streamlit run app/ui.py`
- Generate coverage locally: `pytest --cov=./ --cov-report=xml:coverage.xml --cov-report=html:htmlcov`

### Git & CI
- Add remote & push:
  `git remote add origin https://github.com/<owner>/<repo>.git && git push -u origin master`
- CI: `.github/workflows/ci.yml` runs format/lint/tests and uploads coverage artifacts on push.

### Notes for contributors
- Run `pre-commit run --all-files` before committing; hooks enforce ruff/isort/black for Python and eslint/prettier for web when applicable.
- If network-restricted, run as much locally as possible; CI will still run on push and publish coverage artifacts.

If you'd like, add a CONTRIBUTING.md or a small developer checklist; happy to scaffold one.
