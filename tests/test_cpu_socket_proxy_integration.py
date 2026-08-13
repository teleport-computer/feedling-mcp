from __future__ import annotations

import os
from pathlib import Path
import subprocess
import time
import re
import uuid

import pytest


PROXY_IMAGE = (
    "ghcr.io/wollomatic/socket-proxy:1.13.0@"
    "sha256:be7a61fc50baf0add95d94442c3d40cddc4594925a564f22ba870eb017ceae9f"
)


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], check=check, text=True, capture_output=True
    )


def _container_status(container: str, method: str, url: str) -> int | None:
    args = ["exec", container, "wget", "-S", "-O", "/dev/null"]
    if method == "POST":
        args.append("--post-data=")
    result = _docker(*args, url, check=False)
    statuses = re.findall(r"HTTP/\S+\s+(\d{3})", result.stderr)
    return int(statuses[-1]) if statuses else None


def _docker_socket_path() -> Path | None:
    configured = os.environ.get("DOCKER_HOST", "")
    if not configured:
        result = _docker(
            "context", "inspect", "--format", "{{.Endpoints.docker.Host}}", check=False
        )
        configured = result.stdout.strip() if result.returncode == 0 else ""
    if configured.startswith("unix://"):
        path = Path(configured.removeprefix("unix://"))
    else:
        path = Path("/var/run/docker.sock")
    return path if path.exists() else None


@pytest.mark.skipif(
    os.environ.get("FEEDLING_RUN_DOCKER_SOCKET_TESTS") != "1",
    reason="set FEEDLING_RUN_DOCKER_SOCKET_TESTS=1 for the live Docker contract",
)
def test_socket_proxy_allows_only_cpu_recorder_reads():
    docker_socket = _docker_socket_path()
    if docker_socket is None:
        pytest.skip("Docker Unix socket is unavailable")
    if _docker("info", check=False).returncode != 0:
        pytest.skip("Docker daemon is unavailable")

    suffix = uuid.uuid4().hex[:12]
    network = f"cpu-recorder-test-{suffix}"
    target = f"cpu-recorder-target-{suffix}"
    client = f"cpu-recorder-client-{suffix}"
    proxy = f"cpu-recorder-proxy-{suffix}"
    created_containers: list[str] = []
    created_network = False
    try:
        _docker("network", "create", "--label", "feedling.cpu-recorder-test=1", network)
        created_network = True
        target_id = _docker(
            "run",
            "-d",
            "--name",
            target,
            "--network",
            network,
            "--label",
            "feedling.cpu-recorder-test=1",
            "alpine:3.21",
            "sleep",
            "300",
        ).stdout.strip()
        created_containers.append(target_id)
        client_id = _docker(
            "run",
            "-d",
            "--name",
            client,
            "--network",
            network,
            "--label",
            "feedling.cpu-recorder-test=1",
            "alpine:3.21",
            "sleep",
            "300",
        ).stdout.strip()
        created_containers.append(client_id)
        proxy_id = _docker(
            "run",
            "-d",
            "--name",
            proxy,
            "--network",
            network,
            "--label",
            "feedling.cpu-recorder-test=1",
            "-v",
            f"{docker_socket}:/var/run/docker.sock:ro",
            "--group-add",
            "0",
            PROXY_IMAGE,
            "-loglevel=INFO",
            "-listenip=0.0.0.0",
            f"-allowfrom={client}",
            "-allowGET=/containers/json",
            r"-allowGET=/containers/[0-9a-f]{64}/stats",
        ).stdout.strip()
        created_containers.append(proxy_id)

        base_url = f"http://{proxy}:2375"
        deadline = time.monotonic() + 20
        while True:
            if (
                _container_status(
                    client, "GET", f"{base_url}/containers/json?all=0"
                )
                == 200
            ):
                break
            if time.monotonic() >= deadline:
                pytest.fail("socket proxy did not become ready")
            time.sleep(0.2)

        assert (
            _container_status(client, "GET", f"{base_url}/containers/json?all=0")
            == 200
        )
        assert (
            _container_status(
                client,
                "GET",
                f"{base_url}/containers/{target_id}/stats?stream=false",
            )
            == 200
        )
        denied_reads = [
            ("GET", f"{base_url}/containers/{target_id}/json"),
            ("GET", f"{base_url}/containers/{target_id}/logs?stdout=1"),
            ("GET", f"{base_url}/images/json"),
            ("GET", f"{base_url}/info"),
            ("GET", f"{base_url}/version"),
        ]
        assert [
            _container_status(client, method, url) for method, url in denied_reads
        ] == [403] * len(denied_reads)
        assert (
            _container_status(
                client, "POST", f"{base_url}/containers/{target_id}/restart"
            )
            == 405
        )
        assert _docker("inspect", "-f", "{{.State.Running}}", target_id).stdout.strip() == "true"
    finally:
        for container_id in reversed(created_containers):
            _docker("rm", "-f", container_id, check=False)
        if created_network:
            _docker("network", "rm", network, check=False)
