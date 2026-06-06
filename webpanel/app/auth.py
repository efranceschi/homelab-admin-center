"""Authentication, sessions, CSRF, and access guards.

- Passwords hashed with Argon2id (argon2-cffi), transparently rehashed on login.
- Sessions are signed cookies via Starlette's SessionMiddleware.
- CSRF uses a per-session synchronizer token verified on every unsafe method.
- A first-run setup flow creates the initial admin (no default password ships).
"""
from __future__ import annotations

import secrets
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import db_dependency
from .models import User, utcnow

_hasher = PasswordHasher()

MAX_FAILED_LOGINS = 5
LOCKOUT = timedelta(minutes=10)


# --------------------------------------------------------------------------- #
# Password hashing
# --------------------------------------------------------------------------- #
def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        _hasher.verify(stored_hash, password)
        return True
    except (VerifyMismatchError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True


# --------------------------------------------------------------------------- #
# User / setup helpers
# --------------------------------------------------------------------------- #
def user_count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(User)) or 0


def is_setup_complete(db: Session) -> bool:
    return user_count(db) > 0


def create_user(db: Session, username: str, password: str, role: str = "admin") -> User:
    user = User(username=username, password_hash=hash_password(password), role=role)
    db.add(user)
    db.flush()
    return user


def authenticate(db: Session, username: str, password: str) -> User | None:
    user = db.scalar(select(User).where(User.username == username))
    if user is None or not user.is_active:
        return None
    if user.locked_until and user.locked_until > utcnow():
        return None
    if not verify_password(user.password_hash, password):
        user.failed_logins += 1
        if user.failed_logins >= MAX_FAILED_LOGINS:
            user.locked_until = utcnow() + LOCKOUT
            user.failed_logins = 0
        return None
    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = utcnow()
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    return user


# --------------------------------------------------------------------------- #
# Session
# --------------------------------------------------------------------------- #
def login_session(request: Request, user: User) -> None:
    request.session["user_id"] = user.id
    request.session["role"] = user.role
    request.session["username"] = user.username
    request.session.setdefault("csrf", secrets.token_urlsafe(32))


def logout_session(request: Request) -> None:
    request.session.clear()


def get_csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


async def verify_csrf(request: Request) -> None:
    """Dependency: enforce CSRF token on unsafe methods."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    submitted = request.headers.get("x-csrf-token")
    if submitted is None:
        form = await request.form()
        submitted = form.get("csrf_token")
    expected = request.session.get("csrf")
    if not expected or not submitted or not secrets.compare_digest(str(submitted), expected):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF token invalid or missing")


# --------------------------------------------------------------------------- #
# Guards
# --------------------------------------------------------------------------- #
class RedirectToLogin(Exception):
    """Raised to bounce an unauthenticated browser to /login."""


def current_user(
    request: Request, db: Session = Depends(db_dependency)
) -> User:
    uid = request.session.get("user_id")
    if uid is None:
        raise RedirectToLogin()
    user = db.get(User, uid)
    if user is None or not user.is_active:
        request.session.clear()
        raise RedirectToLogin()
    return user


def require_role(*roles: str):
    def _dep(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient privileges")
        return user

    return _dep


require_admin = require_role("admin")


def install_redirect_handler(app) -> None:
    """Register the exception handler that turns RedirectToLogin into a 303."""
    from fastapi import Request as _Request

    @app.exception_handler(RedirectToLogin)
    async def _handler(request: _Request, _exc: RedirectToLogin):
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
