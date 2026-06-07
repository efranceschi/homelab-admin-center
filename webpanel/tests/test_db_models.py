"""Unit tests for DB init, additive migrations, WAL, and FK enforcement."""

from __future__ import annotations

import pytest
from app import db as dbmod
from app.models import HostGroupChild, Setting
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError


def test_init_db_is_idempotent_and_sets_schema_version():
    dbmod.init_db()
    dbmod.init_db()  # second call must not raise
    with dbmod.session_scope() as s:
        row = s.get(Setting, "schema_version")
        assert row is not None
        assert row.value == dbmod.SCHEMA_VERSION


def test_migrate_is_idempotent():
    engine = dbmod.init_engine()
    # Running the additive migration twice adds no duplicate columns / no error.
    dbmod._migrate(engine)
    dbmod._migrate(engine)
    with engine.connect() as conn:
        cols = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(jobs)").fetchall()}
    # Columns declared in _ADDITIVE_COLUMNS must all be present.
    assert {"log_text", "server_ids", "plugin_ids", "group_ids"} <= cols


def test_wal_journal_mode_enabled():
    engine = dbmod.init_engine()
    with engine.connect() as conn:
        mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
    assert str(mode).lower() == "wal"


def test_foreign_keys_enforced():
    with pytest.raises(IntegrityError):
        with dbmod.session_scope() as s:
            # parent/child reference non-existent host_groups rows.
            s.add(HostGroupChild(parent_group_id=999, child_group_id=998))


def test_foreign_keys_pragma_on():
    engine = dbmod.init_engine()
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
