from __future__ import annotations

import io
import json
import csv
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from ops.cpu_recorder import (
    ContainerCpuSnapshot,
    ContainerCpuUsage,
    ContainerRef,
    CpuRecorder,
    CSV_FIELDS,
    DailyCsvStore,
    DockerStatsClient,
    HostCounters,
    HostCpuUsage,
    SampleBatch,
    calculate_container_usage,
    calculate_host_usage,
    parse_logical_cpu_count,
    parse_proc_stat,
    main,
)


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class _FakeUrlOpen:
    def __init__(self, responses):
        self._responses = iter(responses)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request.get_method(), request.full_url, timeout))
        return _Response(json.dumps(next(self._responses)).encode("utf-8"))


def test_parse_proc_stat_ignores_guest_fields():
    counters = parse_proc_stat("cpu 100 20 30 400 10 5 15 20 999 888\n")

    assert counters == HostCounters(
        total_ticks=600,
        idle_ticks=400,
        iowait_ticks=10,
    )


@pytest.mark.parametrize(
    "text",
    [
        "cpu0 1 2 3 4 5 6 7 8\n",
        "cpu 1 2 3\n",
        "cpu 1 2 3 -4 5 6 7 8\n",
        "cpu 1 2 nope 4 5 6 7 8\n",
    ],
)
def test_parse_proc_stat_rejects_invalid_aggregate_input(text):
    with pytest.raises(ValueError, match="invalid_proc_stat"):
        parse_proc_stat(text)


def test_parse_logical_cpu_count_uses_only_numbered_cpu_rows():
    text = (
        "cpu 10 0 0 90 0 0 0 0\n"
        "cpu0 5 0 0 45 0 0 0 0\n"
        "cpu1 5 0 0 45 0 0 0 0\n"
        "cpux 1 0 0 1 0 0 0 0\n"
        "intr 12\n"
    )

    assert parse_logical_cpu_count(text) == 2


def test_parse_logical_cpu_count_rejects_zero_cpu_rows():
    with pytest.raises(ValueError, match="invalid_logical_cpu_count"):
        parse_logical_cpu_count("cpu 10 0 0 90 0 0 0 0\n")


def test_host_usage_splits_busy_idle_and_iowait():
    usage = calculate_host_usage(
        HostCounters(1000, 600, 50),
        HostCounters(1200, 700, 70),
    )

    assert usage is not None
    assert usage.busy_pct == 40.0
    assert usage.idle_pct == 50.0
    assert usage.iowait_pct == 10.0
    assert usage.busy_pct + usage.idle_pct + usage.iowait_pct == 100.0


@pytest.mark.parametrize(
    ("previous", "current"),
    [
        (HostCounters(1000, 600, 50), HostCounters(900, 500, 40)),
        (HostCounters(1000, 600, 50), HostCounters(1000, 600, 50)),
        (HostCounters(1000, 600, 50), HostCounters(1100, 590, 60)),
        (HostCounters(1000, 600, 50), HostCounters(1100, 650, 40)),
    ],
)
def test_host_usage_skips_counter_resets_and_invalid_deltas(previous, current):
    assert calculate_host_usage(previous, current) is None


def test_container_cpu_usage_reports_cores_and_total_capacity():
    ref = ContainerRef("a" * 64, "feedling-enclave-backend-1")

    usage = calculate_container_usage(
        ref,
        ContainerCpuSnapshot(total_ns=1_000, system_ns=10_000, online_cpus=8),
        ContainerCpuSnapshot(total_ns=1_400, system_ns=10_800, online_cpus=8),
    )

    assert usage is not None
    assert usage.container_id == "a" * 64
    assert usage.name == "feedling-enclave-backend-1"
    assert usage.cores == 4.0
    assert usage.capacity_pct == 50.0


@pytest.mark.parametrize(
    ("previous", "current"),
    [
        (ContainerCpuSnapshot(500, 1000, 8), ContainerCpuSnapshot(400, 1200, 8)),
        (ContainerCpuSnapshot(500, 1000, 8), ContainerCpuSnapshot(600, 1000, 8)),
        (ContainerCpuSnapshot(500, 1000, 8), ContainerCpuSnapshot(600, 1200, 4)),
        (ContainerCpuSnapshot(500, 1000, 8), ContainerCpuSnapshot(800, 1200, 8)),
    ],
)
def test_container_cpu_usage_skips_invalid_or_reset_counters(previous, current):
    ref = ContainerRef("b" * 64, "serve-worker")

    assert calculate_container_usage(ref, previous, current) is None


def test_docker_client_uses_only_allowlisted_unversioned_gets():
    container_id = "a" * 64
    fake_urlopen = _FakeUrlOpen(
        [
            [{"Id": container_id, "Names": ["/backend"]}],
            {
                "cpu_stats": {
                    "cpu_usage": {
                        "total_usage": 1200,
                        "percpu_usage": [100] * 8,
                    },
                    "system_cpu_usage": 9600,
                    "online_cpus": 8,
                }
            },
        ]
    )
    client = DockerStatsClient(
        "http://cpu-socket-proxy:2375",
        timeout_sec=10,
        urlopen_fn=fake_urlopen,
    )

    refs = client.list_running_containers()
    snapshot = client.read_cpu_snapshot(container_id)

    assert refs == [ContainerRef(container_id, "backend")]
    assert snapshot == ContainerCpuSnapshot(1200, 9600, 8)
    assert fake_urlopen.requests == [
        ("GET", "http://cpu-socket-proxy:2375/containers/json?all=0", 10),
        (
            "GET",
            f"http://cpu-socket-proxy:2375/containers/{container_id}/stats?stream=false",
            10,
        ),
    ]


@pytest.mark.parametrize("online_cpus", [None, 0])
def test_docker_client_falls_back_to_positive_percpu_count(online_cpus):
    cpu_stats = {
        "cpu_usage": {"total_usage": 40, "percpu_usage": [1, 2, 3, 4]},
        "system_cpu_usage": 200,
    }
    if online_cpus is not None:
        cpu_stats["online_cpus"] = online_cpus
    fake_urlopen = _FakeUrlOpen(
        [
            {
                "cpu_stats": cpu_stats
            }
        ]
    )
    client = DockerStatsClient("http://proxy", urlopen_fn=fake_urlopen)

    assert client.read_cpu_snapshot("c" * 64).online_cpus == 4


def test_docker_client_rejects_invalid_id_before_request():
    fake_urlopen = _FakeUrlOpen([])
    client = DockerStatsClient("http://proxy", urlopen_fn=fake_urlopen)

    with pytest.raises(ValueError, match="invalid_container_id"):
        client.read_cpu_snapshot("../secret")

    assert fake_urlopen.requests == []


@pytest.mark.parametrize(
    "payload",
    [
        {"not": "a list"},
        [{"Id": "short", "Names": ["/backend"]}],
        [{"Id": "d" * 64, "Names": []}],
    ],
)
def test_docker_client_skips_malformed_list_entries(payload):
    client = DockerStatsClient(
        "http://proxy",
        urlopen_fn=_FakeUrlOpen([payload]),
    )

    if isinstance(payload, list):
        assert client.list_running_containers() == []
    else:
        with pytest.raises(ValueError, match="invalid_docker_response"):
            client.list_running_containers()


def test_docker_client_rejects_malformed_stats_without_leaking_payload():
    secret_marker = "must-not-leak"
    client = DockerStatsClient(
        "http://proxy",
        urlopen_fn=_FakeUrlOpen([{"secret": secret_marker}]),
    )

    with pytest.raises(ValueError) as caught:
        client.read_cpu_snapshot("e" * 64)

    assert str(caught.value) == "invalid_docker_response"
    assert secret_marker not in str(caught.value)


def _sample_batch(*containers):
    return SampleBatch(
        timestamp_utc=datetime(2026, 8, 13, 6, 7, 8, tzinfo=timezone.utc),
        host=HostCpuUsage(busy_pct=40.0, idle_pct=55.0, iowait_pct=5.0),
        host_logical_cpus=8,
        loads=(4.1, 3.9, 3.5),
        containers=tuple(containers),
    )


def test_daily_csv_store_writes_stable_schema_and_one_row_per_container(tmp_path):
    store = DailyCsvStore(tmp_path, "feedling-io-test")
    batch = _sample_batch(
        ContainerCpuUsage("a" * 64, "backend", 2.5, 31.25),
        ContainerCpuUsage("b" * 64, "serve-worker", 0.25, 3.125),
    )

    path = store.append(batch)
    store.append(batch)

    assert path == tmp_path / "cpu-2026-08-13.csv"
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
        assert tuple(handle.seek(0) or next(csv.reader(handle))) == CSV_FIELDS
    assert len(rows) == 4
    assert rows[0] == {
        "timestamp_utc": "2026-08-13T06:07:08Z",
        "cvm_name": "feedling-io-test",
        "host_logical_cpus": "8",
        "host_cpu_busy_pct": "40.000000",
        "host_cpu_idle_pct": "55.000000",
        "host_cpu_iowait_pct": "5.000000",
        "load1": "4.100000",
        "load5": "3.900000",
        "load15": "3.500000",
        "container_id": "a" * 64,
        "container_name": "backend",
        "container_cpu_cores": "2.500000",
        "container_cpu_capacity_pct": "31.250000",
    }


def test_daily_csv_store_writes_host_only_row_when_no_container_delta(tmp_path):
    path = DailyCsvStore(tmp_path, "feedling-enclave-v2").append(_sample_batch())

    with path.open(newline="", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))

    assert row["cvm_name"] == "feedling-enclave-v2"
    assert row["container_id"] == ""
    assert row["container_name"] == ""
    assert row["container_cpu_cores"] == ""
    assert row["container_cpu_capacity_pct"] == ""


def test_daily_csv_store_escapes_container_names(tmp_path):
    path = DailyCsvStore(tmp_path, "feedling-io-test").append(
        _sample_batch(ContainerCpuUsage("c" * 64, 'worker,"quoted"', 1.0, 12.5))
    )

    with path.open(newline="", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))

    assert row["container_name"] == 'worker,"quoted"'


def test_daily_csv_store_prunes_only_exact_files_before_oldest_kept_day(tmp_path):
    old = tmp_path / "cpu-2026-07-14.csv"
    oldest_kept = tmp_path / "cpu-2026-07-15.csv"
    today = tmp_path / "cpu-2026-08-13.csv"
    ignored = [
        tmp_path / "notes.txt",
        tmp_path / "cpu-bad.csv",
        tmp_path / "cpu-2026-07-13.csv.gz",
    ]
    for path in [old, oldest_kept, today, *ignored]:
        path.write_text("sample\n")
    directory = tmp_path / "cpu-2026-07-01.csv"
    directory.mkdir()
    symlink = tmp_path / "cpu-2026-07-02.csv"
    symlink.symlink_to(oldest_kept)
    store = DailyCsvStore(tmp_path, "feedling-io-test")

    deleted = store.prune(date(2026, 8, 13))

    assert deleted == [old]
    assert not old.exists()
    assert oldest_kept.exists()
    assert today.exists()
    assert all(path.exists() for path in ignored)
    assert directory.is_dir()
    assert symlink.is_symlink()


@pytest.mark.parametrize(
    ("path_factory", "cvm_name"),
    [
        (lambda tmp: Path("relative"), "feedling-io-test"),
        (lambda tmp: Path("/"), "feedling-io-test"),
        (lambda tmp: tmp / "missing", "feedling-io-test"),
        (lambda tmp: tmp, "bad/name"),
        (lambda tmp: tmp, ""),
    ],
)
def test_daily_csv_store_rejects_unsafe_paths_and_names(tmp_path, path_factory, cvm_name):
    with pytest.raises(ValueError, match="invalid_cpu_history_config"):
        DailyCsvStore(path_factory(tmp_path), cvm_name)


class _SequenceDockerClient:
    def __init__(self, refs, snapshots, list_results=None):
        self.refs = refs
        self.snapshots = {
            container_id: iter(values) for container_id, values in snapshots.items()
        }
        self.list_results = iter(list_results) if list_results is not None else None

    def list_running_containers(self, timeout_sec=None):
        if self.list_results is None:
            return list(self.refs)
        result = next(self.list_results)
        if isinstance(result, BaseException):
            raise result
        return list(result)

    def read_cpu_snapshot(self, container_id, timeout_sec=None):
        result = next(self.snapshots[container_id])
        if isinstance(result, BaseException):
            raise result
        return result


def _write_proc(proc_root, total_user, idle=600, iowait=50):
    proc_root.mkdir(exist_ok=True)
    (proc_root / "stat").write_text(
        f"cpu {total_user} 0 0 {idle} {iowait} 0 0 0\n"
        "cpu0 1 0 0 1 0 0 0 0\n"
        "cpu1 1 0 0 1 0 0 0 0\n"
    )
    (proc_root / "loadavg").write_text("1.25 2.50 3.75 1/100 42\n")


def _read_csv_rows(data_dir):
    paths = sorted(data_dir.glob("cpu-*.csv"))
    if not paths:
        return []
    with paths[-1].open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_recorder_first_sample_baselines_and_second_writes_stable_containers(tmp_path):
    proc_root = tmp_path / "proc"
    data_dir = tmp_path / "history"
    data_dir.mkdir()
    _write_proc(proc_root, 350)
    ref = ContainerRef("a" * 64, "backend")
    client = _SequenceDockerClient(
        [ref],
        {
            ref.container_id: [
                ContainerCpuSnapshot(1000, 10_000, 2),
                ContainerCpuSnapshot(1100, 10_400, 2),
            ]
        },
    )
    recorder = CpuRecorder(
        client,
        proc_root,
        DailyCsvStore(data_dir, "feedling-io-test"),
    )

    assert recorder.sample_once(datetime(2026, 8, 13, tzinfo=timezone.utc)) is False
    assert _read_csv_rows(data_dir) == []
    _write_proc(proc_root, 430, idle=700, iowait=70)
    assert recorder.sample_once(
        datetime(2026, 8, 13, 0, 1, tzinfo=timezone.utc)
    ) is True

    rows = _read_csv_rows(data_dir)
    assert len(rows) == 1
    assert rows[0]["host_logical_cpus"] == "2"
    assert rows[0]["container_name"] == "backend"
    assert rows[0]["container_cpu_cores"] == "0.500000"
    assert rows[0]["container_cpu_capacity_pct"] == "25.000000"


def test_recorder_new_container_waits_for_its_second_snapshot(tmp_path):
    proc_root = tmp_path / "proc"
    data_dir = tmp_path / "history"
    data_dir.mkdir()
    first_ref = ContainerRef("a" * 64, "backend")
    new_ref = ContainerRef("b" * 64, "serve-worker")
    client = _SequenceDockerClient(
        [],
        {
            first_ref.container_id: [
                ContainerCpuSnapshot(100, 1000, 2),
                ContainerCpuSnapshot(200, 1400, 2),
                ContainerCpuSnapshot(300, 1800, 2),
            ],
            new_ref.container_id: [
                ContainerCpuSnapshot(50, 1400, 2),
                ContainerCpuSnapshot(100, 1800, 2),
            ],
        },
        list_results=[[first_ref], [first_ref, new_ref], [first_ref, new_ref]],
    )
    recorder = CpuRecorder(client, proc_root, DailyCsvStore(data_dir, "test"))

    _write_proc(proc_root, 350)
    recorder.sample_once(datetime(2026, 8, 13, tzinfo=timezone.utc))
    _write_proc(proc_root, 430, idle=700, iowait=70)
    recorder.sample_once(datetime(2026, 8, 13, 0, 1, tzinfo=timezone.utc))
    assert [row["container_name"] for row in _read_csv_rows(data_dir)] == ["backend"]
    _write_proc(proc_root, 510, idle=800, iowait=90)
    recorder.sample_once(datetime(2026, 8, 13, 0, 2, tzinfo=timezone.utc))

    assert [row["container_name"] for row in _read_csv_rows(data_dir)] == [
        "backend",
        "backend",
        "serve-worker",
    ]


def test_recorder_container_failure_keeps_valid_peers_and_hides_error_detail(
    tmp_path, capsys
):
    proc_root = tmp_path / "proc"
    data_dir = tmp_path / "history"
    data_dir.mkdir()
    good = ContainerRef("a" * 64, "backend")
    bad = ContainerRef("b" * 64, "secret-container")
    client = _SequenceDockerClient(
        [good, bad],
        {
            good.container_id: [
                ContainerCpuSnapshot(100, 1000, 2),
                ContainerCpuSnapshot(200, 1400, 2),
            ],
            bad.container_id: [
                ContainerCpuSnapshot(50, 1000, 2),
                RuntimeError("payload-must-not-leak"),
            ],
        },
    )
    recorder = CpuRecorder(client, proc_root, DailyCsvStore(data_dir, "test"))

    _write_proc(proc_root, 350)
    recorder.sample_once(datetime(2026, 8, 13, tzinfo=timezone.utc))
    _write_proc(proc_root, 430, idle=700, iowait=70)
    assert recorder.sample_once(
        datetime(2026, 8, 13, 0, 1, tzinfo=timezone.utc)
    ) is True

    assert [row["container_name"] for row in _read_csv_rows(data_dir)] == ["backend"]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "cpu_recorder_container_sample_failed error=RuntimeError\n"
    assert "payload-must-not-leak" not in captured.err
    assert "secret-container" not in captured.err


def test_recorder_timeout_does_not_replace_baseline_and_next_samples_recover(
    tmp_path, capsys
):
    proc_root = tmp_path / "proc"
    data_dir = tmp_path / "history"
    data_dir.mkdir()
    ref = ContainerRef("a" * 64, "backend")
    client = _SequenceDockerClient(
        [],
        {
            ref.container_id: [
                ContainerCpuSnapshot(100, 1000, 2),
                ContainerCpuSnapshot(200, 1400, 2),
            ]
        },
        list_results=[TimeoutError("url-secret"), [ref], [ref]],
    )
    recorder = CpuRecorder(client, proc_root, DailyCsvStore(data_dir, "test"))

    _write_proc(proc_root, 350)
    assert recorder.sample_once(datetime(2026, 8, 13, tzinfo=timezone.utc)) is False
    _write_proc(proc_root, 430, idle=700, iowait=70)
    assert recorder.sample_once(
        datetime(2026, 8, 13, 0, 1, tzinfo=timezone.utc)
    ) is False
    _write_proc(proc_root, 510, idle=800, iowait=90)
    assert recorder.sample_once(
        datetime(2026, 8, 13, 0, 2, tzinfo=timezone.utc)
    ) is True

    assert len(_read_csv_rows(data_dir)) == 1
    captured = capsys.readouterr()
    assert captured.err == "cpu_recorder_sample_failed error=TimeoutError\n"
    assert "url-secret" not in captured.err


def test_recorder_bounds_all_docker_reads_to_one_cycle_timeout(tmp_path, capsys):
    class _Clock:
        now = 0.0

        def monotonic(self):
            return self.now

    class _BudgetClient:
        def __init__(self, clock):
            self.clock = clock
            self.timeouts = []
            self.refs = [
                ContainerRef("a" * 64, "first"),
                ContainerRef("b" * 64, "second"),
            ]

        def list_running_containers(self, timeout_sec=None):
            self.timeouts.append(timeout_sec)
            self.clock.now += 3.0
            return self.refs

        def read_cpu_snapshot(self, container_id, timeout_sec=None):
            self.timeouts.append(timeout_sec)
            self.clock.now += timeout_sec
            raise TimeoutError("bounded")

    proc_root = tmp_path / "proc"
    data_dir = tmp_path / "history"
    data_dir.mkdir()
    _write_proc(proc_root, 350)
    clock = _Clock()
    client = _BudgetClient(clock)
    recorder = CpuRecorder(
        client,
        proc_root,
        DailyCsvStore(data_dir, "test"),
        docker_cycle_timeout_sec=10.0,
        docker_monotonic_fn=clock.monotonic,
    )

    assert recorder.sample_once(datetime(2026, 8, 13, tzinfo=timezone.utc)) is False
    assert client.timeouts == [10.0, 7.0]
    assert clock.now == 10.0
    assert capsys.readouterr().err == (
        "cpu_recorder_sample_failed error=TimeoutError\n"
    )


def test_recorder_default_cycle_budget_handles_seven_delayed_containers(tmp_path):
    class _Clock:
        now = 0.0

        def monotonic(self):
            return self.now

    class _DelayedClient:
        timeout_sec = 10.0

        def __init__(self, clock):
            self.clock = clock
            self.refs = [
                ContainerRef(f"{index:064x}", f"container-{index}")
                for index in range(1, 8)
            ]
            self.samples = 0

        def list_running_containers(self, timeout_sec=None):
            self.clock.now += 0.5
            return self.refs

        def read_cpu_snapshot(self, container_id, timeout_sec=None):
            self.clock.now += 2.0
            self.samples += 1
            round_number = (self.samples - 1) // len(self.refs) + 1
            total = round_number * 100
            return ContainerCpuSnapshot(total, round_number * 400, 2)

    proc_root = tmp_path / "proc"
    data_dir = tmp_path / "history"
    data_dir.mkdir()
    _write_proc(proc_root, 350)
    clock = _Clock()
    recorder = CpuRecorder(
        _DelayedClient(clock),
        proc_root,
        DailyCsvStore(data_dir, "test"),
        docker_monotonic_fn=clock.monotonic,
    )

    assert recorder.sample_once(datetime(2026, 8, 13, tzinfo=timezone.utc)) is False
    _write_proc(proc_root, 430, idle=700, iowait=70)
    assert recorder.sample_once(
        datetime(2026, 8, 13, 0, 1, tzinfo=timezone.utc)
    ) is True

    assert [row["container_name"] for row in _read_csv_rows(data_dir)] == [
        f"container-{index}" for index in range(1, 8)
    ]


def test_recorder_deadline_failure_preserves_all_container_baselines(tmp_path):
    class _Clock:
        now = 0.0

        def monotonic(self):
            return self.now

    class _DeadlineClient:
        timeout_sec = 10.0

        def __init__(self, clock):
            self.clock = clock
            self.round = 0
            self.refs = [
                ContainerRef("a" * 64, "first"),
                ContainerRef("b" * 64, "second"),
            ]
            self.totals = {
                self.refs[0].container_id: [100, 200, 300],
                self.refs[1].container_id: [100, 300],
            }

        def list_running_containers(self, timeout_sec=None):
            self.round += 1
            return self.refs

        def read_cpu_snapshot(self, container_id, timeout_sec=None):
            if self.round == 2 and container_id == self.refs[1].container_id:
                self.clock.now += timeout_sec
                raise TimeoutError("cycle exhausted")
            total = self.totals[container_id].pop(0)
            return ContainerCpuSnapshot(total, total * 4, 2)

    proc_root = tmp_path / "proc"
    data_dir = tmp_path / "history"
    data_dir.mkdir()
    _write_proc(proc_root, 350)
    clock = _Clock()
    client = _DeadlineClient(clock)
    recorder = CpuRecorder(
        client,
        proc_root,
        DailyCsvStore(data_dir, "test"),
        docker_monotonic_fn=clock.monotonic,
    )

    assert recorder.sample_once(datetime(2026, 8, 13, tzinfo=timezone.utc)) is False
    _write_proc(proc_root, 430, idle=700, iowait=70)
    assert recorder.sample_once(
        datetime(2026, 8, 13, 0, 1, tzinfo=timezone.utc)
    ) is False
    _write_proc(proc_root, 510, idle=800, iowait=90)
    assert recorder.sample_once(
        datetime(2026, 8, 13, 0, 2, tzinfo=timezone.utc)
    ) is True

    assert [row["container_name"] for row in _read_csv_rows(data_dir)] == [
        "first",
        "second",
    ]


def test_recorder_keeps_request_timeout_separate_from_cycle_budget(tmp_path):
    proc_root = tmp_path / "proc"
    data_dir = tmp_path / "history"
    data_dir.mkdir()
    _write_proc(proc_root, 350)
    client = DockerStatsClient("http://proxy", timeout_sec=5)

    recorder = CpuRecorder(client, proc_root, DailyCsvStore(data_dir, "test"))

    assert client.timeout_sec == 5.0
    assert recorder.docker_cycle_timeout_sec == 30.0


def test_invalid_proc_sample_keeps_last_good_host_baseline(tmp_path, capsys):
    proc_root = tmp_path / "proc"
    data_dir = tmp_path / "history"
    data_dir.mkdir()
    client = _SequenceDockerClient([], {})
    recorder = CpuRecorder(client, proc_root, DailyCsvStore(data_dir, "test"))

    _write_proc(proc_root, 350)
    recorder.sample_once(datetime(2026, 8, 13, tzinfo=timezone.utc))
    (proc_root / "stat").write_text("broken\n")
    assert recorder.sample_once(
        datetime(2026, 8, 13, 0, 1, tzinfo=timezone.utc)
    ) is False
    _write_proc(proc_root, 510, idle=800, iowait=90)
    assert recorder.sample_once(
        datetime(2026, 8, 13, 0, 2, tzinfo=timezone.utc)
    ) is True

    assert len(_read_csv_rows(data_dir)) == 1
    assert capsys.readouterr().err == "cpu_recorder_sample_failed error=ValueError\n"


def test_recorder_prunes_once_on_startup_and_each_utc_day_change(tmp_path):
    class _SpyStore(DailyCsvStore):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.prune_calls = []

        def prune(self, today_utc):
            self.prune_calls.append(today_utc)
            return super().prune(today_utc)

    proc_root = tmp_path / "proc"
    data_dir = tmp_path / "history"
    data_dir.mkdir()
    _write_proc(proc_root, 350)
    store = _SpyStore(data_dir, "test")
    recorder = CpuRecorder(_SequenceDockerClient([], {}), proc_root, store)

    recorder.sample_once(datetime(2026, 8, 13, tzinfo=timezone.utc))
    _write_proc(proc_root, 430, idle=700, iowait=70)
    recorder.sample_once(datetime(2026, 8, 13, 23, 59, tzinfo=timezone.utc))
    _write_proc(proc_root, 510, idle=800, iowait=90)
    recorder.sample_once(datetime(2026, 8, 14, tzinfo=timezone.utc))

    assert store.prune_calls == [date(2026, 8, 13), date(2026, 8, 14)]


def test_run_forever_uses_monotonic_deadlines_and_skips_missed_intervals(tmp_path):
    proc_root = tmp_path / "proc"
    data_dir = tmp_path / "history"
    data_dir.mkdir()
    _write_proc(proc_root, 350)
    monotonic_values = iter([0.0, 5.0, 130.0])
    sleep_calls = []

    def fake_sleep(seconds):
        sleep_calls.append(seconds)
        if len(sleep_calls) == 2:
            raise StopIteration

    recorder = CpuRecorder(
        _SequenceDockerClient([], {}),
        proc_root,
        DailyCsvStore(data_dir, "test"),
        monotonic_fn=lambda: next(monotonic_values),
        sleep_fn=fake_sleep,
        now_fn=lambda: datetime(2026, 8, 13, tzinfo=timezone.utc),
    )

    with pytest.raises(StopIteration):
        recorder.run_forever()

    assert sleep_calls == [55.0, 50.0]


def test_main_rejects_missing_cvm_name(monkeypatch, capsys):
    monkeypatch.delenv("CPU_RECORDER_CVM_NAME", raising=False)

    assert main() == 2
    assert capsys.readouterr().err == "cpu_recorder_startup_failed error=KeyError\n"


def test_main_rejects_unmeasured_numeric_override(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("CPU_RECORDER_CVM_NAME", "feedling-io-test")
    monkeypatch.setenv("CPU_RECORDER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CPU_RECORDER_INTERVAL_SEC", "61")

    assert main() == 2
    assert capsys.readouterr().err == "cpu_recorder_startup_failed error=ValueError\n"


def test_main_rejects_missing_host_proc_files(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("CPU_RECORDER_CVM_NAME", "feedling-io-test")
    monkeypatch.setenv("CPU_RECORDER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CPU_RECORDER_PROC_ROOT", str(tmp_path / "missing-proc"))

    assert main() == 2
    assert capsys.readouterr().err == "cpu_recorder_startup_failed error=ValueError\n"


def test_main_accepts_measured_defaults_and_runs_recorder(monkeypatch, tmp_path):
    calls = []
    proc_root = tmp_path / "proc"
    _write_proc(proc_root, 350)
    monkeypatch.setenv("CPU_RECORDER_CVM_NAME", "feedling-io-test")
    monkeypatch.setenv("CPU_RECORDER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CPU_RECORDER_PROC_ROOT", str(proc_root))
    monkeypatch.setattr(CpuRecorder, "run_forever", lambda self: calls.append(self))

    assert main() == 0
    assert len(calls) == 1
    assert calls[0].interval_sec == 60.0
    assert calls[0].store.retention_days == 30
    assert calls[0].client._timeout_sec == 10.0
    assert calls[0].docker_cycle_timeout_sec == 30.0
