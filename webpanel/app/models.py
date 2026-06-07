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
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    credential: Mapped[Credential | None] = relationship()
    groups: Mapped[list["HostGroup"]] = relationship(
        secondary="host_group_members", back_populates="servers"
    )
    state: Mapped["HostState | None"] = relationship(
        back_populates="server", cascade="all, delete-orphan", uselist=False
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
    # Configuration drift state, derived from --check runs (NULL = unknown).
    config_status: Mapped[str | None] = mapped_column(String(16))  # updated|out_of_date|unknown
    config_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pending_changes: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    server: Mapped[Server] = relationship(back_populates="state")


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
