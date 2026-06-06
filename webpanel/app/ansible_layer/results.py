"""Parse ansible-playbook stdout for the PLAY RECAP and reboot-required signal.

Shared by the async JobManager (panel-triggered runs) and the headless scheduler
executor so both update host state identically.
"""
from __future__ import annotations

import re

_RECAP_RE = re.compile(
    r"^(?P<host>\S+)\s*:\s*ok=(?P<ok>\d+).*?changed=(?P<changed>\d+).*?failed=(?P<failed>\d+)"
)


def parse_recap(text: str) -> dict[str, dict]:
    """Return {host: {ok, changed, failed}} parsed from the PLAY RECAP block."""
    out: dict[str, dict] = {}
    in_recap = False
    for line in text.splitlines():
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
    for line in text.splitlines():
        if "REBOOT REQUIRED" in line:
            m = re.search(r'"msg":\s*"(\S+?):', line) or re.search(r"msg:\s*(\S+?):", line)
            if m:
                hosts.add(m.group(1))
    return hosts


def status_from_stats(stats: dict | None) -> str | None:
    if stats is None:
        return None
    if stats["failed"] > 0:
        return "failed"
    if stats["changed"] > 0:
        return "changed"
    return "ok"
