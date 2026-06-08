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


def test_migrate_backfills_virtualization_tree():
    """The v9 backfill links existing proxmox guests to the local node and tags
    types/virt_kind — idempotent, only touching NULLs."""
    from app.models import Server

    with dbmod.session_scope() as s:
        node = Server(name="pve1", connection_type="local")
        guest = Server(name="ct-a", connection_type="proxmox", proxmox_vmid="101")
        s.add_all([node, guest])

    engine = dbmod.init_engine()
    dbmod._migrate(engine)

    with dbmod.session_scope() as s:
        node = s.scalar(text_select_by_name(s, "pve1"))
        guest = s.scalar(text_select_by_name(s, "ct-a"))
        assert node.virt_kind == "proxmox"
        assert guest.guest_type == "lxc"
        assert guest.parent_server_id == node.id


def text_select_by_name(session, name):
    from app.models import Server
    from sqlalchemy import select

    return select(Server).where(Server.name == name)


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
