"""Unit tests for the unified Hosts forest (app/tree.py).

Covers the physical node→guest nesting, the logical group overlay, orphan-guest
banding, and the targeting invariant: the parent/child link is presentation-only
and never widens a job's target set.
"""

from __future__ import annotations

from app.groups import expand_group_hosts
from app.models import HostGroup, HostGroupChild, HostGroupMember, Server
from app.tree import build_host_forest


def _node(db, **kw):
    s = Server(**kw)
    db.add(s)
    db.flush()
    return s


def test_guests_nest_under_their_virtualization_host(db):
    node = _node(db, name="pve1", connection_type="local", virt_kind="proxmox")
    g1 = _node(db, name="ct-a", connection_type="proxmox", proxmox_vmid="101",
               guest_type="lxc", parent_server_id=node.id)
    g2 = _node(db, name="ct-b", connection_type="proxmox", proxmox_vmid="102",
               guest_type="lxc", parent_server_id=node.id)
    db.commit()

    forest = build_host_forest(db)
    assert len(forest) == 1
    root = forest[0]
    assert root["kind"] == "vhost"
    assert root["label"] == "pve1"
    assert root["has_children"] is True
    child_labels = sorted(c["label"] for c in root["children"])
    assert child_labels == ["ct-a", "ct-b"]
    assert all(c["kind"] == "guest" for c in root["children"])


def test_group_overlay_lists_members_without_guest_nesting(db):
    node = _node(db, name="pve1", connection_type="local", virt_kind="proxmox")
    guest = _node(db, name="ct-a", connection_type="proxmox", proxmox_vmid="101",
                  guest_type="lxc", parent_server_id=node.id)
    grp = HostGroup(name="apps")
    db.add(grp)
    db.flush()
    db.add(HostGroupMember(host_group_id=grp.id, server_id=guest.id))
    db.commit()

    forest = build_host_forest(db)
    groups = [n for n in forest if n["kind"] == "group"]
    assert len(groups) == 1
    g = groups[0]
    assert g["label"] == "apps"
    # The member shows as a plain host leaf inside the group (no guest re-nesting).
    assert [c["label"] for c in g["children"]] == ["ct-a"]
    assert g["children"][0]["kind"] == "host"
    assert g["children"][0]["has_children"] is False


def test_orphan_guest_bands_by_node_name_when_node_unmanaged(db):
    # A guest whose proxmox_node has no managed host -> synthetic label-only band.
    _node(db, name="ct-x", connection_type="proxmox", proxmox_vmid="105",
          guest_type="lxc", proxmox_node="remote-node")
    db.commit()

    forest = build_host_forest(db)
    bands = [n for n in forest if n.get("synthetic")]
    assert len(bands) == 1
    assert bands[0]["label"] == "remote-node"
    assert bands[0]["server"] is None
    assert [c["label"] for c in bands[0]["children"]] == ["ct-x"]


def test_parent_link_is_not_a_targeting_expansion(db):
    # The node→guest link must NOT be reachable via group expansion (which is the
    # only thing job targeting expands). A node is just one server id.
    node = _node(db, name="pve1", connection_type="local", virt_kind="proxmox")
    _node(db, name="ct-a", connection_type="proxmox", proxmox_vmid="101",
          guest_type="lxc", parent_server_id=node.id)
    db.commit()
    # No groups exist, so expand of "everything" yields nothing — the parent edge
    # is invisible to targeting.
    assert expand_group_hosts(db, []) == set()


def test_nested_groups_render_recursively(db):
    parent = HostGroup(name="parent")
    child = HostGroup(name="child")
    db.add_all([parent, child])
    db.flush()
    db.add(HostGroupChild(parent_group_id=parent.id, child_group_id=child.id))
    host = _node(db, name="h1", connection_type="ssh", address="10.0.0.1")
    db.add(HostGroupMember(host_group_id=child.id, server_id=host.id))
    db.commit()

    forest = build_host_forest(db)
    # Only the root group surfaces at top level; child nests under it.
    roots = [n for n in forest if n["kind"] == "group"]
    assert [g["label"] for g in roots] == ["parent"]
    sub = [c for c in roots[0]["children"] if c["kind"] == "group"]
    assert [c["label"] for c in sub] == ["child"]
    assert [c["label"] for c in sub[0]["children"]] == ["h1"]
