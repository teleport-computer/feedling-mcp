from pathlib import Path
import re
import pytest
from tools.strict_yaml import load_yaml_strict

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("suffix,environment,cvm,volume", [("", "prod", "feedling-enclave-v2", "feedling_log_history"),
    (".test", "test", "feedling-io-test", "feedling_log_history_test")])
def test_log_collector_boundary(suffix, environment, cvm, volume):
    compose = load_yaml_strict((ROOT / f"deploy/docker-compose.phala{suffix}.yaml").read_text())
    services = compose["services"]
    service = services["log-shipper"]
    assert service["image"] == services["backend"]["image"] == services["enclave"]["image"]
    assert service["command"] == ["python", "-u", "ops/log_shipper.py"]
    assert service["read_only"] is True and service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert service["cpus"] == 0.1 and service["mem_limit"] == "128m"
    assert not service.get("ports") and not service.get("privileged")
    assert service["volumes"] == [volume + ":/var/lib/feedling-logs"]
    assert compose["volumes"][volume] == {"name": volume}
    assert service["networks"] == ["cpu-observability", "log-egress"]
    assert compose["networks"]["cpu-observability"]["internal"] is True
    assert compose["networks"]["log-egress"]["internal"] is False
    assert all("log-egress" not in other.get("networks", []) for name, other in services.items() if name != "log-shipper")
    assert service["depends_on"] == {"cpu-socket-proxy": {"condition": "service_healthy"}}
    env = service["environment"]
    assert env["LOG_SHIPPER_ENV"] == environment and env["LOG_SHIPPER_CVM_NAME"] == cvm
    assert env["LOG_SHIPPER_RETENTION_DAYS"] == "30"
    assert env["LOG_SHIPPER_CONTAINERS"].split(",") == [compose["name"]+"-enclave-1", compose["name"]+"-enclave-domain-1"]
    assert env["CPU_RECORDER_DOCKER_URL"] == "http://cpu-socket-proxy:2375"
    assert env["R2_LOGS_BUCKET"] == "${R2_LOGS_BUCKET:-}"
    for key in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        assert env[key] == "${"+key+":-}"
    proxy = services["cpu-socket-proxy"]
    assert [arg for arg in proxy["command"] if arg.startswith("-allowGET")] == [
        "-allowGET=/containers/json", r"-allowGET=/containers/[0-9a-f]{64}/stats", r"-allowGET=/containers/[0-9a-f]{64}/logs"]
    assert [arg for arg in proxy["command"] if arg.startswith("-allowfrom=")] == ["-allowfrom=cpu-recorder,log-shipper"]
    assert proxy["networks"] == ["cpu-observability"]
    assert proxy["volumes"] == ["/var/run/docker.sock:/var/run/docker.sock:ro"]


def test_ci_deploy_bucket_secret_and_flag_are_both_wired():
    workflow = load_yaml_strict((ROOT / ".github/workflows/ci.yml").read_text())
    wired = []
    for job_name, job in workflow["jobs"].items():
        for step in job.get("steps", []):
            if "R2_LOGS_BUCKET" in step.get("env", {}):
                wired.append((job_name, step))
    assert len(wired) == 2
    assert {step["env"]["R2_LOGS_BUCKET"] for _, step in wired} == {
        "${{ secrets.R2_LOGS_BUCKET }}", "${{ secrets.TEST_R2_LOGS_BUCKET }}"}
    for _, step in wired:
        assert '-e "R2_LOGS_BUCKET=$R2_LOGS_BUCKET"' in step["run"]
        assert "phala deploy" in step["run"]
    assert sum('tests/test_log_shipper.py' in s.get('run', '') and 'tests/test_log_shipper_compose.py' in s.get('run', '')
               for j in workflow['jobs'].values() for s in j.get('steps', [])) == 1


def test_image_has_writable_volume_mountpoint_for_nonroot():
    dockerfile = (ROOT / 'deploy/Dockerfile').read_text()
    assert re.search(r'mkdir -p [^\n]*/var/lib/feedling-logs', dockerfile)
    assert re.search(r'chown -R feedling:feedling [^\n]*/var/lib/feedling-logs', dockerfile)
    assert 'COPY ops/ ./ops/' in dockerfile and 'USER feedling' in dockerfile
