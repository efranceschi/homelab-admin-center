"""Database engine, session factory, and schema initialization.

Uses SQLite with WAL journaling (so the dashboard can read while a job writes)
and enforced foreign keys. For the MVP the schema is created with
``Base.metadata.create_all`` plus a ``schema_version`` setting; a migration tool
(Alembic) can be layered on later without changing call sites.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from . import config
from .models import Base

SCHEMA_VERSION = "1"

_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None


def _on_connect(dbapi_conn, _record) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()


def init_engine() -> Engine:
    global _engine, _SessionFactory
    if _engine is not None:
        return _engine
    config.ensure_runtime_dirs()
    url = f"sqlite:///{config.DB_PATH}"
    _engine = create_engine(url, future=True, connect_args={"check_same_thread": False})
    event.listen(_engine, "connect", _on_connect)
    _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def init_db() -> None:
    """Create tables and tighten DB file permissions."""
    engine = init_engine()
    Base.metadata.create_all(engine)
    try:
        os.chmod(config.DB_PATH, 0o600)
    except OSError:
        pass
    with session_scope() as db:
        from .models import Setting

        if db.get(Setting, "schema_version") is None:
            db.add(Setting(key="schema_version", value=SCHEMA_VERSION))


def get_session() -> Session:
    if _SessionFactory is None:
        init_engine()
    assert _SessionFactory is not None
    return _SessionFactory()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session context manager."""
    db = get_session()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def db_dependency() -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped session."""
    db = get_session()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
