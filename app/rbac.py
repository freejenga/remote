"""Role-Based Access Control for the Clinical Research Platform.

Provides real multi-user accounts stored in SQLite (via store.get_conn()),
session tokens that survive restarts, FastAPI dependencies for auth and
role enforcement, and an RBAC router mounted at ``/rbac``.

Roles (ordered least -> most privileged): viewer < coordinator < admin.

Passwords hashed with PBKDF2-HMAC-SHA256, >=200 000 iterations, per-user
random salt.  No external dependencies — only stdlib (hashlib, secrets).
"""
from __future__ import annotations

import hashlib
import os
import secrets
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response

from .store import get_conn
from . import context as _ctx

# ---------------------------------------------------------------------------
# Role ordering
# ---------------------------------------------------------------------------

ROLES = ("viewer", "coordinator", "admin")
_ROLE_RANK = {r: i for i, r in enumerate(ROLES)}

SESSION_COOKIE = "crp_session"
_ITERATIONS = 200_000
_HASH_ALG = "sha256"
_SALT_BYTES = 32
_TOKEN_BYTES = 32


# ---------------------------------------------------------------------------
# DB initialisation
# ---------------------------------------------------------------------------

def init_rbac() -> None:
    """Create ``users`` and ``sessions`` tables; optionally seed a default admin."""
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                username   TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL,
                salt       TEXT NOT NULL,
                role       TEXT NOT NULL DEFAULT 'viewer',
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                token      TEXT PRIMARY KEY,
                username   TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )

    # Seed default admin from env if no users exist yet.
    admin_user = os.environ.get("RBAC_ADMIN_USER", "")
    admin_pass = os.environ.get("RBAC_ADMIN_PASSWORD", "")
    if admin_user and admin_pass:
        with get_conn() as conn:
            count = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        if count == 0:
            create_user(admin_user, admin_pass, "admin")


# ---------------------------------------------------------------------------
# Password helpers
# ---------------------------------------------------------------------------

def _hash_password(password: str, salt_hex: str) -> str:
    salt = bytes.fromhex(salt_hex)
    dk = hashlib.pbkdf2_hmac(_HASH_ALG, password.encode(), salt, _ITERATIONS)
    return dk.hex()


def _new_salt() -> str:
    return secrets.token_bytes(_SALT_BYTES).hex()


# ---------------------------------------------------------------------------
# User management
# ---------------------------------------------------------------------------

def create_user(username: str, password: str, role: str = "viewer") -> dict:
    """Create a new user; raise ValueError on duplicate username or bad role."""
    if role not in _ROLE_RANK:
        raise ValueError(f"Unknown role {role!r}. Choose from: {ROLES}")
    salt = _new_salt()
    ph = _hash_password(password, salt)
    try:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO users (username, password_hash, salt, role) VALUES (?, ?, ?, ?)",
                (username, ph, salt, role),
            )
    except Exception as exc:
        raise ValueError(f"Could not create user {username!r}: {exc}") from exc
    return {"username": username, "role": role}


def verify_credentials(username: str, password: str) -> Optional[str]:
    """Return role if credentials are valid, else None."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT password_hash, salt, role FROM users WHERE username = ?",
            (username,),
        ).fetchone()
    if row is None:
        return None
    expected = _hash_password(password, row["salt"])
    # Constant-time comparison to resist timing attacks.
    if not secrets.compare_digest(expected, row["password_hash"]):
        return None
    return row["role"]


def list_users() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT username, role, created_at FROM users ORDER BY created_at"
        ).fetchall()
    return [{"username": r["username"], "role": r["role"], "created_at": r["created_at"]} for r in rows]


def delete_user(username: str) -> bool:
    """Delete user and all their sessions.  Returns True if the user existed."""
    with get_conn() as conn:
        deleted = conn.execute(
            "DELETE FROM users WHERE username = ?", (username,)
        ).rowcount
        conn.execute("DELETE FROM sessions WHERE username = ?", (username,))
    return deleted > 0


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

def create_session(username: str) -> str:
    """Create a persisted opaque session token and return it."""
    token = secrets.token_hex(_TOKEN_BYTES)
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO sessions (token, username) VALUES (?, ?)",
            (token, username),
        )
    return token


def _resolve_session(token: str) -> Optional[str]:
    """Return the username for a valid token, else None."""
    if not token:
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT username FROM sessions WHERE token = ?", (token,)
        ).fetchone()
    return row["username"] if row else None


def delete_session(token: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


def _get_user_role(username: str) -> Optional[str]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT role FROM users WHERE username = ?", (username,)
        ).fetchone()
    return row["role"] if row else None


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------

def _extract_session_token(request: Request) -> Optional[str]:
    tok = request.cookies.get(SESSION_COOKIE)
    if tok:
        return tok
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return None


def require_user(request: Request) -> dict:
    """Dependency: resolve session cookie/Bearer -> user dict; 401 if invalid."""
    token = _extract_session_token(request)
    username = _resolve_session(token) if token else None
    if not username:
        raise HTTPException(status_code=401, detail="RBAC login required. POST /rbac/login")
    role = _get_user_role(username)
    if role is None:
        raise HTTPException(status_code=401, detail="User account not found")
    _ctx.set_actor(username)
    return {"username": username, "role": role}


def require_role(min_role: str):
    """Return a FastAPI dependency that enforces a minimum role level."""
    if min_role not in _ROLE_RANK:
        raise ValueError(f"Unknown role {min_role!r}")

    def _dep(user: dict = Depends(require_user)) -> dict:
        if _ROLE_RANK[user["role"]] < _ROLE_RANK[min_role]:
            raise HTTPException(
                status_code=403,
                detail=f"Role {user['role']!r} insufficient; need {min_role!r} or above",
            )
        return user

    return _dep


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/rbac", tags=["rbac"])


@router.post("/login")
def rbac_login(
    response: Response,
    username: str = Form(...),
    password: str = Form(...),
):
    role = verify_credentials(username, password)
    if role is None:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token = create_session(username)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="strict",
    )
    return {"ok": True, "username": username, "role": role}


@router.post("/logout")
def rbac_logout(request: Request, response: Response):
    token = _extract_session_token(request)
    if token:
        delete_session(token)
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@router.get("/me")
def rbac_me(user: dict = Depends(require_user)):
    return user


# Admin-only user management endpoints
@router.post("/users", dependencies=[Depends(require_role("admin"))])
def create_user_endpoint(
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form("viewer"),
):
    try:
        return create_user(username, password, role)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/users", dependencies=[Depends(require_role("admin"))])
def list_users_endpoint():
    return {"users": list_users()}


@router.delete("/users/{username}", dependencies=[Depends(require_role("admin"))])
def delete_user_endpoint(username: str):
    if not delete_user(username):
        raise HTTPException(status_code=404, detail=f"User {username!r} not found")
    return {"ok": True, "deleted": username}
