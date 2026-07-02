from __future__ import annotations
import logging

log = logging.getLogger("feedling.observability")


def read_host_load(path: str = "/proc/loadavg") -> tuple[float, float, float]:
    with open(path) as f:
        parts = f.read().split()
    return (float(parts[0]), float(parts[1]), float(parts[2]))


def read_host_mem(path: str = "/proc/meminfo") -> dict:
    vals: dict[str, int] = {}
    with open(path) as f:
        for ln in f:
            k, _, rest = ln.partition(":")
            n = rest.strip().split()
            if n and n[0].isdigit():
                vals[k] = int(n[0]) * 1024  # kB → bytes
    return {
        "total_bytes": vals.get("MemTotal"),
        "avail_bytes": vals.get("MemAvailable"),
        "free_bytes": vals.get("MemFree"),
        "swap_total_bytes": vals.get("SwapTotal"),
        "swap_free_bytes": vals.get("SwapFree"),
    }


def _read_int(path: str) -> int | None:
    try:
        with open(path) as f:
            return int(f.read().split()[0])
    except (OSError, ValueError):
        return None


def read_cgroup(root: str = "/sys/fs/cgroup") -> dict:
    mem = _read_int(f"{root}/memory.current")
    usage = None
    try:
        with open(f"{root}/cpu.stat") as f:
            for ln in f:
                if ln.startswith("usage_usec"):
                    usage = int(ln.split()[1])
                    break
    except OSError:
        pass
    return {"mem_bytes": mem, "cpu_usage_usec": usage}
