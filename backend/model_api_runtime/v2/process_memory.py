"""Content-free memory readings for Runtime V2 processes (T779 step 2c).

The worker heartbeat carries these so memory can be measured from the database
without shell access to the machine. Every value is kilobytes or ``None`` when
the source cannot be read on this host (a missing file is never reported as 0).

- ``rss_kb``: ``VmRSS`` from ``/proc/<pid>/status``.
- ``pss_kb``: ``Pss`` from ``/proc/<pid>/smaps_rollup`` (shared pages divided
  among the processes that map them, so parent and slots can be summed).
- ``cgroup_kb``: the container's current usage, cgroup v2 ``memory.current``
  or v1 ``memory.usage_in_bytes``.
"""
from __future__ import annotations

_CGROUP_FILES = ("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory/memory.usage_in_bytes")


def _field_kb(path: str, name: str) -> int | None:
    try:
        with open(path, encoding="ascii", errors="replace") as handle:
            for line in handle:
                if line.startswith(name + ":"):
                    parts = line.split()
                    return int(parts[1]) if len(parts) >= 2 else None
    except (OSError, ValueError):
        return None
    return None


def process(pid: int | None) -> dict:
    if not pid:
        return {"rss_kb": None, "pss_kb": None}
    return {"rss_kb": _field_kb(f"/proc/{int(pid)}/status", "VmRSS"),
            "pss_kb": _field_kb(f"/proc/{int(pid)}/smaps_rollup", "Pss")}


def cgroup_kb() -> int | None:
    for path in _CGROUP_FILES:
        try:
            with open(path, encoding="ascii") as handle:
                return int(handle.read().strip()) // 1024
        except (OSError, ValueError):
            continue
    return None
