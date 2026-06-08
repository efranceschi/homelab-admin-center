"""Parse ansible-playbook stdout for the PLAY RECAP and reboot-required signal.

Shared by the async JobManager (panel-triggered runs) and the headless scheduler
executor so both update host state identically.
"""
from __future__ import annotations

import base64
import binascii
import json
import re

from ..ansi import strip_ansi

_RECAP_RE = re.compile(
    r"^(?P<host>\S+)\s*:\s*ok=(?P<ok>\d+).*?changed=(?P<changed>\d+).*?failed=(?P<failed>\d+)"
)


def parse_recap(text: str) -> dict[str, dict]:
    """Return {host: {ok, changed, failed}} parsed from the PLAY RECAP block."""
    out: dict[str, dict] = {}
    in_recap = False
    for line in strip_ansi(text).splitlines():
        if "PLAY RECAP" in line:
            in_recap = True
            continue
        if in_recap:
            m = _RECAP_RE.match(line.strip())
            if m:
                out[m.group("host")] = {
                    "ok": int(m.group("ok")),
                    "changed": int(m.group("changed")),
                    "failed": int(m.group("failed")),
                }
    return out


def parse_reboot(text: str) -> set[str]:
    """Return the set of hosts that reported a pending reboot."""
    hosts: set[str] = set()
    for line in strip_ansi(text).splitlines():
        if "REBOOT REQUIRED" in line:
            m = re.search(r'"msg":\s*"(\S+?):', line) or re.search(r"msg:\s*(\S+?):", line)
            if m:
                hosts.add(m.group(1))
    return hosts


def parse_hostnames(text: str) -> dict[str, str]:
    """Return ``{inventory_host: live_os_hostname}`` from the facts-probe marker.

    Mirrors :func:`parse_reboot`: the playbook's "panel facts probe" debug task
    emits ``PANEL_HOSTNAME <inventory_host> :: <hostname> :: <fqdn>``. Hosts with
    an empty gathered hostname are skipped.
    """
    out: dict[str, str] = {}
    for line in strip_ansi(text).splitlines():
        if "PANEL_HOSTNAME" in line:
            m = re.search(r"PANEL_HOSTNAME\s+(\S+)\s+::\s+(\S*)\s+::", line)
            if m and m.group(2):
                out[m.group(1)] = m.group(2)
    return out


_FACTS_RE = re.compile(r"PANEL_FACTS\s+(\S+)\s+::\s+([A-Za-z0-9+/=]+)")


def parse_facts(text: str) -> dict[str, dict]:
    """Return ``{inventory_host: {fact_key: value}}`` from the facts-probe marker.

    Mirrors :func:`parse_hostnames`: the playbook emits one
    ``PANEL_FACTS <inventory_host> :: <base64(json)>`` line per host. Base64
    keeps the payload a single contiguous, space/quote/newline-free token so the
    regex survives debug's line-wrapping and ANSI coloring. A wrapped or garbled
    line that fails to decode is skipped, never raised (old values are retained).
    """
    out: dict[str, dict] = {}
    for line in strip_ansi(text).splitlines():
        if "PANEL_FACTS" not in line:
            continue
        m = _FACTS_RE.search(line)
        if not m:
            continue
        try:
            payload = json.loads(base64.b64decode(m.group(2)))
        except (ValueError, TypeError, binascii.Error, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            out[m.group(1)] = payload
    return out


_DOCKER_RE = re.compile(r"PANEL_DOCKER\s+(\S+)\s+::\s+([A-Za-z0-9+/=]*)")
_DOCKER_SENTINEL = "__DOCKER_PRESENT__"


def parse_docker(text: str) -> dict[str, list[dict]]:
    """Return ``{inventory_host: [container, …]}`` from the docker-probe marker.

    The playbook emits one ``PANEL_DOCKER <inventory_host> :: <base64>`` line per
    host that runs Docker. The decoded payload is a ``__DOCKER_PRESENT__``
    sentinel followed by ``docker ps`` output as one ``{{json .}}`` object per
    line. A host present in the result ran Docker (possibly with zero
    containers, mapped to ``[]``); a host absent has no Docker (or was
    unreachable) — the caller uses that distinction to set/clear ``virt_kind``.
    A wrapped or garbled marker that fails to decode is skipped, never raised.
    """
    out: dict[str, list[dict]] = {}
    for line in strip_ansi(text).splitlines():
        if "PANEL_DOCKER" not in line:
            continue
        m = _DOCKER_RE.search(line)
        if not m:
            continue
        try:
            payload = base64.b64decode(m.group(2)).decode("utf-8", "replace")
        except (ValueError, binascii.Error):
            continue
        containers: list[dict] = []
        for row in payload.splitlines():
            row = row.strip()
            if not row or row == _DOCKER_SENTINEL:
                continue
            try:
                obj = json.loads(row)
            except (ValueError, json.JSONDecodeError):
                continue
            if isinstance(obj, dict):
                containers.append(obj)
        out[m.group(1)] = containers
    return out


def status_from_stats(stats: dict | None) -> str | None:
    if stats is None:
        return None
    if stats["failed"] > 0:
        return "failed"
    if stats["changed"] > 0:
        return "changed"
    return "ok"


def derive_host_state(
    mode: str, stats: dict | None, reachable: bool
) -> tuple[str, int]:
    """Derive the settled (config_status, pending_changes) of one host from a run.

    A single state, reflecting only the last execution and always overwritten:

        unreachable / no recap / failed > 0  -> ('failed', 0)
        check, changed > 0                   -> ('pending', changed)
        check, changed == 0                  -> ('ok', 0)
        apply, failed == 0                   -> ('ok', 0)   # converged

    Drift is detected in check mode: ``changed`` tasks are ones that *would*
    change, i.e. updates are pending to apply. In apply mode a clean run means
    the host just converged, so it is up to date regardless of the changed count.
    """
    if not reachable or stats is None or stats.get("failed", 0) > 0:
        return "failed", 0
    if mode == "check" and stats.get("changed", 0) > 0:
        return "pending", stats["changed"]
    return "ok", 0
