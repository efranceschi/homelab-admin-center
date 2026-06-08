"""SQLAlchemy 2.0 ORM models for the control panel database.

The schema covers users, hosts (across local/ssh/proxmox connection types),
host groups, the installed-plugin registry, per-scope plugin configuration,
encrypted credentials, jobs, cached host state, app settings, and an audit log.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(16), default="admin")  # admin | viewer
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Credential(Base):
    """A secret blob, encrypted at rest with Fernet (see crypto.SecretBox)."""

    __tablename__ = "credentials"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    # ssh_key | proxmox_token | vault_password | password
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    secret_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)  # ENCRYPTED
    meta_json: Mapped[str] = mapped_column(Text, default="{}")  # non-secret metadata
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Server(Base):
    __tablename__ = "servers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    # local | ssh | proxmox
    connection_type: Mapped[str] = mapped_column(String(16), nullable=False)
    address: Mapped[str | None] = mapped_column(String(255))
    port: Mapped[int | None] = mapped_column(Integer)
    ssh_user: Mapped[str | None] = mapped_column(String(64))
    credential_id: Mapped[int | None] = mapped_column(ForeignKey("credentials.id"))
    proxmox_node: Mapped[str | None] = mapped_column(String(64))
    proxmox_vmid: Mapped[str | None] = mapped_column(String(16))
    proxmox_endpoint: Mapped[str | None] = mapped_column(String(255))
    # Physical parent in the virtualization tree: a guest (LXC/QEMU) points at
    # the host that runs it (a Proxmox node). Presentation-only — it is NEVER a
    # job-targeting expansion (see app/tree.py and ansible_layer/service.py), so
    # a node's Check/Apply never reaches its guests. SET NULL keeps guests alive
    # when their node host is deleted (also enforced in the delete route, since
    # SQLite cannot add an FK action via ALTER on an upgraded DB).
    parent_server_id: Mapped[int | None] = mapped_column(
        ForeignKey("servers.id", ondelete="SET NULL")
    )
    # Virtualization capability: a non-NULL value marks a host that can nest
    # guests (proxmox node today; docker later). Drives the tree expander/badge
    # even before any guest exists.
    virt_kind: Mapped[str | None] = mapped_column(String(16))  # proxmox | docker
    guest_type: Mapped[str | None] = mapped_column(String(8))  # lxc | qemu
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    credential: Mapped[Credential | None] = relationship()
    parent: Mapped["Server | None"] = relationship(
        remote_side="Server.id", back_populates="children"
    )
    children: Mapped[list["Server"]] = relationship(back_populates="parent")
    groups: Mapped[list["HostGroup"]] = relationship(
        secondary="host_group_members", back_populates="servers"
    )
    state: Mapped["HostState | None"] = relationship(
        back_populates="server", cascade="all, delete-orphan", uselist=False
    )
    events: Mapped[list["HostEvent"]] = relationship(
        cascade="all, delete-orphan",
        order_by="HostEvent.created_at.desc()",
    )
    inventory: Mapped[list["HostInventory"]] = relationship(
        cascade="all, delete-orphan",
        order_by="HostInventory.key",
    )
    docker_containers: Mapped[list["DockerContainer"]] = relationship(
        cascade="all, delete-orphan",
        order_by="DockerContainer.name",
    )


class HostGroup(Base):
    __tablename__ = "host_groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    servers: Mapped[list[Server]] = relationship(
        secondary="host_group_members", back_populates="groups"
    )
    # Nested groups: a group may contain other groups (children) and be
    # contained by other groups (parents). Cycle prevention lives in app/groups.py.
    children: Mapped[list["HostGroup"]] = relationship(
        secondary="host_group_children",
        primaryjoin="HostGroup.id == HostGroupChild.parent_group_id",
        secondaryjoin="HostGroup.id == HostGroupChild.child_group_id",
        back_populates="parents",
    )
    parents: Mapped[list["HostGroup"]] = relationship(
        secondary="host_group_children",
        primaryjoin="HostGroup.id == HostGroupChild.child_group_id",
        secondaryjoin="HostGroup.id == HostGroupChild.parent_group_id",
        back_populates="children",
    )


class HostGroupMember(Base):
    __tablename__ = "host_group_members"
    __table_args__ = (UniqueConstraint("host_group_id", "server_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    host_group_id: Mapped[int] = mapped_column(
        ForeignKey("host_groups.id", ondelete="CASCADE")
    )
    server_id: Mapped[int] = mapped_column(
        ForeignKey("servers.id", ondelete="CASCADE")
    )


class HostGroupChild(Base):
    """Self-referential edge: ``parent_group`` contains ``child_group``."""

    __tablename__ = "host_group_children"
    __table_args__ = (UniqueConstraint("parent_group_id", "child_group_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    parent_group_id: Mapped[int] = mapped_column(
        ForeignKey("host_groups.id", ondelete="CASCADE")
    )
    child_group_id: Mapped[int] = mapped_column(
        ForeignKey("host_groups.id", ondelete="CASCADE")
    )


class Plugin(Base):
    """Installed-plugin registry. One row per discovered plugin folder."""

    __tablename__ = "plugins"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    ansible_tags: Mapped[str] = mapped_column(String(255), default="")  # comma list
    role_name: Mapped[str] = mapped_column(String(128), default="")
    description: Mapped[str | None] = mapped_column(Text)
    schema_json: Mapped[str] = mapped_column(Text, default="{}")
    supported_connections: Mapped[str] = mapped_column(
        String(64), default="local,ssh,proxmox"
    )
    enable_var: Mapped[str | None] = mapped_column(String(128))
    supports_check_mode: Mapped[bool] = mapped_column(Boolean, default=True)
    order: Mapped[int] = mapped_column(Integer, default=100)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[str | None] = mapped_column(String(32))
    installed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class PluginConfig(Base):
    """Global / group / host scoped variable values for a plugin."""

    __tablename__ = "plugin_configs"
    __table_args__ = (UniqueConstraint("plugin_id", "scope", "scope_ref_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    plugin_id: Mapped[int] = mapped_column(
        ForeignKey("plugins.id", ondelete="CASCADE"), nullable=False
    )
    scope: Mapped[str] = mapped_column(String(16), default="global")  # global|group|host
    scope_ref_id: Mapped[int | None] = mapped_column(Integer)
    config_json: Mapped[str] = mapped_column(Text, default="{}")
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    # queued | running | success | failed | cancelled
    status: Mapped[str] = mapped_column(String(16), default="queued")
    mode: Mapped[str] = mapped_column(String(8), default="check")  # check | apply
    # ansible | power — a power job runs a pct/qm/docker lifecycle command and
    # skips the ansible-recap finalize (no config-state writes, no docker resync).
    kind: Mapped[str] = mapped_column(String(16), default="ansible")
    target_type: Mapped[str] = mapped_column(String(16), default="host")  # all|group|host
    target_ref: Mapped[str | None] = mapped_column(String(255))
    plugin_tags: Mapped[str | None] = mapped_column(String(255))
    # Original selection, persisted so a failed job can be retried verbatim.
    server_ids: Mapped[str | None] = mapped_column(String(512))  # csv of server ids
    plugin_ids: Mapped[str | None] = mapped_column(String(512))  # csv of plugin keys
    group_ids: Mapped[str | None] = mapped_column(String(512))  # csv of group ids (pre-expansion)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    return_code: Mapped[int | None] = mapped_column(Integer)
    log_path: Mapped[str | None] = mapped_column(Text)
    # Full captured output, persisted on completion so the log survives the
    # rotation/housekeeping of the on-disk run directory. Cleared for old jobs.
    log_text: Mapped[str | None] = mapped_column(Text)
    pid: Mapped[int | None] = mapped_column(Integer)
    triggered_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class HostState(Base):
    __tablename__ = "host_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(
        ForeignKey("servers.id", ondelete="CASCADE"), unique=True
    )
    last_job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"))
    last_status: Mapped[str | None] = mapped_column(String(16))  # ok|changed|failed
    reboot_required: Mapped[bool] = mapped_column(Boolean, default=False)
    facts_json: Mapped[str] = mapped_column(Text, default="{}")
    plugin_state_json: Mapped[str] = mapped_column(Text, default="{}")
    # Settled host state from the last run, always overwritten (NULL = never run).
    config_status: Mapped[str | None] = mapped_column(String(16))  # ok|pending|failed
    config_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pending_changes: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    server: Mapped[Server] = relationship(back_populates="state")


class HostEvent(Base):
    """A timestamped tracking event for a single host (its history timeline).

    Records configuration check/apply outcomes and Proxmox container renames so
    the host detail page can show what happened and when. Distinct from the
    global :class:`AuditLog` (keyed by server id, survives renames, host-scoped).
    """

    __tablename__ = "host_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(
        ForeignKey("servers.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # check|apply|name_sync
    status: Mapped[str | None] = mapped_column(String(16))  # ok|changed|failed|pending|updated|out_of_date
    message: Mapped[str] = mapped_column(Text, default="")
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class HostInventory(Base):
    """One curated fact per host, with last-change tracking (no full history).

    Populated from the playbook's ``PANEL_FACTS`` probe on every run. Only the
    LAST change is kept: ``previous_value`` + ``changed_at`` are set when (and
    only when) the gathered value actually differs from the stored one. STABLE
    facts only — volatile ones (uptime, free mem, …) are excluded upstream so a
    value never churns spuriously per run.
    """

    __tablename__ = "host_inventory"
    __table_args__ = (UniqueConstraint("server_id", "key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(
        ForeignKey("servers.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(64), nullable=False)  # e.g. "kernel"
    value: Mapped[str] = mapped_column(Text, default="")  # current value
    previous_value: Mapped[str | None] = mapped_column(Text)  # value before last change
    changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow  # last seen/refreshed
    )


class DockerContainer(Base):
    """A Docker container running on a docker host, for display only.

    Populated from the playbook's ``PANEL_DOCKER`` probe (``docker ps``) and
    synced read-only: rows are upserted by ``container_id`` and pruned when the
    container disappears. These are NOT Ansible-managed — no check/apply, no
    credentials, no groups, no facts. They render as leaf children of the host
    that runs Docker (see ``app/tree.py``), the docker analogue of a Proxmox
    node's LXC/QEMU guests.
    """

    __tablename__ = "docker_containers"
    __table_args__ = (UniqueConstraint("host_server_id", "container_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    host_server_id: Mapped[int] = mapped_column(
        ForeignKey("servers.id", ondelete="CASCADE"), nullable=False
    )
    container_id: Mapped[str] = mapped_column(String(64), nullable=False)  # docker ID
    name: Mapped[str] = mapped_column(String(128), default="")
    image: Mapped[str] = mapped_column(String(255), default="")
    state: Mapped[str] = mapped_column(String(32), default="")  # running | exited | …
    status: Mapped[str] = mapped_column(String(128), default="")  # "Up 3 hours"
    ports: Mapped[str] = mapped_column(Text, default="")  # textual summary
    compose_project: Mapped[str | None] = mapped_column(String(128))
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Schedule(Base):
    """A recurring run definition, executed by the scheduler child process.

    Replaces the legacy cron entry — scheduling is owned by the application.
    """

    __tablename__ = "schedules"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # 'interval' (every N minutes) or 'daily' (at HH:MM local time).
    kind: Mapped[str] = mapped_column(String(16), default="daily")
    interval_minutes: Mapped[int | None] = mapped_column(Integer)
    daily_time: Mapped[str | None] = mapped_column(String(5))  # "HH:MM"
    mode: Mapped[str] = mapped_column(String(8), default="apply")  # check | apply
    # Targets: comma-separated server ids, or empty = all enabled servers.
    server_ids: Mapped[str] = mapped_column(String(512), default="")
    # Comma-separated plugin keys, or empty = all enabled plugins.
    plugin_ids: Mapped[str] = mapped_column(String(512), default="")
    # Comma-separated group ids; expanded (recursively) to member hosts at run.
    group_ids: Mapped[str] = mapped_column(String(512), default="")
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Discovery(Base):
    """A change the panel detected about the fleet, awaiting a decision.

    Generalizes the old discovered-hosts table. ``kind`` distinguishes a brand
    new unmanaged host (``new_host``) from a hostname change on an existing one
    (``name_change``); ``status`` tracks the lifecycle (``pending`` ->
    ``confirmed`` | ``ignored``). Confirming applies the change (register the
    host, or rename it); ignoring records the decision so a recurring identical
    event is a no-op. Confirmed/ignored rows are retained as history.

    Dedup is done in application code (see :mod:`app.discovery`), so there is no
    unique constraint — the indexes below only speed the per-source / per-server
    lookups. ``status_text`` holds the legacy running/stopped container state
    (it was named ``status`` before this column carried the lifecycle).
    """

    __tablename__ = "discoveries"
    __table_args__ = (
        Index("ix_discoveries_source_vmid", "source", "proxmox_vmid"),
        Index("ix_discoveries_server_status", "server_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), default="new_host")  # new_host | name_change
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | confirmed | ignored
    source: Mapped[str] = mapped_column(String(16), default="proxmox")  # proxmox | ssh | local
    name: Mapped[str | None] = mapped_column(String(128))  # display label

    # new_host fields
    proxmox_node: Mapped[str | None] = mapped_column(String(64))
    proxmox_vmid: Mapped[str | None] = mapped_column(String(16))
    guest_type: Mapped[str | None] = mapped_column(String(8))  # lxc | qemu
    status_text: Mapped[str | None] = mapped_column(String(32))  # running | stopped

    # name_change fields (the host is already known)
    server_id: Mapped[int | None] = mapped_column(
        ForeignKey("servers.id", ondelete="CASCADE")
    )
    old_name: Mapped[str | None] = mapped_column(String(128))
    new_name: Mapped[str | None] = mapped_column(String(128))

    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    value_type: Mapped[str] = mapped_column(String(16), default="str")


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str | None] = mapped_column(String(255))
    detail_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
