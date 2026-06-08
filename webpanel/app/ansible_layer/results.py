"""Parse ansible-playbook stdout for the PLAY RECAP and reboot-required signal.

Shared by the async JobManager (panel-triggered runs) and the headless scheduler
executor so both update host state identically.
"""
from __future__ import annotations

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
