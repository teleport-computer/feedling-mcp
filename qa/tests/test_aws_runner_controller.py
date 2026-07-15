from __future__ import annotations

import base64
import json
import os
import stat
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from qa.aws import reap as reap_module
from qa.aws.common import (
    INSTANCE_TYPE,
    MANAGED_BY,
    AwsCli,
    AwsRunnerError,
    load_private_ascii_file,
    validate_region,
)
from qa.aws.launch import (
    REQUIRED_CODEX_MEMBERS,
    _is_https_only_egress,
    build_run_request,
    build_user_data,
    launch,
    load_runtime_lock,
    validate_network,
)
from qa.aws.reap import reap
from qa.aws.terminate import terminate


AMI_ID = "ami-0123456789abcdef0"
SUBNET_ID = "subnet-0123456789abcdef0"
SECURITY_GROUP_ID = "sg-0123456789abcdef0"
INSTANCE_ID = "i-0123456789abcdef0"
VPC_ID = "vpc-0123456789abcdef0"
REPOSITORY = "teleport-computer/feedling-mcp"
RUN_ID = "123456789"
RUN_ATTEMPT = 2
TARGET_SHA = "a" * 40
CONTROLLER_SHA = "b" * 40
NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


def test_region_validator_accepts_commercial_aws_and_rejects_govcloud():
    assert validate_region("us-east-1") == "us-east-1"
    assert validate_region("eu-central-1") == "eu-central-1"
    with pytest.raises(AwsRunnerError, match="invalid AWS region"):
        validate_region("us-gov-west-1")


def _private_file(path: Path, value: str = "sensitive-jit-token") -> Path:
    path.write_text(value, encoding="ascii")
    path.chmod(0o600)
    return path


def _tags(*, expires_at: str = "2026-07-15T14:00:00Z") -> dict[str, str]:
    return {
        "ManagedBy": MANAGED_BY,
        "Purpose": "api-key-e2e",
        "Repository": REPOSITORY,
        "RunId": RUN_ID,
        "RunAttempt": str(RUN_ATTEMPT),
        "TargetSHA": TARGET_SHA,
        "ControllerSHA": CONTROLLER_SHA,
        "ExpiresAt": expires_at,
    }


def _instance(
    *,
    instance_id: str = INSTANCE_ID,
    state: str = "running",
    tags: dict[str, str] | None = None,
    launch_time: str = "2026-07-15T12:00:00Z",
) -> dict[str, Any]:
    return {
        "InstanceId": instance_id,
        "InstanceType": INSTANCE_TYPE,
        "State": {"Name": state},
        "LaunchTime": launch_time,
        "Tags": [
            {"Key": key, "Value": value} for key, value in (tags or _tags()).items()
        ],
    }


def _reservations(*instances: dict[str, Any]) -> dict[str, Any]:
    return {"Reservations": [] if not instances else [{"Instances": list(instances)}]}


def test_aws_cli_uses_private_request_file_and_sanitized_environment(
    monkeypatch: pytest.MonkeyPatch,
):
    captured: dict[str, Any] = {}
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "access")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("AWS_ENDPOINT_URL", "https://attacker.invalid")
    monkeypatch.setenv("PYTHONPATH", "/tmp/attacker")
    monkeypatch.setenv("LD_PRELOAD", "/tmp/attacker.so")

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["environment"] = kwargs["env"]
        file_argument = next(item for item in command if item.startswith("file://"))
        request_path = Path(file_argument.removeprefix("file://"))
        captured["request_path"] = request_path
        captured["mode"] = stat.S_IMODE(request_path.stat().st_mode)
        captured["request"] = json.loads(request_path.read_text(encoding="utf-8"))
        return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = AwsCli("us-east-1").json_input(
        "ec2", "run-instances", {"UserData": "sensitive-request-value"}
    )

    assert result == {}
    assert captured["mode"] == 0o600
    assert captured["request"] == {"UserData": "sensitive-request-value"}
    assert "sensitive-request-value" not in " ".join(captured["command"])
    assert not captured["request_path"].exists()
    environment = captured["environment"]
    assert environment["AWS_ACCESS_KEY_ID"] == "access"
    assert environment["AWS_SECRET_ACCESS_KEY"] == "secret"
    assert environment["AWS_EC2_METADATA_DISABLED"] == "true"
    assert environment["AWS_IGNORE_CONFIGURED_ENDPOINT_URLS"] == "true"
    assert environment["AWS_PAGER"] == ""
    assert "AWS_ENDPOINT_URL" not in environment
    assert "PYTHONPATH" not in environment
    assert "LD_PRELOAD" not in environment


def test_private_jit_reader_rejects_links_permissions_and_non_ascii(tmp_path: Path):
    private = _private_file(tmp_path / "jit")
    assert load_private_ascii_file(private, max_bytes=100) == "sensitive-jit-token"

    private.chmod(0o640)
    with pytest.raises(AwsRunnerError, match="unsafe"):
        load_private_ascii_file(private, max_bytes=100)
    private.chmod(0o600)

    symlink = tmp_path / "jit-link"
    symlink.symlink_to(private)
    with pytest.raises(AwsRunnerError):
        load_private_ascii_file(symlink, max_bytes=100)

    hardlink = tmp_path / "jit-hardlink"
    hardlink.hardlink_to(private)
    with pytest.raises(AwsRunnerError, match="unsafe"):
        load_private_ascii_file(private, max_bytes=100)

    hardlink.unlink()
    private.write_bytes(b"\xff")
    private.chmod(0o600)
    with pytest.raises(AwsRunnerError, match="ASCII"):
        load_private_ascii_file(private, max_bytes=100)


def test_bootstrap_is_locked_non_root_injection_safe_and_self_terminating():
    runtime = load_runtime_lock()
    jit = "sensitive-jit-token"
    script = build_user_data(jit, runtime, ttl_seconds=3_600)

    assert jit not in script
    assert base64.b64encode(jit.encode("ascii")).decode("ascii") in script
    assert "set -x" not in script
    assert "./run.sh --jitconfig" in script
    assert "config.sh --jitconfig" not in script
    assert "runuser -u feedling-runner" in script
    assert "shutdown -h now" in script
    assert "--on-active=3600s" in script
    assert script.index("systemd-run") < script.index("apt-get update")
    assert script.index("s#http://#https://#g") < script.index("apt-get update")
    assert "unencrypted apt source is forbidden" in script
    assert "Ubuntu apt sources are missing" in script
    assert "| grep -q" not in script
    assert "-print -quit" in script
    assert script.index("useradd --create-home") < script.index(
        "install -d -m 0700 -o feedling-runner"
    )
    assert runtime.actions_runner.url in script
    assert runtime.actions_runner.digest in script
    assert runtime.codex.url in script
    assert runtime.codex.digest in script
    assert "sha256sum --check --strict" in script
    assert "sha512sum --check --strict" in script
    assert "user-data.txt" in script
    assert "RUNNER_TOOL_CACHE=/opt/actions-runner/_tool" in script
    assert len(script.encode("utf-8")) <= 16_384
    subprocess.run(
        ["bash", "-n"], input=script, check=True, capture_output=True, text=True
    )

    member_line = next(
        line
        for line in script.splitlines()
        if line.startswith("printf '%s\\n' package/")
    )
    command = member_line.rsplit(" > ", 1)[0]
    completed = subprocess.run(
        ["bash", "-c", command], check=True, capture_output=True, text=True
    )
    assert completed.stdout.splitlines() == sorted(
        f"package/{member}" for member in REQUIRED_CODEX_MEMBERS
    )


def test_run_request_enforces_exact_ec2_shape_and_imdsv2_only():
    request = build_run_request(
        ami_id=AMI_ID,
        subnet_id=SUBNET_ID,
        security_group_id=SECURITY_GROUP_ID,
        root_device_name="/dev/sda1",
        user_data="#!/bin/bash\ntrue\n",
        tags=_tags(),
        client_token="feedling-e2e-token",
    )

    assert request["InstanceType"] == "m7i.xlarge"
    assert request["MinCount"] == request["MaxCount"] == 1
    assert request["InstanceInitiatedShutdownBehavior"] == "terminate"
    assert request["MetadataOptions"] == {
        "HttpEndpoint": "enabled",
        "HttpTokens": "required",
        "HttpPutResponseHopLimit": 1,
        "InstanceMetadataTags": "disabled",
    }
    assert request["NetworkInterfaces"] == [
        {
            "AssociatePublicIpAddress": True,
            "DeleteOnTermination": True,
            "DeviceIndex": 0,
            "Groups": [SECURITY_GROUP_ID],
            "SubnetId": SUBNET_ID,
        }
    ]
    assert request["BlockDeviceMappings"] == [
        {
            "DeviceName": "/dev/sda1",
            "Ebs": {
                "DeleteOnTermination": True,
                "Encrypted": True,
                "VolumeSize": 40,
                "VolumeType": "gp3",
            },
        }
    ]
    assert "KeyName" not in request
    assert "IamInstanceProfile" not in request
    assert base64.b64decode(request["UserData"]) == b"#!/bin/bash\ntrue\n"
    assert {item["ResourceType"] for item in request["TagSpecifications"]} == {
        "instance",
        "volume",
    }


def test_security_group_egress_contract_rejects_protocol_and_target_drift():
    allowed = {
        "IpProtocol": "tcp",
        "FromPort": 443,
        "ToPort": 443,
        "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "HTTPS bootstrap only"}],
        "Ipv6Ranges": [],
        "PrefixListIds": [],
        "UserIdGroupPairs": [],
    }
    assert _is_https_only_egress(allowed)
    for mutation in (
        {**allowed, "IpProtocol": "-1", "FromPort": None, "ToPort": None},
        {**allowed, "FromPort": 80, "ToPort": 443},
        {**allowed, "Ipv6Ranges": [{"CidrIpv6": "::/0"}]},
        {**allowed, "PrefixListIds": [{"PrefixListId": "pl-12345678"}]},
        {**allowed, "UserIdGroupPairs": [{"GroupId": SECURITY_GROUP_ID}]},
        {**allowed, "IpRanges": [{"CidrIp": "10.0.0.0/8"}]},
    ):
        assert not _is_https_only_egress(mutation)

    duplicate = LaunchAws(Path("/unused"), egress=[allowed, allowed])
    with pytest.raises(AwsRunnerError, match="exactly IPv4 HTTPS"):
        validate_network(duplicate, SUBNET_ID, SECURITY_GROUP_ID)  # type: ignore[arg-type]


class LaunchAws:
    def __init__(
        self,
        output_path: Path,
        *,
        fail_wait: bool = False,
        egress: list[dict[str, Any]] | None = None,
    ):
        self.output_path = output_path
        self.fail_wait = fail_wait
        self.egress = (
            egress
            if egress is not None
            else [
                {
                    "IpProtocol": "tcp",
                    "FromPort": 443,
                    "ToPort": 443,
                    "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                }
            ]
        )
        self.request: dict[str, Any] | None = None
        self.run_calls: list[tuple[str, ...]] = []
        self.created = False

    def json(self, *arguments: str) -> dict[str, Any]:
        operation = arguments[1]
        if operation == "describe-images":
            return {
                "Images": [
                    {
                        "ImageId": AMI_ID,
                        "OwnerId": "099720109477",
                        "State": "available",
                        "Architecture": "x86_64",
                        "RootDeviceType": "ebs",
                        "VirtualizationType": "hvm",
                        "PlatformDetails": "Linux/UNIX",
                        "Name": "ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-20260701",
                        "RootDeviceName": "/dev/sda1",
                    }
                ]
            }
        if operation == "describe-subnets":
            return {
                "Subnets": [
                    {
                        "SubnetId": SUBNET_ID,
                        "VpcId": VPC_ID,
                        "State": "available",
                        "MapPublicIpOnLaunch": True,
                    }
                ]
            }
        if operation == "describe-route-tables":
            return {
                "RouteTables": [
                    {
                        "VpcId": VPC_ID,
                        "Routes": [
                            {
                                "DestinationCidrBlock": "0.0.0.0/0",
                                "GatewayId": "igw-0123456789abcdef0",
                                "State": "active",
                            }
                        ],
                    }
                ]
            }
        if operation == "describe-security-groups":
            return {
                "SecurityGroups": [
                    {
                        "GroupId": SECURITY_GROUP_ID,
                        "VpcId": VPC_ID,
                        "IpPermissions": [],
                        "IpPermissionsEgress": self.egress,
                    }
                ]
            }
        if operation == "describe-instances":
            if "--instance-ids" in arguments:
                assert self.request is not None
                tag_spec = next(
                    item
                    for item in self.request["TagSpecifications"]
                    if item["ResourceType"] == "instance"
                )
                tags = {item["Key"]: item["Value"] for item in tag_spec["Tags"]}
                return _reservations(_instance(tags=tags))
            if self.created and self.fail_wait:
                assert self.request is not None
                tag_spec = self.request["TagSpecifications"][0]
                tags = {item["Key"]: item["Value"] for item in tag_spec["Tags"]}
                return _reservations(_instance(tags=tags))
            return _reservations()
        raise AssertionError(arguments)

    def json_input(
        self, service: str, operation: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        assert (service, operation) == ("ec2", "run-instances")
        self.request = payload
        self.created = True
        return _reservations({"InstanceId": INSTANCE_ID})

    def run(self, *arguments: str) -> None:
        self.run_calls.append(arguments)
        if arguments[:3] == ("ec2", "wait", "instance-running"):
            assert f"instance_id={INSTANCE_ID}" in self.output_path.read_text(
                encoding="ascii"
            )
            if self.fail_wait:
                raise AwsRunnerError("wait failed")


def test_launch_validates_then_waits_and_writes_output_before_wait(tmp_path: Path):
    output = _private_file(tmp_path / "github-output", "")
    jit = _private_file(tmp_path / "jit")
    aws = LaunchAws(output)

    result = launch(
        aws,  # type: ignore[arg-type]
        ami_id=AMI_ID,
        subnet_id=SUBNET_ID,
        security_group_id=SECURITY_GROUP_ID,
        repository=REPOSITORY,
        run_id=RUN_ID,
        run_attempt=RUN_ATTEMPT,
        target_sha=TARGET_SHA,
        controller_sha=CONTROLLER_SHA,
        jit_config_path=jit,
        ttl_seconds=3_600,
        github_output=output,
        now=NOW,
    )

    assert result == (INSTANCE_ID, "2026-07-15T13:00:00Z")
    assert output.read_text(encoding="ascii").splitlines() == [
        f"instance_id={INSTANCE_ID}",
        "expires_at=2026-07-15T13:00:00Z",
    ]
    assert aws.request is not None
    assert aws.request["MinCount"] == aws.request["MaxCount"] == 1
    assert aws.run_calls[:2] == [
        ("ec2", "wait", "instance-running", "--instance-ids", INSTANCE_ID),
        ("ec2", "wait", "instance-status-ok", "--instance-ids", INSTANCE_ID),
    ]


def test_launch_rolls_back_exact_tagged_instance_when_wait_fails(tmp_path: Path):
    output = _private_file(tmp_path / "github-output", "")
    aws = LaunchAws(output, fail_wait=True)
    with pytest.raises(AwsRunnerError, match="wait failed"):
        launch(
            aws,  # type: ignore[arg-type]
            ami_id=AMI_ID,
            subnet_id=SUBNET_ID,
            security_group_id=SECURITY_GROUP_ID,
            repository=REPOSITORY,
            run_id=RUN_ID,
            run_attempt=RUN_ATTEMPT,
            target_sha=TARGET_SHA,
            controller_sha=CONTROLLER_SHA,
            jit_config_path=_private_file(tmp_path / "jit"),
            ttl_seconds=3_600,
            github_output=output,
            now=NOW,
        )
    assert (
        "ec2",
        "terminate-instances",
        "--instance-ids",
        INSTANCE_ID,
    ) in aws.run_calls


class CleanupAws:
    def __init__(self, instances: list[dict[str, Any]]):
        self.instances = instances
        self.run_calls: list[tuple[str, ...]] = []

    def json(self, *arguments: str) -> dict[str, Any]:
        instance_filter = next(
            (item for item in arguments if item.startswith("Name=instance-id,Values=")),
            None,
        )
        if instance_filter is None:
            return _reservations(*self.instances)
        instance_id = instance_filter.split("=", 2)[-1]
        return _reservations(
            *(item for item in self.instances if item["InstanceId"] == instance_id)
        )

    def run(self, *arguments: str) -> None:
        self.run_calls.append(arguments)


def test_terminate_discovers_all_exact_matches_and_is_idempotent():
    second_id = "i-1123456789abcdef0"
    aws = CleanupAws(
        [_instance(), _instance(instance_id=second_id, state="shutting-down")]
    )
    ids = terminate(
        aws,  # type: ignore[arg-type]
        repository=REPOSITORY,
        run_id=RUN_ID,
        run_attempt=RUN_ATTEMPT,
    )
    assert ids == [INSTANCE_ID, second_id]
    assert (
        "ec2",
        "terminate-instances",
        "--instance-ids",
        INSTANCE_ID,
    ) in aws.run_calls
    assert aws.run_calls[-1] == (
        "ec2",
        "wait",
        "instance-terminated",
        "--instance-ids",
        INSTANCE_ID,
        second_id,
    )

    empty = CleanupAws([])
    assert (
        terminate(
            empty,  # type: ignore[arg-type]
            repository=REPOSITORY,
            run_id=RUN_ID,
            run_attempt=RUN_ATTEMPT,
        )
        == []
    )
    assert empty.run_calls == []


def test_terminate_exact_id_refuses_partial_or_mismatched_identity():
    bad_tags = _tags()
    bad_tags["ControllerSHA"] = "not-a-sha"
    aws = CleanupAws([_instance(tags=bad_tags)])
    with pytest.raises(AwsRunnerError):
        terminate(
            aws,  # type: ignore[arg-type]
            repository=REPOSITORY,
            run_id=RUN_ID,
            run_attempt=RUN_ATTEMPT,
            instance_id=INSTANCE_ID,
        )
    assert aws.run_calls == []


def test_terminate_rejects_wrong_id_returned_by_fresh_guard_read():
    wrong_id = "i-1123456789abcdef0"

    class WrongFreshAws(CleanupAws):
        def json(self, *arguments: str) -> dict[str, Any]:
            if any(item.startswith("Name=instance-id,Values=") for item in arguments):
                return _reservations(_instance(instance_id=wrong_id))
            return super().json(*arguments)

    aws = WrongFreshAws([_instance()])
    with pytest.raises(AwsRunnerError, match="wrong instance"):
        terminate(
            aws,  # type: ignore[arg-type]
            repository=REPOSITORY,
            run_id=RUN_ID,
            run_attempt=RUN_ATTEMPT,
        )
    assert aws.run_calls == []


def test_reaper_uses_tag_expiry_and_independent_launch_time_cap(
    monkeypatch: pytest.MonkeyPatch,
):
    expired = _instance(tags=_tags(expires_at="2026-07-15T12:30:00Z"))
    future = _instance(
        instance_id="i-1123456789abcdef0",
        tags=_tags(expires_at="2026-07-15T15:00:00Z"),
    )
    aws = CleanupAws([expired, future])
    calls: list[str] = []

    def fake_terminate(aws: Any, **kwargs: Any) -> list[str]:
        calls.append(kwargs["instance_id"])
        return [kwargs["instance_id"]]

    monkeypatch.setattr(reap_module, "terminate", fake_terminate)
    assert reap(aws, repository=REPOSITORY, now=NOW + timedelta(hours=1)) == [
        INSTANCE_ID
    ]
    assert calls == [INSTANCE_ID]

    too_late = _instance(
        tags=_tags(expires_at="2026-07-16T12:00:00Z"),
        launch_time="2026-07-15T00:00:00Z",
    )
    calls.clear()
    with pytest.raises(AwsRunnerError, match="refused"):
        reap(
            CleanupAws([too_late]),  # type: ignore[arg-type]
            repository=REPOSITORY,
            now=datetime(2026, 7, 15, 7, 0, tzinfo=timezone.utc),
        )
    assert calls == [INSTANCE_ID]


def test_reaper_rejects_malformed_or_future_launch_time(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        reap_module,
        "terminate",
        lambda *args, **kwargs: pytest.fail("unsafe instance was terminated"),
    )
    for launch_time in ("not-a-time", "2026-07-16T00:00:00Z"):
        with pytest.raises(AwsRunnerError, match="refused"):
            reap(
                CleanupAws([_instance(launch_time=launch_time)]),  # type: ignore[arg-type]
                repository=REPOSITORY,
                now=NOW,
            )
