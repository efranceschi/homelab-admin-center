"""Unit tests for Docker container support.

Covers parsing the ``PANEL_DOCKER`` probe marker, the read-only sync into the
``docker_containers`` table (upsert / prune / auto-mark / clear), and rendering
containers as display-only leaves in the Hosts forest without ever becoming a
job target.
"""

from __future__ import annotations

import base64
import json

from sqlalchemy import select

from app import docker
from app.ansible_layer.results import parse_docker
from app.groups import expand_group_hosts
from app.models import DockerContainer, Server
from app.tree import build_host_forest


def _marker(host: str, containers: list[dict] | None) -> str:
    """Build a PANEL_DOCKER log line. ``None`` => docker absent (no marker)."""
    if containers is None:
        return f"PLAY [whatever] on {host}\n"
    payload = "__DOCKER_PRESENT__\n" + "\n".join(json.dumps(c) for c in containers)
    b64 = base64.b64encode(payload.encode()).decode()
    return f"ok: [{host}] => msg: PANEL_DOCKER {host} :: {b64}"


def _container(cid: str, name: str, **kw) -> dict:
    base = {
        "ID": cid, "Names": name, "Image": "img:latest",
        "State": "running", "Status": "Up 3 hours", "Ports": "", "Labels": "",
    }
    base.update(kw)
    return base


def _host(db, **kw) -> Server:
    s = Server(**kw)
    db.add(s)
    db.flush()
    return s


# --------------------------------------------------------------------------- #
# parse_docker
# --------------------------------------------------------------------------- #
def test_parse_docker_decodes_container_rows():
    text = _marker("komodo", [_container("abc123", "web"), _container("def456", "db")])
    out = parse_docker(text)
    assert set(out) == {"komodo"}
    assert [c["Names"] for c in out["komodo"]] == ["web", "db"]


def test_parse_docker_present_with_zero_containers_maps_to_empty_list():
    # Sentinel only -> docker installed but nothing running.
    out = parse_docker(_marker("komodo", []))
    assert out == {"komodo": []}


def test_parse_docker_absent_host_not_in_result():
    out = parse_docker(_marker("plain", None))
    assert out == {}


def test_parse_docker_skips_garbled_payload():
    out = parse_docker("PANEL_DOCKER komodo :: not-valid-base64!!!")
    # Non-base64 chars stop the regex match -> nothing parsed, never raises.
    assert out == {}


# --------------------------------------------------------------------------- #
# sync_containers
# --------------------------------------------------------------------------- #
def test_sync_creates_rows_and_auto_marks_host(db):
    host = _host(db, name="komodo", connection_type="proxmox", proxmox_vmid="120")
    docker.sync_containers(
        db, [host],
        {"komodo": [_container("abc", "web"), _container("def", "db")]},
        ["komodo"],
    )
    db.commit()
    rows = db.scalars(
        select(DockerContainer).where(DockerContainer.host_server_id == host.id)
    ).all()
    assert sorted(r.name for r in rows) == ["db", "web"]
    assert db.get(Server, host.id).virt_kind == "docker"


def test_sync_upserts_and_prunes(db):
    host = _host(db, name="komodo", connection_type="proxmox", proxmox_vmid="120")
    docker.sync_containers(db, [host], {"komodo": [_container("abc", "web")]}, ["komodo"])
    db.commit()
    # 'web' restarts (same ID, new status); 'db' appears; nothing removed yet.
    docker.sync_containers(
        db, [host],
        {"komodo": [
            _container("abc", "web", Status="Up 1 minute"),
            _container("def", "db"),
        ]},
        ["komodo"],
    )
    db.commit()
    rows = {r.container_id: r for r in db.scalars(select(DockerContainer)).all()}
    assert set(rows) == {"abc", "def"}
    assert rows["abc"].status == "Up 1 minute"
    # 'def' goes away -> pruned.
    docker.sync_containers(db, [host], {"komodo": [_container("abc", "web")]}, ["komodo"])
    db.commit()
    assert {r.container_id for r in db.scalars(select(DockerContainer)).all()} == {"abc"}


def test_sync_clears_mark_when_reachable_host_stops_running_docker(db):
    host = _host(db, name="komodo", connection_type="proxmox", proxmox_vmid="120")
    docker.sync_containers(db, [host], {"komodo": [_container("abc", "web")]}, ["komodo"])
    db.commit()
    # Reachable (in hostnames) but no docker marker -> clear mark + drop rows.
    docker.sync_containers(db, [host], {}, ["komodo"])
    db.commit()
    assert db.get(Server, host.id).virt_kind is None
    assert db.scalars(select(DockerContainer)).all() == []


def test_sync_unreachable_host_keeps_last_known_containers(db):
    host = _host(db, name="komodo", connection_type="proxmox", proxmox_vmid="120")
    docker.sync_containers(db, [host], {"komodo": [_container("abc", "web")]}, ["komodo"])
    db.commit()
    # Not reachable this pass (empty reachable set) -> retain, do not clear.
    docker.sync_containers(db, [host], {}, [])
    db.commit()
    assert db.get(Server, host.id).virt_kind == "docker"
    assert len(db.scalars(select(DockerContainer)).all()) == 1


def test_sync_preserves_proxmox_virt_kind(db):
    # A Proxmox node that also runs docker keeps its 'proxmox' kind.
    node = _host(db, name="pve1", connection_type="local", virt_kind="proxmox")
    docker.sync_containers(db, [node], {"pve1": [_container("abc", "web")]}, ["pve1"])
    db.commit()
    assert db.get(Server, node.id).virt_kind == "proxmox"
    assert len(db.scalars(select(DockerContainer)).all()) == 1


def test_sync_extracts_compose_project(db):
    host = _host(db, name="komodo", connection_type="proxmox", proxmox_vmid="120")
    c = _container("abc", "web", Labels="foo=bar,com.docker.compose.project=stackx,x=y")
    docker.sync_containers(db, [host], {"komodo": [c]}, ["komodo"])
    db.commit()
    row = db.scalars(select(DockerContainer)).one()
    assert row.compose_project == "stackx"


# --------------------------------------------------------------------------- #
# tree rendering
# --------------------------------------------------------------------------- #
def test_docker_containers_render_as_host_children(db):
    host = _host(db, name="komodo", connection_type="proxmox", proxmox_vmid="120")
    db.add(DockerContainer(host_server_id=host.id, container_id="abc", name="web",
                           image="nginx", state="running", status="Up 2h"))
    db.add(DockerContainer(host_server_id=host.id, container_id="def", name="db",
                           image="postgres", state="exited", status="Exited (0)"))
    db.commit()

    forest = build_host_forest(db)
    root = next(n for n in forest if n["label"] == "komodo")
    assert root["has_children"] is True
    kids = [c for c in root["children"] if c["kind"] == "docker"]
    assert sorted(c["label"] for c in kids) == ["db", "web"]
    assert all(c["server"] is None and c["has_children"] is False for c in kids)


def test_docker_containers_are_not_a_targeting_expansion(db):
    host = _host(db, name="komodo", connection_type="proxmox", proxmox_vmid="120")
    db.add(DockerContainer(host_server_id=host.id, container_id="abc", name="web"))
    db.commit()
    # Containers live outside `servers`, so they can never widen a target set.
    assert expand_group_hosts(db, []) == set()
    assert db.scalars(select(Server)).all() == [host]


# --------------------------------------------------------------------------- #
# stack (compose project) grouping
# --------------------------------------------------------------------------- #
def _dc(host_id, cid, name, project=None, state="running", **kw):
    return DockerContainer(host_server_id=host_id, container_id=cid, name=name,
                           compose_project=project, state=state, **kw)


def test_compose_containers_group_under_one_stack_node(db):
    host = _host(db, name="komodo", connection_type="proxmox", proxmox_vmid="120")
    db.add(_dc(host.id, "a1", "web", project="blog"))
    db.add(_dc(host.id, "a2", "db", project="blog"))
    db.commit()

    root = next(n for n in build_host_forest(db) if n["label"] == "komodo")
    stacks = [c for c in root["children"] if c["kind"] == "stack"]
    assert [s["label"] for s in stacks] == ["blog"]
    stack = stacks[0]
    assert stack["server"] is None and stack["has_children"] is True
    # Containers nest under the stack, not directly under the host.
    assert sorted(c["label"] for c in stack["children"]) == ["db", "web"]
    assert all(c["kind"] == "docker" for c in stack["children"])
    assert [c for c in root["children"] if c["kind"] == "docker"] == []


def test_standalone_containers_render_directly_under_host(db):
    host = _host(db, name="komodo", connection_type="proxmox", proxmox_vmid="120")
    db.add(_dc(host.id, "s1", "loner", project=None))
    db.add(_dc(host.id, "a1", "web", project="blog"))
    db.commit()

    root = next(n for n in build_host_forest(db) if n["label"] == "komodo")
    kinds = {c["kind"] for c in root["children"]}
    assert kinds == {"stack", "docker"}
    leaves = [c for c in root["children"] if c["kind"] == "docker"]
    assert [c["label"] for c in leaves] == ["loner"]


def test_stack_rollup_counts_running_over_total(db):
    host = _host(db, name="komodo", connection_type="proxmox", proxmox_vmid="120")
    db.add(_dc(host.id, "a1", "web", project="blog", state="running"))
    db.add(_dc(host.id, "a2", "db", project="blog", state="exited"))
    db.add(_dc(host.id, "a3", "cache", project="blog", state="running"))
    db.commit()

    root = next(n for n in build_host_forest(db) if n["label"] == "komodo")
    stack = next(c for c in root["children"] if c["kind"] == "stack")
    assert stack["stack_total"] == 3
    assert stack["stack_running"] == 2


def test_multiple_stacks_sorted_alphabetically(db):
    host = _host(db, name="komodo", connection_type="proxmox", proxmox_vmid="120")
    db.add(_dc(host.id, "z1", "z", project="zeta"))
    db.add(_dc(host.id, "a1", "a", project="alpha"))
    db.commit()

    root = next(n for n in build_host_forest(db) if n["label"] == "komodo")
    stacks = [c["label"] for c in root["children"] if c["kind"] == "stack"]
    assert stacks == ["alpha", "zeta"]
