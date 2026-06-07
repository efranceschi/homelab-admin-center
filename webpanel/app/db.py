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

SCHEMA_VERSION = "4"

# Columns added after the initial release, keyed by table. PRAGMA table_info is
# the source of truth, so applying these is idempotent (only missing columns are
# added). create_all() never alters an existing table, hence this additive step.
_ADDITIVE_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "host_state": [
        ("config_status", "VARCHAR(16)"),
        ("config_checked_at", "DATETIME"),
        ("pending_changes", "INTEGER NOT NULL DEFAULT 0"),
    ],
    "jobs": [
        ("log_text", "TEXT"),
        ("server_ids", "VARCHAR(512)"),
        ("plugin_ids", "VARCHAR(512)"),
    ],
}

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


def _migrate(engine: Engine) -> None:
    """Apply additive ALTER TABLE statements for columns added post-v1.

    SQLite's ``ALTER TABLE ADD COLUMN`` is online and cheap; only columns the
    table is missing are added (never dropped/renamed), so this is safe to run
    on every boot.
    """
    with engine.begin() as conn:
        for table, cols in _ADDITIVE_COLUMNS.items():
            rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
            have = {r[1] for r in rows}  # r[1] == column name
            for name, type_clause in cols:
                if name not in have:
                    conn.exec_driver_sql(
                        f"ALTER TABLE {table} ADD COLUMN {name} {type_clause}"
                    )


def init_db() -> None:
    """Create tables, apply additive migrations, and tighten DB permissions."""
    engine = init_engine()
    Base.metadata.create_all(engine)
    _migrate(engine)
    try:
        os.chmod(config.DB_PATH, 0o600)
    except OSError:
        pass
    with session_scope() as db:
        from .models import Setting

        row = db.get(Setting, "schema_version")
        if row is None:
            db.add(Setting(key="schema_version", value=SCHEMA_VERSION))
        elif row.value != SCHEMA_VERSION:
            row.value = SCHEMA_VERSION


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
