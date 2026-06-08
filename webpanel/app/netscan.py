"""Active network scan for unmanaged SSH hosts.

The panel sweeps the user-registered :class:`~app.models.Subnet` ranges, probes
TCP 22 on every address, and records each reachable IP as a ``new_host``
discovery (source ``network``) so it surfaces in the Discovered tab alongside
Proxmox finds. Confirming one opens the Add-host modal prefilled for ssh.

No external dependency and no privilege: a plain ``connect_ex`` to port 22 over a
thread pool. ``run_network_scan`` opens its own session (like
:func:`app.discovery.run_discovery`) so it is safe to call from the scheduler
child process or a request worker thread (``asyncio.to_thread``).
"""
from __future__ import annotations

import ipaddress
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed

from sqlalchemy import select

from .db import session_scope
from .models import Discovery, Server, Subnet, utcnow

SSH_PORT = 22
# Per-subnet and whole-scan expansion ceilings. A single spec wider than a /16
# is rejected at parse time (settings form validation); the whole-scan total is
# capped with a log line so truncation is never silent.
MAX_HOSTS_PER_SPEC = 65536
MAX_TOTAL_HOSTS = 65536


# --------------------------------------------------------------------------- #
# Target parsing (nmap-style)
# --------------------------------------------------------------------------- #
def parse_targets(spec: str) -> list[str]:
    """Expand an nmap-style target spec into a de-duplicated list of IPv4s.

    Accepts, comma-separated:
    * CIDR — ``192.168.0.0/24`` (network/broadcast excluded for /<31).
    * Per-octet wildcard — ``192.168.0.*`` or ``192.168.0-1.*``.
    * Per-octet range — ``192.168.0.1-50``.
    * A bare address — ``192.168.0.5``.

    Raises ``ValueError`` on malformed input or an expansion exceeding
    :data:`MAX_HOSTS_PER_SPEC`.
    """
    out: list[str] = []
    seen: set[str] = set()
    tokens = [t.strip() for t in spec.replace(";", ",").split(",")]
    if not any(tokens):
        raise ValueError("empty target spec")
    for token in tokens:
        if not token:
            continue
        for ip in _expand_token(token):
            if ip not in seen:
                seen.add(ip)
                out.append(ip)
                if len(out) > MAX_HOSTS_PER_SPEC:
                    raise ValueError(
                        f"target expands to more than {MAX_HOSTS_PER_SPEC} hosts"
                    )
    if not out:
        raise ValueError("target spec yields no hosts")
    return out


def _expand_token(token: str) -> list[str]:
    if "/" in token:
        net = ipaddress.ip_network(token, strict=False)
        if not isinstance(net, ipaddress.IPv4Network):
            raise ValueError("only IPv4 ranges are supported")
        if net.num_addresses > MAX_HOSTS_PER_SPEC:
            raise ValueError(
                f"target expands to more than {MAX_HOSTS_PER_SPEC} hosts"
            )
        # .hosts() drops network/broadcast for /<31; for /31 and /32 it yields
        # the usable host(s), matching what an operator expects to scan.
        return [str(ip) for ip in net.hosts()]

    octets = token.split(".")
    if len(octets) != 4:
        raise ValueError(f"invalid address spec: {token!r}")
    ranges = [_expand_octet(o) for o in octets]
    return [
        f"{a}.{b}.{c}.{d}"
        for a in ranges[0]
        for b in ranges[1]
        for c in ranges[2]
        for d in ranges[3]
    ]


def _expand_octet(part: str) -> list[int]:
    part = part.strip()
    if part == "*":
        return list(range(0, 256))
    if "-" in part:
        lo_s, hi_s = part.split("-", 1)
        lo = int(lo_s) if lo_s.strip() else 0
        hi = int(hi_s) if hi_s.strip() else 255
        if not (0 <= lo <= hi <= 255):
            raise ValueError(f"invalid octet range: {part!r}")
        return list(range(lo, hi + 1))
    n = int(part)  # raises ValueError on non-numeric
    if not 0 <= n <= 255:
        raise ValueError(f"octet out of range: {part!r}")
    return [n]


# --------------------------------------------------------------------------- #
# Port probe
# --------------------------------------------------------------------------- #
def _probe(ip: str, port: int, timeout: float) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex((ip, port)) == 0
    except OSError:
        return False
    finally:
        s.close()


def scan_port(
    ips: list[str], *, port: int = SSH_PORT, timeout: float = 1.0, concurrency: int = 256
) -> list[str]:
    """Return the subset of ``ips`` whose ``port`` accepts a TCP connection."""
    if not ips:
        return []
    open_ips: list[str] = []
    workers = max(1, min(concurrency, len(ips)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_probe, ip, port, timeout): ip for ip in ips}
        for fut in as_completed(futs):
            if fut.result():
                open_ips.append(futs[fut])
    return open_ips


def _reverse_dns(ip: str) -> str | None:
    try:
        return socket.gethostbyaddr(ip)[0] or None
    except OSError:
        return None


# --------------------------------------------------------------------------- #
# Scan + reconcile
# --------------------------------------------------------------------------- #
def run_network_scan() -> dict[str, int]:
    """Sweep enabled subnets for SSH-reachable hosts and reconcile discoveries.

    Returns ``{"scanned", "open", "new", "pending"}``. Empty inputs (no enabled
    subnets, nothing reachable) are a no-op that never prunes existing rows —
    the same defensive stance as :func:`app.discovery.run_discovery`.
    """
    stats = {"scanned": 0, "open": 0, "new": 0, "pending": 0}
    with session_scope() as db:
        subnets = db.scalars(select(Subnet).where(Subnet.enabled.is_(True))).all()
        targets: list[str] = []
        seen: set[str] = set()
        for sn in subnets:
            try:
                ips = parse_targets(sn.spec)
            except ValueError as exc:
                print(f"[netscan] skip subnet {sn.id} ({sn.spec!r}): {exc}", flush=True)
                continue
            for ip in ips:
                if ip not in seen:
                    seen.add(ip)
                    targets.append(ip)
        if not targets:
            return stats
        if len(targets) > MAX_TOTAL_HOSTS:
            print(
                f"[netscan] capping scan {len(targets)} -> {MAX_TOTAL_HOSTS} hosts",
                flush=True,
            )
            targets = targets[:MAX_TOTAL_HOSTS]
        stats["scanned"] = len(targets)

        open_ips = scan_port(targets)
        stats["open"] = len(open_ips)

        servers = db.scalars(select(Server)).all()
        # Dedup against any managed host's address so a registered IP never
        # re-surfaces as a discovery.
        managed = {s.address for s in servers if s.address}
        existing = {
            d.address: d
            for d in db.scalars(
                select(Discovery).where(
                    Discovery.kind == "new_host", Discovery.source == "network"
                )
            ).all()
            if d.address
        }

        for ip in open_ips:
            if ip in managed:
                continue
            row = existing.get(ip)
            if row is None:
                db.add(Discovery(
                    kind="new_host",
                    status="pending",
                    source="network",
                    address=ip,
                    name=_reverse_dns(ip) or ip,
                ))
                stats["new"] += 1
            elif row.status == "pending":
                row.last_seen = utcnow()

        # A pending discovery whose IP has since become a managed host is now
        # resolved — record it as confirmed history rather than leaving it.
        for ip, row in existing.items():
            if ip in managed and row.status == "pending":
                row.status = "confirmed"
                row.resolved_at = utcnow()

        db.flush()
        stats["pending"] = len(
            db.scalars(
                select(Discovery.id).where(Discovery.status == "pending")
            ).all()
        )
    return stats
