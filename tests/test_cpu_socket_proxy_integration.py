from __future__ import annotations

import os
from pathlib import Path
import socket
import subprocess
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
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


def _status(method: str, url: str) -> int:
    request = Request(url, data=b"" if method == "POST" else None, method=method)
    try:
        with urlopen(request, timeout=10) as response:
            response.read(1)
            return response.status
    except HTTPError as exc:
        exc.read()
        return exc.code


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


def _unused_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


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
    proxy = f"cpu-recorder-proxy-{suffix}"
    created_containers: list[str] = []
    created_network = False
    port = _unused_loopback_port()
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
            "-p",
            f"127.0.0.1:{port}:2375",
            PROXY_IMAGE,
            "-loglevel=INFO",
            "-listenip=0.0.0.0",
            "-allowfrom=0.0.0.0/0",
            "-allowGET=/containers/json",
            r"-allowGET=/containers/[0-9a-f]{64}/stats",
        ).stdout.strip()
        created_containers.append(proxy_id)

        base_url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 20
        while True:
            try:
                if _status("GET", f"{base_url}/containers/json?all=0") == 200:
                    break
            except OSError:
                pass
            if time.monotonic() >= deadline:
                pytest.fail("socket proxy did not become ready")
            time.sleep(0.2)

        assert _status("GET", f"{base_url}/containers/json?all=0") == 200
        assert (
            _status("GET", f"{base_url}/containers/{target_id}/stats?stream=false")
            == 200
        )
        denied_reads = [
            ("GET", f"{base_url}/containers/{target_id}/json"),
            ("GET", f"{base_url}/containers/{target_id}/logs?stdout=1"),
            ("GET", f"{base_url}/images/json"),
            ("GET", f"{base_url}/info"),
            ("GET", f"{base_url}/version"),
        ]
        assert [_status(method, url) for method, url in denied_reads] == [403] * len(
            denied_reads
        )
        assert (
            _status("POST", f"{base_url}/containers/{target_id}/restart") == 405
        )
        assert _docker("inspect", "-f", "{{.State.Running}}", target_id).stdout.strip() == "true"
    finally:
        for container_id in reversed(created_containers):
            _docker("rm", "-f", container_id, check=False)
        if created_network:
            _docker("network", "rm", network, check=False)
