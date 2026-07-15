#!/usr/bin/env python3
"""Launch exactly one disposable GitHub Actions qualification runner."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shlex
import stat
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from qa.aws.common import (  # noqa: E402
    CANONICAL_OWNER_ID,
    INSTANCE_TYPE,
    MANAGED_BY,
    MAX_JIT_CONFIG_BYTES,
    MAX_TTL_SECONDS,
    MAX_USER_DATA_BYTES,
    MIN_TTL_SECONDS,
    AwsCli,
    AwsRunnerError,
    checked_instance_id,
    instance_state,
    instances_from_response,
    load_private_ascii_file,
    tags_by_key,
    utc_timestamp,
    validate_aws_id,
    validate_commit_sha,
    validate_region,
    validate_repository,
    validate_run_attempt,
    validate_run_id,
)


RUNTIME_LOCK_PATH = Path(__file__).with_name("runtime_lock.json")
UBUNTU_AMI_NAME = re.compile(
    r"^ubuntu/images/hvm-ssd(?:-gp3)?/ubuntu-noble-24\.04-amd64-server-\d{8}(?:\.\d+)?$"
)
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SHA512 = re.compile(r"^[0-9a-f]{128}$")
VERSION = re.compile(r"^\d+\.\d+\.\d+$")
HTTPS_URL = re.compile(r"^https://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+$")
INSTANCE_STATES = ("pending", "running", "stopping", "stopped", "shutting-down")
REQUIRED_CODEX_MEMBERS = (
    "README.md",
    "package.json",
    "vendor/x86_64-unknown-linux-musl/bin/codex",
    "vendor/x86_64-unknown-linux-musl/bin/codex-code-mode-host",
    "vendor/x86_64-unknown-linux-musl/codex-package.json",
    "vendor/x86_64-unknown-linux-musl/codex-path/rg",
    "vendor/x86_64-unknown-linux-musl/codex-resources/bwrap",
    "vendor/x86_64-unknown-linux-musl/codex-resources/zsh/bin/zsh",
)


@dataclass(frozen=True)
class ArtifactLock:
    version: str
    url: str
    digest: str


@dataclass(frozen=True)
class RuntimeLock:
    actions_runner: ArtifactLock
    codex: ArtifactLock
    codex_members: tuple[str, ...]


@dataclass(frozen=True)
class NetworkContract:
    vpc_id: str
    root_device_name: str


def _strict_object(
    value: object, keys: set[str], description: str
) -> Mapping[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise AwsRunnerError(f"runtime lock has invalid {description}")
    return value


def load_runtime_lock(path: Path = RUNTIME_LOCK_PATH) -> RuntimeLock:
    try:
        raw = path.read_text(encoding="utf-8")
        document = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AwsRunnerError("cannot read the immutable runtime lock") from exc
    root = _strict_object(
        document, {"schema_version", "actions_runner", "codex"}, "root"
    )
    if root["schema_version"] != 1:
        raise AwsRunnerError("runtime lock schema is unsupported")
    runner = _strict_object(
        root["actions_runner"], {"version", "url", "sha256"}, "runner entry"
    )
    codex = _strict_object(
        root["codex"], {"version", "url", "sha512", "members"}, "Codex entry"
    )
    for item, name in ((runner, "runner"), (codex, "Codex")):
        if (
            not isinstance(item["version"], str)
            or VERSION.fullmatch(item["version"]) is None
        ):
            raise AwsRunnerError(f"runtime lock has invalid {name} version")
        if not isinstance(item["url"], str) or HTTPS_URL.fullmatch(item["url"]) is None:
            raise AwsRunnerError(f"runtime lock has invalid {name} URL")
    if (
        not isinstance(runner["sha256"], str)
        or SHA256.fullmatch(runner["sha256"]) is None
    ):
        raise AwsRunnerError("runtime lock has invalid runner digest")
    if (
        not isinstance(codex["sha512"], str)
        or SHA512.fullmatch(codex["sha512"]) is None
    ):
        raise AwsRunnerError("runtime lock has invalid Codex digest")
    members = codex["members"]
    if (
        not isinstance(members, list)
        or any(not isinstance(item, str) for item in members)
        or tuple(members) != REQUIRED_CODEX_MEMBERS
    ):
        raise AwsRunnerError("runtime lock has an unexpected Codex file set")
    expected_runner_url = (
        "https://github.com/actions/runner/releases/download/"
        f"v{runner['version']}/actions-runner-linux-x64-{runner['version']}.tar.gz"
    )
    expected_codex_url = (
        "https://registry.npmjs.org/@openai/codex/-/"
        f"codex-{codex['version']}-linux-x64.tgz"
    )
    if runner["url"] != expected_runner_url or codex["url"] != expected_codex_url:
        raise AwsRunnerError("runtime lock URL does not match its pinned version")
    return RuntimeLock(
        actions_runner=ArtifactLock(
            version=runner["version"], url=runner["url"], digest=runner["sha256"]
        ),
        codex=ArtifactLock(
            version=codex["version"], url=codex["url"], digest=codex["sha512"]
        ),
        codex_members=tuple(members),
    )


def _one(response: Mapping[str, Any], key: str, description: str) -> Mapping[str, Any]:
    values = response.get(key)
    if (
        not isinstance(values, list)
        or len(values) != 1
        or not isinstance(values[0], dict)
    ):
        raise AwsRunnerError(f"AWS returned an invalid {description}")
    return values[0]


def validate_ami(aws: AwsCli, ami_id: str) -> str:
    ami_id = validate_aws_id("AMI id", ami_id, "ami")
    image = _one(
        aws.json(
            "ec2",
            "describe-images",
            "--image-ids",
            ami_id,
            "--owners",
            CANONICAL_OWNER_ID,
        ),
        "Images",
        "AMI response",
    )
    if (
        image.get("ImageId") != ami_id
        or image.get("OwnerId") != CANONICAL_OWNER_ID
        or image.get("State") != "available"
        or image.get("Architecture") != "x86_64"
        or image.get("RootDeviceType") != "ebs"
        or image.get("VirtualizationType") != "hvm"
        or image.get("PlatformDetails") not in {None, "Linux/UNIX"}
        or not isinstance(image.get("Name"), str)
        or UBUNTU_AMI_NAME.fullmatch(image["Name"]) is None
    ):
        raise AwsRunnerError("AMI is not a pinned Canonical Ubuntu 24.04 x86_64 image")
    root_device = image.get("RootDeviceName")
    if (
        not isinstance(root_device, str)
        or re.fullmatch(r"/dev/[A-Za-z0-9._-]+", root_device) is None
    ):
        raise AwsRunnerError("AMI has an invalid root device")
    return root_device


def _route_is_public(route: object) -> bool:
    return (
        isinstance(route, dict)
        and route.get("DestinationCidrBlock") == "0.0.0.0/0"
        and route.get("State", "active") == "active"
        and isinstance(route.get("GatewayId"), str)
        and re.fullmatch(r"igw-[0-9a-f]{8,17}", route["GatewayId"]) is not None
    )


def _is_https_only_egress(permission: object) -> bool:
    if not isinstance(permission, dict):
        return False
    ranges = permission.get("IpRanges")
    if (
        not isinstance(ranges, list)
        or len(ranges) != 1
        or not isinstance(ranges[0], dict)
    ):
        return False
    ip_range = ranges[0]
    if (
        set(ip_range) - {"CidrIp", "Description"}
        or ip_range.get("CidrIp") != "0.0.0.0/0"
    ):
        return False
    if "Description" in ip_range and not isinstance(ip_range["Description"], str):
        return False
    return (
        permission.get("IpProtocol") == "tcp"
        and permission.get("FromPort") == 443
        and permission.get("ToPort") == 443
        and permission.get("Ipv6Ranges", []) == []
        and permission.get("PrefixListIds", []) == []
        and permission.get("UserIdGroupPairs", []) == []
    )


def validate_network(aws: AwsCli, subnet_id: str, security_group_id: str) -> str:
    subnet_id = validate_aws_id("subnet id", subnet_id, "subnet")
    security_group_id = validate_aws_id("security group id", security_group_id, "sg")
    subnet = _one(
        aws.json("ec2", "describe-subnets", "--subnet-ids", subnet_id),
        "Subnets",
        "subnet response",
    )
    vpc_id = subnet.get("VpcId")
    if (
        subnet.get("SubnetId") != subnet_id
        or subnet.get("State") != "available"
        or subnet.get("MapPublicIpOnLaunch") is not True
        or not isinstance(vpc_id, str)
        or re.fullmatch(r"vpc-[0-9a-f]{8,17}", vpc_id) is None
    ):
        raise AwsRunnerError("subnet is not an available public IPv4 subnet")

    route_response = aws.json(
        "ec2",
        "describe-route-tables",
        "--filters",
        f"Name=association.subnet-id,Values={subnet_id}",
    )
    route_tables = route_response.get("RouteTables")
    if not isinstance(route_tables, list):
        raise AwsRunnerError("AWS returned invalid route tables")
    if not route_tables:
        route_response = aws.json(
            "ec2",
            "describe-route-tables",
            "--filters",
            f"Name=vpc-id,Values={vpc_id}",
            "Name=association.main,Values=true",
        )
        route_tables = route_response.get("RouteTables")
    if (
        not isinstance(route_tables, list)
        or len(route_tables) != 1
        or not isinstance(route_tables[0], dict)
        or route_tables[0].get("VpcId") != vpc_id
        or not isinstance(route_tables[0].get("Routes"), list)
        or not any(_route_is_public(route) for route in route_tables[0]["Routes"])
    ):
        raise AwsRunnerError(
            "subnet does not have one unambiguous Internet Gateway route"
        )

    group = _one(
        aws.json("ec2", "describe-security-groups", "--group-ids", security_group_id),
        "SecurityGroups",
        "security group response",
    )
    ingress = group.get("IpPermissions")
    egress = group.get("IpPermissionsEgress")
    if (
        group.get("GroupId") != security_group_id
        or group.get("VpcId") != vpc_id
        or ingress != []
        or not isinstance(egress, list)
        or len(egress) != 1
        or not _is_https_only_egress(egress[0])
    ):
        raise AwsRunnerError(
            "security group must have no ingress and exactly IPv4 HTTPS egress"
        )
    return vpc_id


def _identity_tags(
    *,
    repository: str,
    run_id: str,
    run_attempt: int,
    target_sha: str,
    controller_sha: str,
    expires_at: str,
) -> dict[str, str]:
    return {
        "ManagedBy": MANAGED_BY,
        "Purpose": "api-key-e2e",
        "Repository": repository,
        "RunId": run_id,
        "RunAttempt": str(run_attempt),
        "TargetSHA": target_sha,
        "ControllerSHA": controller_sha,
        "ExpiresAt": expires_at,
    }


def _instance_filters(repository: str, run_id: str, run_attempt: int) -> list[str]:
    return [
        f"Name=tag:ManagedBy,Values={MANAGED_BY}",
        f"Name=tag:Repository,Values={repository}",
        f"Name=tag:RunId,Values={run_id}",
        f"Name=tag:RunAttempt,Values={run_attempt}",
        "Name=instance-state-name,Values=" + ",".join(INSTANCE_STATES),
    ]


def find_existing(
    aws: AwsCli, repository: str, run_id: str, run_attempt: int
) -> list[dict[str, Any]]:
    response = aws.json(
        "ec2",
        "describe-instances",
        "--filters",
        *_instance_filters(repository, run_id, run_attempt),
    )
    return instances_from_response(response)


def build_user_data(
    jit_config: str, runtime: RuntimeLock, *, ttl_seconds: int = MAX_TTL_SECONDS
) -> str:
    if not jit_config or any(
        ord(character) < 0x21 or ord(character) > 0x7E for character in jit_config
    ):
        raise AwsRunnerError("JIT configuration must be one line of printable ASCII")
    if (
        isinstance(ttl_seconds, bool)
        or not isinstance(ttl_seconds, int)
        or ttl_seconds < MIN_TTL_SECONDS
        or ttl_seconds > MAX_TTL_SECONDS
    ):
        raise AwsRunnerError("runner TTL is outside the allowed range")
    encoded_jit = base64.b64encode(jit_config.encode("ascii")).decode("ascii")
    expected_member_args = " ".join(
        shlex.quote(f"package/{member}") for member in runtime.codex_members
    )
    script = f"""#!/bin/bash
set -Eeuo pipefail
umask 077
cleanup() {{
  status=$?
  trap - EXIT
  rm -f /run/feedling-runner-jit /run/feedling-runner-jit.b64
  find /var/lib/cloud/instances -maxdepth 3 -type f \\( -name 'user-data.txt' -o -name 'user-data.txt.i' -o -path '*/scripts/part-*' \\) -delete 2>/dev/null || true
  sync || true
  shutdown -h now || poweroff -f || true
  exit "$status"
}}
trap cleanup EXIT
systemd-run --quiet --unit=feedling-runner-hard-expiry \
  --on-active={ttl_seconds}s --timer-property=AccuracySec=1min \
  --property=Type=oneshot /sbin/shutdown -h now
export DEBIAN_FRONTEND=noninteractive
source_count=0
for sources in /etc/apt/sources.list /etc/apt/sources.list.d/ubuntu.sources; do
  if [ -f "$sources" ]; then
    source_count=$((source_count + 1))
    sed -i 's#http://#https://#g' "$sources"
  fi
done
if [ "$source_count" -eq 0 ]; then
  echo 'Ubuntu apt sources are missing' >&2
  exit 1
fi
if grep -RhsEm1 '^(deb |URIs: )http://' /etc/apt/sources.list /etc/apt/sources.list.d >/dev/null 2>&1; then
  echo 'unencrypted apt source is forbidden' >&2
  exit 1
fi
apt-get update -q
apt-get install -y --no-install-recommends ca-certificates curl git jq tar gzip xz-utils unzip bubblewrap
install -d -m 0755 /opt/actions-runner /opt/codex
if ! id feedling-runner >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash --user-group feedling-runner
fi
install -d -m 0700 -o feedling-runner -g feedling-runner /opt/actions-runner/_tool
bootstrap_dir="$(mktemp -d /opt/feedling-bootstrap.XXXXXX)"
runner_archive="$bootstrap_dir/actions-runner.tgz"
codex_archive="$bootstrap_dir/codex.tgz"
curl --fail --show-error --silent --location --proto '=https' --tlsv1.2 --retry 3 --output "$runner_archive" '{runtime.actions_runner.url}'
printf '%s  %s\\n' '{runtime.actions_runner.digest}' "$runner_archive" | sha256sum --check --strict -
tar --extract --gzip --file "$runner_archive" --directory /opt/actions-runner --no-same-owner --no-same-permissions
/opt/actions-runner/bin/installdependencies.sh
curl --fail --show-error --silent --location --proto '=https' --tlsv1.2 --retry 3 --output "$codex_archive" '{runtime.codex.url}'
printf '%s  %s\\n' '{runtime.codex.digest}' "$codex_archive" | sha512sum --check --strict -
printf '%s\\n' {expected_member_args} | LC_ALL=C sort > "$bootstrap_dir/expected-codex-members"
tar --list --gzip --file "$codex_archive" | sed '/\\/$/d' | LC_ALL=C sort > "$bootstrap_dir/archive-codex-members"
cmp "$bootstrap_dir/expected-codex-members" "$bootstrap_dir/archive-codex-members"
tar --extract --gzip --file "$codex_archive" --directory "$bootstrap_dir" --no-same-owner --no-same-permissions
unsafe_member="$(find "$bootstrap_dir/package" \\( \\( ! -type d ! -type f \\) -o \\( -type f -links +1 \\) \\) -print -quit)"
if [ -n "$unsafe_member" ]; then
  echo 'unsafe Codex archive member' >&2
  exit 1
fi
cp -a "$bootstrap_dir/package/." /opt/codex/
chown -R root:root /opt/codex
chown -R feedling-runner:feedling-runner /opt/actions-runner
find /opt/codex -type d -exec chmod 0755 {{}} +
find /opt/codex -type f -exec chmod 0644 {{}} +
chmod 0755 \
  /opt/codex/vendor/x86_64-unknown-linux-musl/bin/codex \
  /opt/codex/vendor/x86_64-unknown-linux-musl/bin/codex-code-mode-host \
  /opt/codex/vendor/x86_64-unknown-linux-musl/codex-path/rg \
  /opt/codex/vendor/x86_64-unknown-linux-musl/codex-resources/bwrap \
  /opt/codex/vendor/x86_64-unknown-linux-musl/codex-resources/zsh/bin/zsh
ln -s /opt/codex/vendor/x86_64-unknown-linux-musl/bin/codex /usr/local/bin/codex
printf '%s\\n' 'RUNNER_TOOL_CACHE=/opt/actions-runner/_tool' > /opt/actions-runner/.env
chown feedling-runner:feedling-runner /opt/actions-runner/.env /opt/actions-runner/_tool
printf '%s' '{encoded_jit}' > /run/feedling-runner-jit.b64
base64 --decode /run/feedling-runner-jit.b64 > /run/feedling-runner-jit
chown feedling-runner:feedling-runner /run/feedling-runner-jit
chmod 0600 /run/feedling-runner-jit
rm -rf "$bootstrap_dir" /run/feedling-runner-jit.b64
find /var/lib/cloud/instances -maxdepth 3 -type f \\( -name 'user-data.txt' -o -name 'user-data.txt.i' -o -path '*/scripts/part-*' \\) -delete 2>/dev/null || true
runuser -u feedling-runner -- env HOME=/home/feedling-runner PATH=/usr/local/bin:/usr/bin:/bin RUNNER_TOOL_CACHE=/opt/actions-runner/_tool /bin/bash -c '
  set -Eeuo pipefail
  cd /opt/actions-runner
  jit_config="$(</run/feedling-runner-jit)"
  rm -f /run/feedling-runner-jit
  exec ./run.sh --jitconfig "$jit_config"
'
"""
    if "set -x" in script or len(script.encode("utf-8")) > MAX_USER_DATA_BYTES:
        raise AwsRunnerError("runner bootstrap user-data is unsafe or too large")
    return script


def build_run_request(
    *,
    ami_id: str,
    subnet_id: str,
    security_group_id: str,
    root_device_name: str,
    user_data: str,
    tags: Mapping[str, str],
    client_token: str,
) -> dict[str, Any]:
    tag_list = [{"Key": key, "Value": value} for key, value in sorted(tags.items())]
    return {
        "ImageId": ami_id,
        "InstanceType": INSTANCE_TYPE,
        "MinCount": 1,
        "MaxCount": 1,
        "ClientToken": client_token,
        "InstanceInitiatedShutdownBehavior": "terminate",
        "MetadataOptions": {
            # Endpoint-disabled would also disable Canonical cloud-init's only
            # JIT user-data delivery path. Require token-authenticated IMDSv2.
            "HttpEndpoint": "enabled",
            "HttpTokens": "required",
            "HttpPutResponseHopLimit": 1,
            "InstanceMetadataTags": "disabled",
        },
        "NetworkInterfaces": [
            {
                "AssociatePublicIpAddress": True,
                "DeleteOnTermination": True,
                "DeviceIndex": 0,
                "Groups": [security_group_id],
                "SubnetId": subnet_id,
            }
        ],
        "BlockDeviceMappings": [
            {
                "DeviceName": root_device_name,
                "Ebs": {
                    "DeleteOnTermination": True,
                    "Encrypted": True,
                    "VolumeSize": 40,
                    "VolumeType": "gp3",
                },
            }
        ],
        "TagSpecifications": [
            {"ResourceType": "instance", "Tags": tag_list},
            {"ResourceType": "volume", "Tags": tag_list},
        ],
        "UserData": base64.b64encode(user_data.encode("utf-8")).decode("ascii"),
    }


def _append_outputs(path: Path, values: Mapping[str, str]) -> None:
    if not path.is_absolute():
        raise AwsRunnerError("GitHub output path must be absolute")
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise AwsRunnerError("cannot open GitHub output file") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise AwsRunnerError("GitHub output file is unsafe")
        payload = "".join(f"{key}={value}\n" for key, value in values.items()).encode(
            "ascii"
        )
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise AwsRunnerError("cannot write GitHub output file")
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rollback(
    aws: AwsCli,
    *,
    repository: str,
    run_id: str,
    run_attempt: int,
    instance_id: str | None,
    expected_tags: Mapping[str, str],
) -> None:
    try:
        candidates = find_existing(aws, repository, run_id, run_attempt)
        ids: list[str] = []
        for candidate in candidates:
            candidate_id = checked_instance_id(candidate)
            tags = tags_by_key(candidate)
            if (
                candidate.get("InstanceType") == INSTANCE_TYPE
                and all(tags.get(key) == value for key, value in expected_tags.items())
                and (instance_id is None or candidate_id == instance_id)
            ):
                ids.append(candidate_id)
        if ids:
            aws.run("ec2", "terminate-instances", "--instance-ids", *sorted(set(ids)))
    except AwsRunnerError:
        # Preserve the primary launch failure. Exact tags plus expiry let the
        # independent cleanup/reaper retry a control-plane rollback failure.
        return


def launch(
    aws: AwsCli,
    *,
    ami_id: str,
    subnet_id: str,
    security_group_id: str,
    repository: str,
    run_id: str,
    run_attempt: int,
    target_sha: str,
    controller_sha: str,
    jit_config_path: Path,
    ttl_seconds: int,
    github_output: Path | None,
    now: datetime | None = None,
) -> tuple[str, str]:
    repository = validate_repository(repository)
    run_id = validate_run_id(run_id)
    run_attempt = validate_run_attempt(run_attempt)
    target_sha = validate_commit_sha(target_sha)
    controller_sha = validate_commit_sha(controller_sha)
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
        raise AwsRunnerError("invalid runner TTL")
    if ttl_seconds < MIN_TTL_SECONDS or ttl_seconds > MAX_TTL_SECONDS:
        raise AwsRunnerError("runner TTL is outside the allowed range")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise AwsRunnerError("current time must be timezone-aware")
    expires_at = utc_timestamp(current + timedelta(seconds=ttl_seconds))
    runtime = load_runtime_lock()
    jit_config = load_private_ascii_file(
        jit_config_path, max_bytes=MAX_JIT_CONFIG_BYTES
    )
    root_device_name = validate_ami(aws, ami_id)
    validate_network(aws, subnet_id, security_group_id)
    if find_existing(aws, repository, run_id, run_attempt):
        raise AwsRunnerError("a live runner already exists for this run attempt")
    tags = _identity_tags(
        repository=repository,
        run_id=run_id,
        run_attempt=run_attempt,
        target_sha=target_sha,
        controller_sha=controller_sha,
        expires_at=expires_at,
    )
    client_token = (
        "feedling-e2e-"
        + hashlib.sha256(
            f"{repository}\0{run_id}\0{run_attempt}".encode("utf-8")
        ).hexdigest()[:48]
    )
    request = build_run_request(
        ami_id=validate_aws_id("AMI id", ami_id, "ami"),
        subnet_id=validate_aws_id("subnet id", subnet_id, "subnet"),
        security_group_id=validate_aws_id("security group id", security_group_id, "sg"),
        root_device_name=root_device_name,
        user_data=build_user_data(jit_config, runtime, ttl_seconds=ttl_seconds),
        tags=tags,
        client_token=client_token,
    )
    instance_id: str | None = None
    try:
        response = aws.json_input("ec2", "run-instances", request)
        instances = instances_from_response(response)
        if len(instances) != 1:
            raise AwsRunnerError("run-instances did not return exactly one instance")
        instance_id = checked_instance_id(instances[0])
        outputs = {"instance_id": instance_id, "expires_at": expires_at}
        if github_output is not None:
            _append_outputs(github_output, outputs)
        else:
            print(json.dumps(outputs, separators=(",", ":"), sort_keys=True))
        aws.run("ec2", "wait", "instance-running", "--instance-ids", instance_id)
        aws.run("ec2", "wait", "instance-status-ok", "--instance-ids", instance_id)
        described = aws.json("ec2", "describe-instances", "--instance-ids", instance_id)
        verified = instances_from_response(described)
        if len(verified) != 1 or checked_instance_id(verified[0]) != instance_id:
            raise AwsRunnerError("launched instance could not be verified")
        verified_tags = tags_by_key(verified[0])
        if (
            instance_state(verified[0]) != "running"
            or verified[0].get("InstanceType") != INSTANCE_TYPE
            or any(verified_tags.get(key) != value for key, value in tags.items())
        ):
            raise AwsRunnerError("launched instance violated the runner contract")
        return instance_id, expires_at
    except BaseException:
        _rollback(
            aws,
            repository=repository,
            run_id=run_id,
            run_attempt=run_attempt,
            instance_id=instance_id,
            expected_tags=tags,
        )
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", required=True)
    parser.add_argument("--profile")
    parser.add_argument("--ami-id", required=True)
    parser.add_argument("--subnet-id", required=True)
    parser.add_argument("--security-group-id", required=True)
    parser.add_argument(
        "--repository", "--github-repository", dest="repository", required=True
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True, type=int)
    parser.add_argument("--target-sha", required=True)
    parser.add_argument("--controller-sha", required=True)
    parser.add_argument("--jit-config-file", required=True, type=Path)
    parser.add_argument("--ttl-seconds", type=int, default=MAX_TTL_SECONDS)
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--aws-timeout-seconds", type=int, default=900)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.aws_timeout_seconds < 60 or args.aws_timeout_seconds > 1_800:
            raise AwsRunnerError("invalid AWS timeout")
        aws = AwsCli(
            validate_region(args.region),
            profile=args.profile,
            timeout_seconds=args.aws_timeout_seconds,
        )
        launch(
            aws,
            ami_id=args.ami_id,
            subnet_id=args.subnet_id,
            security_group_id=args.security_group_id,
            repository=args.repository,
            run_id=args.run_id,
            run_attempt=args.run_attempt,
            target_sha=args.target_sha,
            controller_sha=args.controller_sha,
            jit_config_path=args.jit_config_file,
            ttl_seconds=args.ttl_seconds,
            github_output=args.github_output,
        )
    except AwsRunnerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
