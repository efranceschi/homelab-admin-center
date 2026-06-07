"""Unit tests for password hashing, authentication, lockout, sessions, CSRF, RBAC."""

from __future__ import annotations

from datetime import timedelta

import pytest
from app import auth
from app.models import User, utcnow
from fastapi import HTTPException
from sqlalchemy import select


class FakeRequest:
    """Minimal stand-in for starlette Request (header + dict session only)."""

    def __init__(
        self, method: str = "POST", headers: dict | None = None, session: dict | None = None
    ):
        self.method = method
        self.headers = headers or {}
        self.session = session if session is not None else {}


# --------------------------------------------------------------------------- #
# Password hashing
# --------------------------------------------------------------------------- #
def test_hash_and_verify_roundtrip():
    h = auth.hash_password("s3cret-pass")
    assert h != "s3cret-pass"
    assert auth.verify_password(h, "s3cret-pass") is True
    assert auth.verify_password(h, "wrong") is False


def test_needs_rehash_on_garbage():
    assert auth.needs_rehash("not-a-valid-argon2-hash") is True


# --------------------------------------------------------------------------- #
# authenticate() + lockout
# --------------------------------------------------------------------------- #
def _make_user(db, password="adminpass1", role="admin", active=True):
    user = auth.create_user(db, "alice", password, role=role)
    user.is_active = active
    db.flush()
    return user


def test_authenticate_success(db):
    _make_user(db)
    user = auth.authenticate(db, "alice", "adminpass1")
    assert user is not None
    assert user.failed_logins == 0
    assert user.last_login_at is not None


def test_authenticate_wrong_password_increments(db):
    _make_user(db)
    assert auth.authenticate(db, "alice", "nope") is None
    db.flush()
    assert db.scalar(select(User)).failed_logins == 1


def test_authenticate_locks_after_max_failures(db):
    _make_user(db)
    for _ in range(auth.MAX_FAILED_LOGINS):
        assert auth.authenticate(db, "alice", "nope") is None
    db.flush()
    user = db.get(User, 1)
    assert user.locked_until is not None
    # Even the correct password is rejected while locked.
    assert auth.authenticate(db, "alice", "adminpass1") is None


def test_authenticate_unlocks_after_expiry(db):
    user = _make_user(db)
    user.locked_until = utcnow() - timedelta(minutes=1)  # lock already expired
    db.flush()
    assert auth.authenticate(db, "alice", "adminpass1") is not None


def test_authenticate_inactive_user(db):
    _make_user(db, active=False)
    assert auth.authenticate(db, "alice", "adminpass1") is None


def test_authenticate_unknown_user(db):
    assert auth.authenticate(db, "ghost", "whatever") is None


# --------------------------------------------------------------------------- #
# Sessions + CSRF
# --------------------------------------------------------------------------- #
def test_login_logout_session(db):
    user = _make_user(db)
    req = FakeRequest()
    auth.login_session(req, user)
    assert req.session["user_id"] == user.id
    assert req.session["role"] == "admin"
    assert req.session["csrf"]
    auth.logout_session(req)
    assert req.session == {}


def test_get_csrf_token_is_stable():
    req = FakeRequest(method="GET")
    first = auth.get_csrf_token(req)
    assert first == auth.get_csrf_token(req)  # same token returned within a session


@pytest.mark.asyncio
async def test_verify_csrf_allows_safe_methods():
    await auth.verify_csrf(FakeRequest(method="GET"))  # no exception


@pytest.mark.asyncio
async def test_verify_csrf_header_match():
    req = FakeRequest(method="POST", headers={"x-csrf-token": "abc"}, session={"csrf": "abc"})
    await auth.verify_csrf(req)  # no exception


@pytest.mark.asyncio
async def test_verify_csrf_rejects_mismatch():
    req = FakeRequest(method="POST", headers={"x-csrf-token": "bad"}, session={"csrf": "abc"})
    with pytest.raises(HTTPException) as exc:
        await auth.verify_csrf(req)
    assert exc.value.status_code == 403


# --------------------------------------------------------------------------- #
# RBAC guards
# --------------------------------------------------------------------------- #
def test_require_role_allows_and_blocks():
    admin = User(username="a", password_hash="x", role="admin")
    viewer = User(username="v", password_hash="x", role="viewer")
    assert auth.require_admin(user=admin) is admin
    with pytest.raises(HTTPException) as exc:
        auth.require_admin(user=viewer)
    assert exc.value.status_code == 403
