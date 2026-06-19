"""Lightweight access gate for the platform's data endpoints.

Disabled by default so local development isn't locked out. Set
``PLATFORM_AUTH_TOKEN`` in the environment to require a shared access token on
every data endpoint (compliance, dispatch, chat, protocol parse/export). Clients
authenticate once via ``POST /auth/login`` (sets an HttpOnly cookie) or by
sending ``Authorization: Bearer <token>``. The cookie means the existing UIs
work unchanged once logged in -- the browser sends it automatically.

This is a single-shared-secret gate suitable for an internal/demo deployment;
it is not a multi-user identity system.
"""
import hmac
import os
from typing import TYPE_CHECKING

from fastapi import APIRouter, Form, HTTPException, Request, Response

from . import context as _ctx

COOKIE = "crp_auth"


def _expected() -> str:
    return os.environ.get("PLATFORM_AUTH_TOKEN", "")


def auth_enabled() -> bool:
    return bool(_expected())


def _provided(request: Request):
    tok = request.cookies.get(COOKIE)
    if tok:
        return tok
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return None


def _valid(token) -> bool:
    expected = _expected()
    return bool(token) and hmac.compare_digest(str(token), expected)


def _rbac_session_user(request: Request):
    """Return the username from a valid RBAC session cookie/Bearer, or None.

    Imported lazily to avoid a circular import (rbac imports auth, auth
    would import rbac at module load time without the lazy guard).
    """
    try:
        from . import rbac as _rbac  # noqa: PLC0415  (intentional lazy import)
        token = _rbac._extract_session_token(request)
        if not token:
            return None
        return _rbac._resolve_session(token)
    except Exception:
        return None


async def require_auth(request: Request):
    """FastAPI dependency: allow if auth disabled, else require a valid token.

    Async so the actor contextvar set below runs in the request task and
    propagates into the (threadpool-executed) sync route handler, where
    ``audit.record`` reads it. A sync dependency would set it in a separate
    threadpool thread and the value would be lost.

    Accepts EITHER:
    * The shared PLATFORM_AUTH_TOKEN (original behaviour), OR
    * A valid RBAC session cookie / Bearer token.

    If a valid RBAC session is found the context actor is set so downstream
    audit entries are attributed to the correct user.
    """
    # --- Check RBAC session first (works even when PLATFORM_AUTH_TOKEN is set) ---
    rbac_user = _rbac_session_user(request)
    if rbac_user:
        _ctx.set_actor(rbac_user)
        return  # authenticated via RBAC

    if not auth_enabled():
        return  # no token configured -> open

    if not _valid(_provided(request)):
        raise HTTPException(
            status_code=401,
            detail="Authentication required. Log in at /ui/login.html",
        )


router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login")
def login(response: Response, token: str = Form(...)):
    if not auth_enabled():
        return {"ok": True, "auth": "disabled"}
    if not _valid(token):
        raise HTTPException(status_code=401, detail="Invalid token")
    # HttpOnly so page scripts can't read it; SameSite=strict to curb CSRF.
    response.set_cookie(COOKIE, token, httponly=True, samesite="strict")
    return {"ok": True}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE)
    return {"ok": True}


@router.get("/status")
def status(request: Request):
    return {
        "auth_enabled": auth_enabled(),
        "authenticated": (not auth_enabled()) or _valid(_provided(request)),
    }
