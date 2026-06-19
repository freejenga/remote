"""Async-safe request context: the currently authenticated actor.

Uses a ``contextvars.ContextVar`` so each concurrent request carries its own
actor identity without thread-local leakage.  Set early in the request
lifecycle (e.g., from a FastAPI dependency) and read from audit/business code
without passing the value explicitly.
"""
from contextvars import ContextVar
from typing import Optional

# Default to None so callers can distinguish "not set" from "system".
current_actor: ContextVar[Optional[str]] = ContextVar("current_actor", default=None)


def get_actor() -> Optional[str]:
    """Return the actor bound to the current request context, or None."""
    return current_actor.get()


def set_actor(username: str) -> None:
    """Bind *username* as the actor for the current request context."""
    current_actor.set(username)
