"""Unit tests for host-group graph helpers: expansion + cycle prevention."""

from __future__ import annotations

import pytest
from app import groups
from app.models import HostGroup, HostGroupChild, HostGroupMember, Server


@pytest.fixture
def graph(db):
    """A small nested graph:

    root ─┬─ web  ─ srvA
          └─ db   ─ srvB
    (root also directly holds srvC)
    """
    root, web, dbg = HostGroup(name="root"), HostGroup(name="web"), HostGroup(name="db")
    db.add_all([root, web, dbg])
    db.flush()
    a = Server(name="srvA", connection_type="local")
    b = Server(name="srvB", connection_type="local")
    c = Server(name="srvC", connection_type="local")
    db.add_all([a, b, c])
    db.flush()
    db.add_all(
        [
            HostGroupChild(parent_group_id=root.id, child_group_id=web.id),
            HostGroupChild(parent_group_id=root.id, child_group_id=dbg.id),
            HostGroupMember(host_group_id=web.id, server_id=a.id),
            HostGroupMember(host_group_id=dbg.id, server_id=b.id),
            HostGroupMember(host_group_id=root.id, server_id=c.id),
        ]
    )
    db.flush()
    return {"root": root.id, "web": web.id, "db": dbg.id, "A": a.id, "B": b.id, "C": c.id}


def test_reachable_includes_self_and_descendants(db, graph):
    reachable = groups.reachable_group_ids(db, [graph["root"]])
    assert reachable == {graph["root"], graph["web"], graph["db"]}


def test_descendant_and_ancestor(db, graph):
    assert groups.descendant_group_ids(db, graph["root"]) == {graph["web"], graph["db"]}
    assert groups.ancestor_group_ids(db, graph["web"]) == {graph["root"]}


def test_expand_group_hosts_recurses(db, graph):
    hosts = groups.expand_group_hosts(db, [graph["root"]])
    assert hosts == {graph["A"], graph["B"], graph["C"]}


def test_expand_empty_selection(db, graph):
    assert groups.expand_group_hosts(db, []) == set()


def test_effective_group_ids_for_host_orders_ancestors_first(db, graph):
    eff = groups.effective_group_ids_for_host(db, graph["A"])
    # srvA is a direct member of web, which is under root.
    assert set(eff) == {graph["web"], graph["root"]}
    assert eff.index(graph["root"]) < eff.index(graph["web"])  # parent overlays before child


def test_would_create_cycle(db, graph):
    # root already reaches web; making root a child of web would close a loop.
    assert groups.would_create_cycle(db, parent_id=graph["web"], child_id=graph["root"]) is True
    assert groups.would_create_cycle(db, parent_id=graph["web"], child_id=graph["web"]) is True
    # web -> db is a brand new, acyclic edge.
    assert groups.would_create_cycle(db, parent_id=graph["web"], child_id=graph["db"]) is False
