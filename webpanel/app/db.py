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

SCHEMA_VERSION = "11"

# Columns added after the initial release, keyed by table. PRAGMA table_info is
# the source of truth, so applying these is idempotent (only missing columns are
# added). create_all() never alters an existing table, hence this additive step.
_ADDITIVE_COLUMNS: dict[str, list[tuple[str, str]]] = {
    # The virtualization tree (v9). SQLite cannot add a foreign-key action via
    # ALTER, so parent_server_id is a plain INTEGER here; the SET NULL semantics
    # live in the model (fresh DBs) and in the delete route (upgraded DBs).
    "servers": [
        ("parent_server_id", "INTEGER"),
        ("virt_kind", "VARCHAR(16)"),
        ("guest_type", "VARCHAR(8)"),
    ],
    "host_state": [
        ("config_status", "VARCHAR(16)"),
        ("config_checked_at", "DATETIME"),
        ("pending_changes", "INTEGER NOT NULL DEFAULT 0"),
    ],
    "jobs": [
        ("log_text", "TEXT"),
        ("server_ids", "VARCHAR(512)"),
        ("plugin_ids", "VARCHAR(512)"),
        ("group_ids", "VARCHAR(512)"),
        ("kind", "VARCHAR(16) NOT NULL DEFAULT 'ansible'"),
    ],
    "schedules": [
        ("group_ids", "VARCHAR(512)"),
    ],
    # discovered_hosts was renamed to discoveries (see _migrate_pre_create) and
    # generalized from "new hosts only" to any discovery kind. These columns are
    # added to the renamed table; existing rows are backfilled in _migrate.
    "discoveries": [
        ("kind", "VARCHAR(16)"),
        ("status", "VARCHAR(16)"),
        ("server_id", "INTEGER"),
        ("old_name", "VARCHAR(128)"),
        ("new_name", "VARCHAR(128)"),
        ("resolved_at", "DATETIME"),
        ("guest_type", "VARCHAR(8)"),
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


def _table_columns(conn, table: str) -> set[str]:
    rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}  # r[1] == column name


def _table_exists(conn, table: str) -> bool:
    row = conn.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _migrate_pre_create(engine: Engine) -> None:
    """Structural migrations that must run BEFORE ``create_all``.

    ``create_all`` would otherwise materialize an empty ``discoveries`` table and
    block the rename. Idempotent: each step is guarded so reruns (and fresh
    installs, where the legacy table is absent) are no-ops.

    1. Rename ``discovered_hosts`` -> ``discoveries`` (the table now holds any
       discovery kind, not just new hosts).
    2. Rename the legacy running/stopped ``status`` column to ``status_text`` so
       the freed ``status`` name can carry the discovery lifecycle. Needs SQLite
       >= 3.34 (RENAME COLUMN); Proxmox ships newer.
    """
    with engine.begin() as conn:
        if _table_exists(conn, "discovered_hosts") and not _table_exists(
            conn, "discoveries"
        ):
            conn.exec_driver_sql("ALTER TABLE discovered_hosts RENAME TO discoveries")
        if _table_exists(conn, "discoveries"):
            cols = _table_columns(conn, "discoveries")
            if "status" in cols and "status_text" not in cols:
                conn.exec_driver_sql(
                    "ALTER TABLE discoveries RENAME COLUMN status TO status_text"
                )


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
        # Virtualization-tree backfill (v9). Idempotent: only ever touches rows
        # left NULL by the additive ALTERs above. Existing proxmox guests are all
        # LXC; the single local host is the Proxmox node that runs them.
        conn.exec_driver_sql(
            "UPDATE servers SET guest_type='lxc' "
            "WHERE connection_type='proxmox' AND guest_type IS NULL"
        )
        conn.exec_driver_sql(
            "UPDATE servers SET virt_kind='proxmox' "
            "WHERE virt_kind IS NULL AND connection_type='local' "
            "AND EXISTS (SELECT 1 FROM servers g WHERE g.connection_type='proxmox')"
        )
        conn.exec_driver_sql(
            "UPDATE servers SET parent_server_id="
            "(SELECT id FROM servers n WHERE n.connection_type='local' LIMIT 1) "
            "WHERE connection_type='proxmox' AND parent_server_id IS NULL "
            "AND EXISTS (SELECT 1 FROM servers n WHERE n.connection_type='local')"
        )
        # Collapse the legacy two-axis drift vocabulary onto the single settled
        # host state (ok|pending|failed). Idempotent: live runs never re-create
        # the old values, so this only ever touches pre-upgrade rows.
        conn.exec_driver_sql(
            "UPDATE host_state SET config_status='ok'      WHERE config_status='updated'"
        )
        conn.exec_driver_sql(
            "UPDATE host_state SET config_status='pending' WHERE config_status='out_of_date'"
        )
        conn.exec_driver_sql(
            "UPDATE host_state SET config_status='failed'  WHERE config_status='unknown'"
        )
        # Backfill the generalized discoveries table. Pre-upgrade rows are all
        # new_host; the old `dismissed` bool maps onto the lifecycle status.
        # Idempotent: only ever touches rows left NULL by the additive ALTERs.
        if _table_exists(conn, "discoveries"):
            conn.exec_driver_sql(
                "UPDATE discoveries SET kind='new_host' WHERE kind IS NULL"
            )
            cols = _table_columns(conn, "discoveries")
            if "dismissed" in cols:
                conn.exec_driver_sql(
                    "UPDATE discoveries SET status='ignored' "
                    "WHERE status IS NULL AND dismissed=1"
                )
            conn.exec_driver_sql(
                "UPDATE discoveries SET status='pending' WHERE status IS NULL"
            )
            if "dismissed" in cols:
                # The unified Discovery model dropped the legacy `dismissed`
                # bool (its meaning now lives in `status`), but the physical
                # column was left behind NOT NULL with no default — so every
                # new insert that omits it raised IntegrityError. Drop it now
                # that status has been backfilled from it. SQLite >=3.35.
                conn.exec_driver_sql(
                    "ALTER TABLE discoveries DROP COLUMN dismissed"
                )
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_discoveries_source_vmid "
                "ON discoveries (source, proxmox_vmid)"
            )
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_discoveries_server_status "
                "ON discoveries (server_id, status)"
            )


def init_db() -> None:
    """Create tables, apply additive migrations, and tighten DB permissions."""
    engine = init_engine()
    _migrate_pre_create(engine)
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
