from __future__ import annotations

import io
import json

import pytest

from ops.cpu_recorder import (
    ContainerCpuSnapshot,
    ContainerRef,
    DockerStatsClient,
    HostCounters,
    calculate_container_usage,
    calculate_host_usage,
    parse_logical_cpu_count,
    parse_proc_stat,
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


def test_docker_client_falls_back_to_percpu_count():
    fake_urlopen = _FakeUrlOpen(
        [
            {
                "cpu_stats": {
                    "cpu_usage": {"total_usage": 40, "percpu_usage": [1, 2, 3, 4]},
                    "system_cpu_usage": 200,
                }
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
