"""Bounded, content-free CPU history recorder for Feedling CVMs."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Callable
from urllib.request import Request, urlopen


_NUMBERED_CPU_RE = re.compile(r"^cpu[0-9]+$")
_CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class HostCounters:
    total_ticks: int
    idle_ticks: int
    iowait_ticks: int


@dataclass(frozen=True)
class HostCpuUsage:
    busy_pct: float
    idle_pct: float
    iowait_pct: float


@dataclass(frozen=True)
class ContainerRef:
    container_id: str
    name: str


@dataclass(frozen=True)
class ContainerCpuSnapshot:
    total_ns: int
    system_ns: int
    online_cpus: int


@dataclass(frozen=True)
class ContainerCpuUsage:
    container_id: str
    name: str
    cores: float
    capacity_pct: float


def parse_proc_stat(text: str) -> HostCounters:
    """Parse the aggregate Linux CPU counters without double-counting guests."""

    for line in text.splitlines():
        fields = line.split()
        if not fields or fields[0] != "cpu":
            continue
        if len(fields) < 9:
            raise ValueError("invalid_proc_stat")
        try:
            values = [int(field) for field in fields[1:9]]
        except ValueError as exc:
            raise ValueError("invalid_proc_stat") from exc
        if any(value < 0 for value in values):
            raise ValueError("invalid_proc_stat")
        return HostCounters(
            total_ticks=sum(values),
            idle_ticks=values[3],
            iowait_ticks=values[4],
        )
    raise ValueError("invalid_proc_stat")


def parse_logical_cpu_count(text: str) -> int:
    """Count numbered host CPU rows in a mounted /proc/stat snapshot."""

    count = sum(
        1
        for line in text.splitlines()
        if line.split() and _NUMBERED_CPU_RE.fullmatch(line.split()[0])
    )
    if count <= 0:
        raise ValueError("invalid_logical_cpu_count")
    return count


def calculate_host_usage(
    previous: HostCounters,
    current: HostCounters,
) -> HostCpuUsage | None:
    """Return interval percentages, or None when cumulative counters reset."""

    delta_total = current.total_ticks - previous.total_ticks
    delta_idle = current.idle_ticks - previous.idle_ticks
    delta_iowait = current.iowait_ticks - previous.iowait_ticks
    if delta_total <= 0 or delta_idle < 0 or delta_iowait < 0:
        return None
    idle_pct = 100.0 * delta_idle / delta_total
    iowait_pct = 100.0 * delta_iowait / delta_total
    busy_pct = 100.0 - idle_pct - iowait_pct
    percentages = (busy_pct, idle_pct, iowait_pct)
    if any(value < -1e-9 or value > 100.0 + 1e-9 for value in percentages):
        return None
    return HostCpuUsage(
        busy_pct=min(100.0, max(0.0, busy_pct)),
        idle_pct=min(100.0, max(0.0, idle_pct)),
        iowait_pct=min(100.0, max(0.0, iowait_pct)),
    )


def calculate_container_usage(
    ref: ContainerRef,
    previous: ContainerCpuSnapshot,
    current: ContainerCpuSnapshot,
) -> ContainerCpuUsage | None:
    """Calculate one container's share of total host CPU over an interval."""

    cpu_delta = current.total_ns - previous.total_ns
    system_delta = current.system_ns - previous.system_ns
    if (
        cpu_delta < 0
        or system_delta <= 0
        or previous.online_cpus <= 0
        or current.online_cpus != previous.online_cpus
    ):
        return None
    capacity_pct = 100.0 * cpu_delta / system_delta
    if capacity_pct < -1e-9 or capacity_pct > 100.0 + 1e-9:
        return None
    capacity_pct = min(100.0, max(0.0, capacity_pct))
    return ContainerCpuUsage(
        container_id=ref.container_id,
        name=ref.name,
        cores=capacity_pct * current.online_cpus / 100.0,
        capacity_pct=capacity_pct,
    )


class DockerStatsClient:
    """Read the two Docker API surfaces admitted by the socket proxy."""

    def __init__(
        self,
        base_url: str,
        timeout_sec: float = 10.0,
        urlopen_fn: Callable[..., Any] = urlopen,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_sec = timeout_sec
        self._urlopen = urlopen_fn

    def _get_json(self, path: str) -> Any:
        request = Request(f"{self._base_url}{path}", method="GET")
        try:
            with self._urlopen(request, timeout=self._timeout_sec) as response:
                return json.load(response)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("invalid_docker_response") from exc

    def list_running_containers(self) -> list[ContainerRef]:
        payload = self._get_json("/containers/json?all=0")
        if not isinstance(payload, list):
            raise ValueError("invalid_docker_response")
        refs: list[ContainerRef] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            container_id = item.get("Id")
            names = item.get("Names")
            if (
                not isinstance(container_id, str)
                or not _CONTAINER_ID_RE.fullmatch(container_id)
                or not isinstance(names, list)
                or not names
                or not isinstance(names[0], str)
            ):
                continue
            name = names[0][1:] if names[0].startswith("/") else names[0]
            if not name:
                continue
            refs.append(ContainerRef(container_id=container_id, name=name))
        return refs

    def read_cpu_snapshot(self, container_id: str) -> ContainerCpuSnapshot:
        if not _CONTAINER_ID_RE.fullmatch(container_id):
            raise ValueError("invalid_container_id")
        payload = self._get_json(
            f"/containers/{container_id}/stats?stream=false"
        )
        try:
            cpu_stats = payload["cpu_stats"]
            cpu_usage = cpu_stats["cpu_usage"]
            total_ns = cpu_usage["total_usage"]
            system_ns = cpu_stats["system_cpu_usage"]
            online_cpus = cpu_stats.get("online_cpus")
            if online_cpus is None:
                percpu_usage = cpu_usage["percpu_usage"]
                online_cpus = len(percpu_usage)
        except (KeyError, TypeError) as exc:
            raise ValueError("invalid_docker_response") from exc
        values = (total_ns, system_ns, online_cpus)
        if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
            raise ValueError("invalid_docker_response")
        if total_ns < 0 or system_ns < 0 or online_cpus <= 0:
            raise ValueError("invalid_docker_response")
        return ContainerCpuSnapshot(
            total_ns=total_ns,
            system_ns=system_ns,
            online_cpus=online_cpus,
        )
