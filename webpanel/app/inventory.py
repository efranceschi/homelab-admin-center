"""Per-host inventory: persist curated Ansible facts with last-change tracking.

The playbook's ``PANEL_FACTS`` probe (see ``playbooks/webpanel.yml``) emits a
curated, STABLE subset of facts per host on every run. ``store_facts`` upserts
them into ``host_inventory``, recording the previous value + a timestamp ONLY
when a value actually changes — so the host detail page can show the current
value and, when it changed, what it was and when. Only the last change is kept
(no full history). Shared by the async JobManager and the headless scheduler so
both run paths populate the inventory identically.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import HostInventory, utcnow

# The curated keys the panel persists. Must match the dict assembled by the
# "Assemble panel inventory facts" task in playbooks/webpanel.yml. STABLE only —
# anything that changes every run would churn changed_at and bury real changes.
INVENTORY_KEYS = (
    "hostname",
    "fqdn",
    "distribution",
    "distribution_version",
    "distribution_release",
    "kernel",
    "architecture",
    "cpu_model",
    "vcpus",
    "memtotal_mb",
    "disk_mounts",
    "default_ipv4",
    "macaddress",
    "all_ipv4",
    "virtualization_type",
    "virtualization_role",
    "python_version",
    "pkg_mgr",
    "timezone",
)


def _norm(v) -> str:
    """Normalize a fact value to a string for storage/comparison."""
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v)
    return str(v).strip()


def store_facts(db: Session, servers, facts_by_host: dict[str, dict]) -> int:
    """Upsert curated facts per host; stamp the last change only on real changes.

    ``facts_by_host`` maps inventory host name (== ``Server.name``) to a
    ``{key: value}`` dict, as returned by ``results.parse_facts``. Returns the
    number of values that changed across all hosts.
    """
    if not facts_by_host:
        return 0
    changed = 0
    now = utcnow()
    for srv in servers:
        facts = facts_by_host.get(srv.name)
        if not facts:
            continue
        existing = {
            r.key: r
            for r in db.scalars(
                select(HostInventory).where(HostInventory.server_id == srv.id)
            ).all()
        }
        for key in INVENTORY_KEYS:
            if key not in facts:
                continue  # fact not gathered this run — leave any stored value as-is
            new_val = _norm(facts.get(key))
            if not new_val:
                continue  # empty/unknown — never clobber a known-good value
            row = existing.get(key)
            if row is None:
                # First observation: seed it; this is an initial value, not a change.
                db.add(
                    HostInventory(
                        server_id=srv.id, key=key, value=new_val,
                        previous_value=None, changed_at=None,
                    )
                )
                continue
            if new_val != row.value:
                # Real change: shift current -> previous and stamp the moment.
                row.previous_value = row.value
                row.value = new_val
                row.changed_at = now
                changed += 1
            # else: unchanged — value/previous_value/changed_at untouched;
            #       updated_at auto-bumps via onupdate. No spurious change.
    return changed
