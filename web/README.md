# Clinical Research Platform — Web UI (Next.js)

A single, cohesive front-end for the platform's four modules (subjects/compliance,
dispatch, protocol parsing, AI assistant) plus RBAC login — talking to the
FastAPI backend in `../app`.

## How it connects
`next.config.mjs` proxies `/api/*` to the FastAPI backend (default
`http://localhost:8000`, override with `BACKEND_ORIGIN`). Routing API calls
through the same origin keeps the HttpOnly `SameSite=strict` session cookie
working — login at `/login` sets it and every later request carries it.

## Run (dev)
```bash
# 1. Start the backend (from ../)
python main.py            # http://localhost:8000

# 2. Start the web UI (from web/)
npm install
npm run dev               # http://localhost:3000
```
If the backend has `PLATFORM_AUTH_TOKEN`/RBAC enabled, sign in at
`http://localhost:3000/login` (use an account seeded via
`RBAC_ADMIN_USER`/`RBAC_ADMIN_PASSWORD`). If auth is disabled, the modules work
without signing in.

## Pages
- `/` dashboard — session, security posture, audit-chain status
- `/login` — RBAC sign-in
- `/subjects` — compliance subjects + integrity report (IDs link to detail)
- `/subjects/[id]` — **cross-module subject view**: compliance + visit logs +
  trips + invoice, joined by the shared subject id
- `/trips` — dispatch trips with live quote and an invoice panel (all / by subject)
- `/protocol` — paste protocol text → **editable** visit schedule (chronological)
  + conflicts, with edited-CSV export
- `/chat` — AI assistant with **live streaming** replies and provenance/sources
  (de-identified before reaching the model)

## Status
Consolidation covering every module against the real API, with streaming chat,
an editable protocol review, and the cross-module subject view. Supersedes the
older `clinical_protocol_ui_full` scaffold (protocol-only, mock data). Possible
next steps: serve the built UI behind the same host as the API (single origin in
prod), and richer SoA grid editing (toggle marks per visit).
