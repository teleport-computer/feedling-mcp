"""Bounded, content-free CPU history recorder for Feedling CVMs."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Callable
from urllib.request import Request, urlopen


_NUMBERED_CPU_RE = re.compile(r"^cpu[0-9]+$")
_CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_CVM_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_DAILY_FILE_RE = re.compile(r"^cpu-([0-9]{4}-[0-9]{2}-[0-9]{2})\.csv$")

CSV_FIELDS = (
    "timestamp_utc",
    "cvm_name",
    "host_logical_cpus",
    "host_cpu_busy_pct",
    "host_cpu_idle_pct",
    "host_cpu_iowait_pct",
    "load1",
    "load5",
    "load15",
    "container_id",
    "container_name",
    "container_cpu_cores",
    "container_cpu_capacity_pct",
)


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


@dataclass(frozen=True)
class SampleBatch:
    timestamp_utc: datetime
    host: HostCpuUsage
    host_logical_cpus: int
    loads: tuple[float, float, float]
    containers: tuple[ContainerCpuUsage, ...]


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


class DailyCsvStore:
    """Append daily CPU samples and prune only recorder-owned old files."""

    def __init__(
        self,
        data_dir: Path,
        cvm_name: str,
        retention_days: int = 30,
    ) -> None:
        data_dir = Path(data_dir)
        try:
            resolved = data_dir.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError("invalid_cpu_history_config") from exc
        if (
            not data_dir.is_absolute()
            or not resolved.is_dir()
            or resolved == Path("/")
            or resolved == Path.cwd().resolve()
            or not _CVM_NAME_RE.fullmatch(cvm_name)
            or isinstance(retention_days, bool)
            or not isinstance(retention_days, int)
            or retention_days <= 0
        ):
            raise ValueError("invalid_cpu_history_config")
        self.data_dir = resolved
        self.cvm_name = cvm_name
        self.retention_days = retention_days

    @staticmethod
    def _timestamp(timestamp: datetime) -> str:
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("invalid_sample_timestamp")
        return timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _number(value: float) -> str:
        return f"{value:.6f}"

    def append(self, batch: SampleBatch) -> Path:
        timestamp = batch.timestamp_utc.astimezone(timezone.utc)
        path = self.data_dir / f"cpu-{timestamp.date().isoformat()}.csv"
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError("invalid_cpu_history_file")
        write_header = not path.exists() or path.stat().st_size == 0
        common = {
            "timestamp_utc": self._timestamp(batch.timestamp_utc),
            "cvm_name": self.cvm_name,
            "host_logical_cpus": str(batch.host_logical_cpus),
            "host_cpu_busy_pct": self._number(batch.host.busy_pct),
            "host_cpu_idle_pct": self._number(batch.host.idle_pct),
            "host_cpu_iowait_pct": self._number(batch.host.iowait_pct),
            "load1": self._number(batch.loads[0]),
            "load5": self._number(batch.loads[1]),
            "load15": self._number(batch.loads[2]),
        }
        containers: tuple[ContainerCpuUsage | None, ...] = (
            tuple(batch.containers) if batch.containers else (None,)
        )
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
            if write_header:
                writer.writeheader()
            for container in containers:
                row = dict(common)
                row.update(
                    {
                        "container_id": container.container_id if container else "",
                        "container_name": container.name if container else "",
                        "container_cpu_cores": (
                            self._number(container.cores) if container else ""
                        ),
                        "container_cpu_capacity_pct": (
                            self._number(container.capacity_pct) if container else ""
                        ),
                    }
                )
                writer.writerow(row)
            handle.flush()
            os.fsync(handle.fileno())
        return path

    def prune(self, today_utc: date) -> list[Path]:
        oldest_kept = today_utc - timedelta(days=self.retention_days - 1)
        deleted: list[Path] = []
        for path in sorted(self.data_dir.iterdir()):
            match = _DAILY_FILE_RE.fullmatch(path.name)
            if match is None or path.is_symlink() or not path.is_file():
                continue
            try:
                file_date = date.fromisoformat(match.group(1))
            except ValueError:
                continue
            if file_date < oldest_kept:
                path.unlink()
                deleted.append(path)
        return deleted


class CpuRecorder:
    """Collect host and container counter deltas without affecting workloads."""

    def __init__(
        self,
        client: DockerStatsClient,
        proc_root: Path,
        store: DailyCsvStore,
        interval_sec: float = 60.0,
        *,
        monotonic_fn: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], None] = time.sleep,
        now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.client = client
        self.proc_root = Path(proc_root)
        self.store = store
        self.interval_sec = interval_sec
        self._monotonic = monotonic_fn
        self._sleep = sleep_fn
        self._now = now_fn
        self._previous_host: HostCounters | None = None
        self._previous_containers: dict[str, ContainerCpuSnapshot] = {}
        self._last_pruned_date: date | None = None

    @staticmethod
    def _error(slug: str, exc: Exception) -> None:
        print(f"{slug} error={type(exc).__name__}", file=sys.stderr, flush=True)

    def _read_host(self) -> tuple[HostCounters, int, tuple[float, float, float]]:
        stat_text = (self.proc_root / "stat").read_text(encoding="utf-8")
        load_fields = (self.proc_root / "loadavg").read_text(
            encoding="utf-8"
        ).split()
        if len(load_fields) < 3:
            raise ValueError("invalid_loadavg")
        try:
            loads = tuple(float(value) for value in load_fields[:3])
        except ValueError as exc:
            raise ValueError("invalid_loadavg") from exc
        if any(value < 0 for value in loads):
            raise ValueError("invalid_loadavg")
        return (
            parse_proc_stat(stat_text),
            parse_logical_cpu_count(stat_text),
            (loads[0], loads[1], loads[2]),
        )

    def sample_once(self, now_utc: datetime) -> bool:
        try:
            current_host, logical_cpus, loads = self._read_host()
            refs = self.client.list_running_containers()
            current_containers: dict[str, ContainerCpuSnapshot] = {}
            usages: list[ContainerCpuUsage] = []
            for ref in refs:
                try:
                    snapshot = self.client.read_cpu_snapshot(ref.container_id)
                except Exception as exc:
                    self._error("cpu_recorder_container_sample_failed", exc)
                    continue
                current_containers[ref.container_id] = snapshot
                previous = self._previous_containers.get(ref.container_id)
                if previous is None:
                    continue
                usage = calculate_container_usage(ref, previous, snapshot)
                if usage is not None:
                    usages.append(usage)

            host_usage = (
                calculate_host_usage(self._previous_host, current_host)
                if self._previous_host is not None
                else None
            )
            self._previous_host = current_host
            self._previous_containers = current_containers

            utc_date = now_utc.astimezone(timezone.utc).date()
            if self._last_pruned_date != utc_date:
                self.store.prune(utc_date)
                self._last_pruned_date = utc_date

            if host_usage is None:
                return False
            self.store.append(
                SampleBatch(
                    timestamp_utc=now_utc,
                    host=host_usage,
                    host_logical_cpus=logical_cpus,
                    loads=loads,
                    containers=tuple(usages),
                )
            )
            return True
        except Exception as exc:
            self._error("cpu_recorder_sample_failed", exc)
            return False

    def run_forever(self) -> None:
        deadline = self._monotonic()
        while True:
            self.sample_once(self._now())
            deadline += self.interval_sec
            current = self._monotonic()
            while deadline <= current:
                deadline += self.interval_sec
            self._sleep(deadline - current)


def _fixed_numeric_env(name: str, default: str) -> float:
    raw = os.environ.get(name, default)
    if raw != default:
        raise ValueError("invalid_cpu_recorder_config")
    return float(raw)


def main() -> int:
    """Validate measured configuration and run until the container stops."""

    try:
        cvm_name = os.environ["CPU_RECORDER_CVM_NAME"]
        docker_url = os.environ.get(
            "CPU_RECORDER_DOCKER_URL", "http://cpu-socket-proxy:2375"
        )
        proc_root = Path(os.environ.get("CPU_RECORDER_PROC_ROOT", "/host/proc"))
        data_dir = Path(
            os.environ.get("CPU_RECORDER_DATA_DIR", "/var/lib/feedling-cpu")
        )
        interval = _fixed_numeric_env("CPU_RECORDER_INTERVAL_SEC", "60")
        retention = int(_fixed_numeric_env("CPU_RECORDER_RETENTION_DAYS", "30"))
        timeout = _fixed_numeric_env("CPU_RECORDER_DOCKER_TIMEOUT_SEC", "10")
        store = DailyCsvStore(data_dir, cvm_name, retention_days=retention)
        recorder = CpuRecorder(
            DockerStatsClient(docker_url, timeout_sec=timeout),
            proc_root,
            store,
            interval_sec=interval,
        )
    except (KeyError, TypeError, ValueError, OSError) as exc:
        CpuRecorder._error("cpu_recorder_startup_failed", exc)
        return 2
    recorder.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
