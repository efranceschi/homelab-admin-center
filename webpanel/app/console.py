"""Interactive web-console plumbing: per-host shell over a PTY.

The panel already knows how to *reach* every host type for power/check/apply
(see :mod:`app.power`). A console reuses exactly that routing, but wraps the
target command in a pseudo-terminal so the browser gets a live, interactive
shell instead of a one-shot command. The wire bridge (a WebSocket pumping bytes
both ways) lives in :mod:`app.routers.console`; this module owns:

  * the per-session registry (``ConsoleManager`` — modelled on
    :class:`app.jobs.JobManager`; single Uvicorn worker => in-memory state is
    authoritative);
  * the per-host interactive command builder (``build_console_argv``); and
  * the low-level PTY spawn / window-size helpers.

No new SSH client: for SSH hosts we shell out to the system ``ssh`` with the
stored key (the same key material :mod:`app.ansible_layer.inventory_builder`
writes), so there is one code path — a PTY around a local command.

Security model: a token is minted by an authenticated, admin-only POST and is
single-use with a short TTL; the SSH key is written 0600 into a per-session run
dir and deleted on teardown; the child runs in its own session so killing it
reaps the whole tree (ssh + remote, ``pct enter``, ``docker exec``).
"""
from __future__ import annotations

import asyncio
import fcntl
import os
import pty
import secrets
import shutil
import signal
import struct
import subprocess
import termios
import time
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from . import config, proxmox
from .models import DockerContainer, Server
from .power import _ssh_key_for

# One-time token lifetime: the popup must open its WebSocket within this window.
TOKEN_TTL_SECONDS = 30
# Hard cap on concurrent live consoles (bounds runaway PTYs).
MAX_SESSIONS = 16
# Root of the per-session ephemeral run dirs (key material lives here).
CONSOLE_RUN_ROOT = config.RUN_DIRS / "console"
# How long to wait after SIGTERM before SIGKILL when tearing a session down.
_TERM_GRACE_SECONDS = 3


class ConsoleError(RuntimeError):
    """A console could not be issued/started (bad target, missing creds, …).

    Surfaced to the POST caller as an HTTP 400."""


# --- per-host interactive command builder -----------------------------------

def _ssh_argv(host: Server, run_dir: Path) -> list[str]:
    """An interactive ``ssh`` argv for `host` (login shell, no remote command).

    ``-tt`` forces a remote PTY; ``BatchMode`` is intentionally omitted (unlike
    the non-interactive job path) so a legitimate prompt isn't fatally swallowed.
    """
    key_file = _ssh_key_for(host, run_dir)  # raises if no credential configured
    argv = [
        "ssh", "-tt", "-i", str(key_file),
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=10",
    ]
    if host.port:
        argv += ["-p", str(host.port)]
    target = f"{host.ssh_user}@{host.address}" if host.ssh_user else (host.address or host.name)
    return argv + [target]


def _wrap_interactive(host: Server, inner: list[str], run_dir: Path) -> list[str]:
    """Run an interactive `inner` command on `host` (local / pct exec / ssh).

    Mirrors :func:`app.power._wrap_for_host` but for interactive use: the SSH
    case forces a TTY (``-tt``) and drops ``BatchMode`` so ``docker exec -it``
    over SSH still gets a terminal.
    """
    ct = host.connection_type
    if ct == "local":
        return proxmox._sudo_argv(*inner)
    if ct == "proxmox":
        if not host.proxmox_vmid:
            raise ConsoleError(f"{host.name}: missing VMID for pct exec")
        pct = proxmox.pct_path()
        if not pct:
            raise ConsoleError("pct binary not found on this node")
        return proxmox._sudo_argv(pct, "exec", host.proxmox_vmid, "--", *inner)
    if ct == "ssh":
        return _ssh_argv(host, run_dir)[:-1] + [
            (f"{host.ssh_user}@{host.address}" if host.ssh_user else (host.address or host.name)),
            "--", *inner,
        ]
    raise ConsoleError(f"{host.name}: unsupported connection type {ct!r}")


def _server_console_argv(srv: Server, run_dir: Path) -> list[str]:
    """Interactive shell argv for a managed Server."""
    ct = srv.connection_type
    if ct == "local":
        # Unprivileged shell on the Proxmox node as the panel's own user (`hac`):
        # NO sudo — least privilege, and the panel can signal it directly. The
        # admin escalates with `sudo pct …` inside if needed. (Falls back to sh
        # only if bash is somehow absent, which it never is on a PVE node.)
        return ["/bin/sh", "-c", "exec /bin/bash -i || exec /bin/sh -i"]
    if ct == "ssh":
        return _ssh_argv(srv, run_dir)
    if ct == "proxmox":
        if srv.guest_type == "lxc":
            pct = proxmox.pct_path()
            if not pct:
                raise ConsoleError("pct binary not found on this node")
            if not srv.proxmox_vmid:
                raise ConsoleError(f"{srv.name}: missing VMID")
            return proxmox._sudo_argv(pct, "enter", srv.proxmox_vmid)
        raise ConsoleError(
            f"{srv.name}: no local console for guest_type={srv.guest_type!r}; "
            "register it as an SSH host to open a console"
        )
    raise ConsoleError(f"{srv.name}: unsupported connection type {ct!r}")


def build_console_argv(
    db: Session, *, kind: str, target_id, run_dir: Path,
) -> tuple[list[str], str]:
    """Return ``(argv, label)`` for an interactive console on a target.

    ``kind``: ``"server"`` (Server id) or ``"docker"`` (DockerContainer id).
    Writes any required SSH key 0600 into ``run_dir``. Raises ``ConsoleError``.
    """
    if kind == "server":
        srv = db.get(Server, int(target_id))
        if srv is None:
            raise ConsoleError("host not found")
        return _server_console_argv(srv, run_dir), srv.name

    if kind == "docker":
        c = db.get(DockerContainer, int(target_id))
        if c is None:
            raise ConsoleError("container not found")
        host = db.get(Server, c.host_server_id)
        if host is None:
            raise ConsoleError("docker host not found")
        # Prefer an interactive bash inside the container, fall back to sh; -it
        # allocates the container-side TTY. (No `2>/dev/null`: redirecting stderr
        # off the tty would make bash decide it is non-interactive — no prompt.)
        inner = [
            "docker", "exec", "-it", c.container_id,
            "sh", "-c", "exec /bin/bash -i || exec /bin/sh -i",
        ]
        return _wrap_interactive(host, inner, run_dir), (c.name or c.container_id[:12])

    raise ConsoleError(f"unknown console kind: {kind!r}")


# --- PTY helpers ------------------------------------------------------------

def set_winsize(fd: int, rows: int, cols: int) -> None:
    """Push a terminal size onto the PTY master; the kernel raises SIGWINCH."""
    rows = max(1, min(rows, 1000))
    cols = max(1, min(cols, 1000))
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def spawn_pty(
    argv: list[str], *, cwd: str, rows: int = 24, cols: int = 80,
) -> tuple[int, subprocess.Popen]:
    """Spawn `argv` attached to a fresh PTY; return ``(master_fd, proc)``.

    The child starts its own session (``setsid``) AND makes the slave its
    controlling terminal (``TIOCSCTTY``). The controlling tty is essential:
    without it ``pct enter`` / ``lxc-attach`` and job-control shells get no
    prompt and never process input. ``setsid`` also gives the child its own
    pgid so a ``killpg`` on teardown reaps the whole tree. The master fd is
    non-blocking so the event-loop reader never stalls.
    """
    master_fd, slave_fd = pty.openpty()

    def _setup_child() -> None:
        # New session, then claim the slave (fd 0) as the controlling terminal.
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    try:
        set_winsize(master_fd, rows, cols)
        env = dict(os.environ)
        env["TERM"] = "xterm-256color"
        proc = subprocess.Popen(
            argv,
            stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
            preexec_fn=_setup_child, close_fds=True, cwd=cwd, env=env,
        )
    finally:
        os.close(slave_fd)
    os.set_blocking(master_fd, False)
    return master_fd, proc


def reap(session: "ConsoleSession") -> None:
    """Tear down a live session's child process and PTY.

    **Close the master fd FIRST.** The LXC/docker shells run as *root* in a
    separate session (via ``sudo``'s ``use_pty`` / ``pct enter``), so the
    unprivileged ``hac`` panel cannot signal them, and interactive bash ignores
    ``SIGTERM``. Closing the master delivers ``SIGHUP`` down the tty chain
    (through sudo's own pty) and the root shell exits — the only reliable kill.
    Then escalate ``killpg`` on the parts we *can* signal (sudo/pct/ssh) as a
    fallback, and reap. Blocking — callers on the event loop run it in an
    executor.
    """
    fd = session.master_fd
    session.master_fd = None
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass
    proc = session.proc
    if proc is None:
        return
    # Let the HUP propagate before escalating.
    deadline = time.monotonic() + _TERM_GRACE_SECONDS
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if proc.poll() is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        return
    for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except OSError:
            return
        try:
            proc.wait(timeout=1)
            return
        except subprocess.TimeoutExpired:
            continue


# --- session registry -------------------------------------------------------

@dataclass
class ConsoleSession:
    token: str
    kind: str
    target_id: object
    user_id: int
    label: str
    argv: list[str]
    run_dir: Path
    claimed: bool = False
    proc: subprocess.Popen | None = None
    master_fd: int | None = None
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    # Set to ask the WebSocket bridge to tear itself down (SIGHUP drain). The
    # bridge waits on this, so teardown runs on its own loop — no cross-thread
    # fd-close race from begin_drain().
    close_requested: asyncio.Event = field(default_factory=asyncio.Event)


class ConsoleManager:
    """In-memory registry of live console sessions (single-worker authoritative)."""

    def __init__(self) -> None:
        self._sessions: dict[str, ConsoleSession] = {}
        self._draining = False

    # --- introspection ---
    def active_count(self) -> int:
        return len(self._sessions)

    def is_draining(self) -> bool:
        return self._draining

    def peek(self, token: str) -> ConsoleSession | None:
        """Look up a session without claiming it (for the page's title)."""
        return self._sessions.get(token)

    # --- lifecycle ---
    def issue(self, db: Session, *, user_id: int, kind: str, target_id) -> ConsoleSession:
        """Mint a one-time session: resolve the argv, stage key material, arm TTL.

        Raises ``ConsoleError`` on a bad target/missing creds, or when the panel
        is draining / at the session cap."""
        if self._draining:
            raise ConsoleError("Panel is restarting; consoles are temporarily unavailable.")
        if len(self._sessions) >= MAX_SESSIONS:
            raise ConsoleError("Too many active consoles; close one and retry.")
        token = secrets.token_urlsafe(32)
        run_dir = CONSOLE_RUN_ROOT / token
        run_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(run_dir, 0o700)
        try:
            argv, label = build_console_argv(db, kind=kind, target_id=target_id, run_dir=run_dir)
        except Exception:
            shutil.rmtree(run_dir, ignore_errors=True)
            raise
        session = ConsoleSession(
            token=token, kind=kind, target_id=target_id, user_id=user_id,
            label=label, argv=argv, run_dir=run_dir,
        )
        self._sessions[token] = session
        try:
            loop = asyncio.get_running_loop()
            loop.call_later(TOKEN_TTL_SECONDS, self._expire_unclaimed, token)
        except RuntimeError:
            pass  # no loop (shouldn't happen on the request path)
        return session

    def claim(self, token: str, user_id: int) -> ConsoleSession | None:
        """One-shot claim by the issuing user; ``None`` if invalid/used/foreign."""
        session = self._sessions.get(token)
        if session is None or session.claimed or session.user_id != user_id:
            return None
        session.claimed = True
        return session

    def _expire_unclaimed(self, token: str) -> None:
        session = self._sessions.get(token)
        if session is not None and not session.claimed:
            self.close(token)

    def close(self, token: str) -> None:
        """Drop a session from the registry and remove its key material.

        Idempotent. The child process + PTY are torn down by :func:`reap` (run by
        the WebSocket bridge); for a session that was never claimed (TTL expiry)
        there is no child, so this is just the run-dir cleanup."""
        session = self._sessions.pop(token, None)
        if session is None:
            return
        reap(session)  # no-op fast path once the bridge has already reaped
        shutil.rmtree(session.run_dir, ignore_errors=True)
        session.closed.set()

    def begin_drain(self) -> None:
        """Refuse new consoles and ask every live one to close (SIGHUP restart).

        Sets each session's ``close_requested`` event; the bridge waits on it and
        tears itself down (HUP via :func:`reap`). Runs on the loop, so it never
        blocks and there is no cross-thread fd-close race. Consoles never
        participate in the job drain, so they can't extend the restart window."""
        self._draining = True
        for token in list(self._sessions):
            session = self._sessions.get(token)
            if session is not None:
                session.close_requested.set()


manager = ConsoleManager()
