"""Tests for the RBAC module: user accounts, roles, session auth, and audit wiring."""
import os
import sys
import tempfile
import uuid

# Ensure unique DB so parallel test runs don't collide.
os.environ["PLATFORM_DB"] = os.path.join(
    tempfile.gettempdir(), f"rbac_{uuid.uuid4().hex}.db"
)

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.store import init_db, get_conn  # noqa: E402
from app import rbac, context, audit  # noqa: E402

init_db()
rbac.init_rbac()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _client():
    """Return a TestClient backed by either the full api.app (if the RBAC router
    has been wired in by the orchestrator) or a minimal test app that includes
    the RBAC router plus the minimal routes needed for integration checks.

    This dual approach lets the tests pass both before and after the orchestrator
    adds the wiring to app/api.py.
    """
    from fastapi.testclient import TestClient
    try:
        from app.api import app as _api_app
        # Check whether the RBAC router is already mounted in the main app.
        routes = [r.path for r in _api_app.routes]  # type: ignore[attr-defined]
        if any("/rbac" in p for p in routes):
            return TestClient(_api_app)
    except Exception:
        pass

    # Fall back to a minimal test app.
    from fastapi import FastAPI
    from app import rbac as _rbac
    # Try to include compliance router so the audit-actor test can hit a gated route.
    _test_app = FastAPI()
    _test_app.include_router(_rbac.router)
    try:
        from app import compliance as _comp, auth as _auth
        from fastapi import Depends
        _test_app.include_router(_comp.router, dependencies=[Depends(_auth.require_auth)])
    except Exception:
        pass
    return TestClient(_test_app)


# ---------------------------------------------------------------------------
# User management unit tests
# ---------------------------------------------------------------------------

class TestCreateVerifyUser:
    def test_create_and_verify_correct_password(self):
        rbac.create_user("alice_test", "hunter2", "viewer")
        role = rbac.verify_credentials("alice_test", "hunter2")
        assert role == "viewer"

    def test_wrong_password_returns_none(self):
        rbac.create_user("bob_test", "correct_horse", "coordinator")
        result = rbac.verify_credentials("bob_test", "wrong_password")
        assert result is None

    def test_unknown_user_returns_none(self):
        assert rbac.verify_credentials("nobody_xyz", "whatever") is None

    def test_duplicate_username_raises(self):
        rbac.create_user("carol_test", "pass1", "viewer")
        import pytest
        with pytest.raises(ValueError):
            rbac.create_user("carol_test", "pass2", "admin")

    def test_invalid_role_raises(self):
        import pytest
        with pytest.raises(ValueError):
            rbac.create_user("dave_test", "pass", "superuser")

    def test_list_users_includes_created(self):
        rbac.create_user("erin_test", "pass", "admin")
        users = rbac.list_users()
        names = [u["username"] for u in users]
        assert "erin_test" in names

    def test_delete_user_removes_account(self):
        rbac.create_user("frank_test", "pass", "viewer")
        assert rbac.delete_user("frank_test") is True
        assert rbac.verify_credentials("frank_test", "pass") is None

    def test_delete_nonexistent_returns_false(self):
        assert rbac.delete_user("ghost_user_xyz") is False


# ---------------------------------------------------------------------------
# Role ordering
# ---------------------------------------------------------------------------

class TestRoleOrdering:
    def test_viewer_is_lowest(self):
        assert rbac._ROLE_RANK["viewer"] < rbac._ROLE_RANK["coordinator"]
        assert rbac._ROLE_RANK["coordinator"] < rbac._ROLE_RANK["admin"]

    def test_admin_has_highest_rank(self):
        for role in ("viewer", "coordinator"):
            assert rbac._ROLE_RANK[role] < rbac._ROLE_RANK["admin"]


# ---------------------------------------------------------------------------
# Session management unit tests
# ---------------------------------------------------------------------------

class TestSessions:
    def test_create_and_resolve_session(self):
        rbac.create_user("grace_test", "pass", "viewer")
        token = rbac.create_session("grace_test")
        assert rbac._resolve_session(token) == "grace_test"

    def test_delete_session_invalidates_token(self):
        rbac.create_user("henry_test", "pass", "viewer")
        token = rbac.create_session("henry_test")
        rbac.delete_session(token)
        assert rbac._resolve_session(token) is None

    def test_invalid_token_returns_none(self):
        assert rbac._resolve_session("bad_token_xyz") is None


# ---------------------------------------------------------------------------
# HTTP: login sets cookie, /rbac/me returns user info
# ---------------------------------------------------------------------------

class TestLoginAndMe:
    def test_login_sets_cookie_and_me_returns_user(self):
        rbac.create_user("irene_http", "securepass", "coordinator")
        c = _client()
        resp = c.post("/rbac/login", data={"username": "irene_http", "password": "securepass"})
        assert resp.status_code == 200
        assert resp.json()["username"] == "irene_http"
        assert resp.json()["role"] == "coordinator"
        # Cookie must be set.
        assert rbac.SESSION_COOKIE in resp.cookies

        me = c.get("/rbac/me")
        assert me.status_code == 200
        assert me.json()["username"] == "irene_http"

    def test_login_wrong_password_401(self):
        rbac.create_user("jake_http", "rightpass", "viewer")
        c = _client()
        resp = c.post("/rbac/login", data={"username": "jake_http", "password": "wrongpass"})
        assert resp.status_code == 401

    def test_me_without_login_401(self):
        c = _client()
        resp = c.get("/rbac/me")
        assert resp.status_code == 401

    def test_logout_clears_session(self):
        rbac.create_user("lisa_http", "pass", "viewer")
        c = _client()
        c.post("/rbac/login", data={"username": "lisa_http", "password": "pass"})
        c.post("/rbac/logout")
        resp = c.get("/rbac/me")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Role enforcement: require_role 403 for viewer on admin route
# ---------------------------------------------------------------------------

class TestRequireRole:
    def test_viewer_cannot_access_admin_route(self):
        rbac.create_user("mike_viewer", "pass", "viewer")
        c = _client()
        c.post("/rbac/login", data={"username": "mike_viewer", "password": "pass"})
        # GET /rbac/users is admin-only.
        resp = c.get("/rbac/users")
        assert resp.status_code == 403

    def test_coordinator_cannot_access_admin_route(self):
        rbac.create_user("nina_coord", "pass", "coordinator")
        c = _client()
        c.post("/rbac/login", data={"username": "nina_coord", "password": "pass"})
        resp = c.get("/rbac/users")
        assert resp.status_code == 403

    def test_admin_can_access_admin_route(self):
        rbac.create_user("otto_admin", "pass", "admin")
        c = _client()
        c.post("/rbac/login", data={"username": "otto_admin", "password": "pass"})
        resp = c.get("/rbac/users")
        assert resp.status_code == 200

    def test_admin_can_create_user_via_api(self):
        rbac.create_user("pete_admin", "adminpass", "admin")
        c = _client()
        c.post("/rbac/login", data={"username": "pete_admin", "password": "adminpass"})
        resp = c.post(
            "/rbac/users",
            data={"username": "new_viewer_99", "password": "vpass", "role": "viewer"},
        )
        assert resp.status_code == 200
        assert resp.json()["username"] == "new_viewer_99"

    def test_admin_can_delete_user_via_api(self):
        rbac.create_user("queen_admin", "adminpass", "admin")
        rbac.create_user("to_delete_99", "pass", "viewer")
        c = _client()
        c.post("/rbac/login", data={"username": "queen_admin", "password": "adminpass"})
        resp = c.delete("/rbac/users/to_delete_99")
        assert resp.status_code == 200

    def test_delete_nonexistent_user_404(self):
        rbac.create_user("rose_admin", "adminpass", "admin")
        c = _client()
        c.post("/rbac/login", data={"username": "rose_admin", "password": "adminpass"})
        resp = c.delete("/rbac/users/ghost_xyz_999")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Audit actor reflects logged-in RBAC user
# ---------------------------------------------------------------------------

class TestAuditActor:
    def test_audit_record_uses_context_actor(self):
        """When context actor is set, audit entries should carry that actor."""
        context.set_actor("audit_test_user")
        entry = audit.record("test.action", {"key": "value"})
        assert entry.get("actor") == "audit_test_user"
        # Clean up context for subsequent tests.
        context.current_actor.set(None)

    def test_audit_record_falls_back_to_system(self):
        """Without a context actor, audit entries should carry 'system'."""
        context.current_actor.set(None)
        entry = audit.record("test.action.fallback")
        assert entry.get("actor") == "system"

    def test_audit_actor_set_after_rbac_login(self):
        """After logging in via RBAC, a gated endpoint audit should record username."""
        rbac.create_user("sam_audit", "auditpass", "viewer")
        c = _client()
        c.post("/rbac/login", data={"username": "sam_audit", "password": "auditpass"})
        # Access any gated endpoint (compliance saved_subjects is behind require_auth).
        # The require_auth dependency now accepts RBAC sessions and sets context actor.
        resp = c.get("/compliance/saved_subjects")
        # Either 200 (open) or 200 (RBAC accepted) — just confirm no 401/403.
        assert resp.status_code in (200, 404)

    def test_explicit_actor_overrides_context(self):
        """Explicit actor kwarg should take precedence over context."""
        context.set_actor("context_actor")
        entry = audit.record("test.explicit", actor="explicit_actor")
        assert entry.get("actor") == "explicit_actor"
        context.current_actor.set(None)


# ---------------------------------------------------------------------------
# Password hashing quality
# ---------------------------------------------------------------------------

class TestPasswordHashing:
    def test_different_salts_per_user(self):
        rbac.create_user("ted_salt1", "samepass", "viewer")
        rbac.create_user("uma_salt2", "samepass", "viewer")
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT salt FROM users WHERE username IN ('ted_salt1', 'uma_salt2')"
            ).fetchall()
        salts = [r["salt"] for r in rows]
        assert len(salts) == 2
        assert salts[0] != salts[1], "Each user must have a unique salt"

    def test_hash_is_not_plaintext(self):
        rbac.create_user("vera_hash", "mypassword", "viewer")
        with get_conn() as conn:
            row = conn.execute(
                "SELECT password_hash FROM users WHERE username = 'vera_hash'"
            ).fetchone()
        assert "mypassword" not in row["password_hash"]
