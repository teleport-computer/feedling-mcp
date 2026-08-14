from __future__ import annotations

from pathlib import Path
import re

import pytest

from tools.strict_yaml import load_yaml_strict


ROOT = Path(__file__).resolve().parents[1]
PROXY_IMAGE = (
    "ghcr.io/wollomatic/socket-proxy:1.13.0@"
    "sha256:be7a61fc50baf0add95d94442c3d40cddc4594925a564f22ba870eb017ceae9f"
)
PROXY_COMMAND = [
    "-loglevel=INFO",
    "-listenip=0.0.0.0",
    "-allowfrom=cpu-recorder",
    "-allowGET=/containers/json",
    r"-allowGET=/containers/[0-9a-f]{64}/stats",
    "-allowhealthcheck",
    "-watchdoginterval=60",
    "-stoponwatchdog",
]
ENVIRONMENT_KEYS = {
    "CPU_RECORDER_CVM_NAME",
    "CPU_RECORDER_DOCKER_URL",
    "CPU_RECORDER_PROC_ROOT",
    "CPU_RECORDER_DATA_DIR",
    "CPU_RECORDER_INTERVAL_SEC",
    "CPU_RECORDER_RETENTION_DAYS",
    "CPU_RECORDER_DOCKER_TIMEOUT_SEC",
    "CPU_RECORDER_DOCKER_CYCLE_TIMEOUT_SEC",
}
CASES = [
    (
        "docker-compose.phala.test.yaml",
        "feedling-io-test",
        "feedling_cpu_history_test",
    ),
    (
        "docker-compose.phala.yaml",
        "feedling-enclave-v2",
        "feedling_cpu_history",
    ),
]


@pytest.mark.parametrize(("filename", "cvm_name", "volume_name"), CASES)
def test_cpu_recorder_compose_is_private_bounded_and_dependency_isolated(
    filename, cvm_name, volume_name
):
    compose = load_yaml_strict(
        (ROOT / "deploy" / filename).read_text(), source_name=filename
    )
    services = compose["services"]
    proxy = services["cpu-socket-proxy"]
    recorder = services["cpu-recorder"]

    assert proxy["image"] == PROXY_IMAGE
    assert proxy["container_name"] == "cpu-socket-proxy"
    assert proxy["command"] == PROXY_COMMAND
    assert proxy["volumes"] == ["/var/run/docker.sock:/var/run/docker.sock:ro"]
    assert proxy["read_only"] is True
    assert proxy["cap_drop"] == ["ALL"]
    assert proxy["security_opt"] == ["no-new-privileges:true"]
    assert proxy["group_add"] == ["996"]
    assert proxy["cpus"] == 0.05
    assert proxy["mem_limit"] == "64m"
    assert proxy["networks"] == ["cpu-observability"]
    assert not proxy.get("ports")
    assert not proxy.get("expose")
    assert not proxy.get("environment")
    assert proxy["healthcheck"] == {
        "test": ["CMD", "./healthcheck"],
        "interval": "30s",
        "timeout": "5s",
        "retries": 3,
    }

    assert recorder["image"] == services["backend"]["image"]
    assert recorder["container_name"] == "cpu-recorder"
    assert recorder["command"] == ["python", "-u", "ops/cpu_recorder.py"]
    assert recorder["read_only"] is True
    assert recorder["cap_drop"] == ["ALL"]
    assert recorder["security_opt"] == ["no-new-privileges:true"]
    assert recorder["cpus"] == 0.10
    assert recorder["mem_limit"] == "128m"
    assert recorder["networks"] == ["cpu-observability"]
    assert recorder["volumes"] == [
        "/proc:/host/proc:ro",
        f"{volume_name}:/var/lib/feedling-cpu",
    ]
    assert all("docker.sock" not in mount for mount in recorder["volumes"])
    assert not recorder.get("ports")
    assert not recorder.get("expose")
    assert recorder["depends_on"] == {
        "cpu-socket-proxy": {"condition": "service_healthy"}
    }

    environment = recorder["environment"]
    assert set(environment) == ENVIRONMENT_KEYS
    assert environment == {
        "CPU_RECORDER_CVM_NAME": cvm_name,
        "CPU_RECORDER_DOCKER_URL": "http://cpu-socket-proxy:2375",
        "CPU_RECORDER_PROC_ROOT": "/host/proc",
        "CPU_RECORDER_DATA_DIR": "/var/lib/feedling-cpu",
        "CPU_RECORDER_INTERVAL_SEC": "60",
        "CPU_RECORDER_RETENTION_DAYS": "30",
        "CPU_RECORDER_DOCKER_TIMEOUT_SEC": "10",
        "CPU_RECORDER_DOCKER_CYCLE_TIMEOUT_SEC": "30",
    }
    assert not any(
        re.search(r"TOKEN|PASSWORD|SECRET|DATABASE|R2|KEY", key)
        for key in environment
    )

    business_services = set(services) - {"cpu-socket-proxy", "cpu-recorder"}
    for service_name in business_services:
        service = services[service_name]
        dependencies = service.get("depends_on", {})
        assert "cpu-socket-proxy" not in dependencies
        assert "cpu-recorder" not in dependencies
        assert "cpu-observability" not in service.get("networks", [])

    assert compose["networks"]["cpu-observability"] == {"internal": True}
    assert compose["volumes"][volume_name] == {"name": volume_name}
