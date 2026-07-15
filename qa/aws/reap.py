#!/usr/bin/env python3
"""Terminate only expired, fully tagged qualification runners."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from qa.aws.common import (  # noqa: E402
    INSTANCE_TYPE,
    MANAGED_BY,
    MAX_TTL_SECONDS,
    AwsCli,
    AwsRunnerError,
    checked_instance_id,
    instances_from_response,
    parse_utc_timestamp,
    tags_by_key,
    validate_commit_sha,
    validate_region,
    validate_repository,
    validate_run_attempt,
    validate_run_id,
)
from qa.aws.terminate import ACTIVE_STATES, terminate  # noqa: E402


LAUNCH_TIME_SKEW = timedelta(minutes=5)


def _launch_time(instance: dict[str, Any]) -> datetime:
    value = instance.get("LaunchTime")
    if not isinstance(value, str):
        raise AwsRunnerError("managed instance has no launch time")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AwsRunnerError("managed instance has an invalid launch time") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AwsRunnerError("managed instance has an invalid launch time")
    return parsed.astimezone(timezone.utc)


def _candidates(aws: AwsCli, repository: str) -> list[dict[str, Any]]:
    states = sorted(ACTIVE_STATES | {"shutting-down"})
    return instances_from_response(
        aws.json(
            "ec2",
            "describe-instances",
            "--filters",
            f"Name=tag:ManagedBy,Values={MANAGED_BY}",
            "Name=tag:Purpose,Values=api-key-e2e",
            f"Name=tag:Repository,Values={repository}",
            "Name=instance-state-name,Values=" + ",".join(states),
        )
    )


def reap(
    aws: AwsCli,
    *,
    repository: str,
    now: datetime | None = None,
    wait: bool = True,
) -> list[str]:
    repository = validate_repository(repository)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise AwsRunnerError("current time must be timezone-aware")
    current = current.astimezone(timezone.utc)
    expired: list[tuple[str, str, int]] = []
    invalid: list[str] = []
    for candidate in _candidates(aws, repository):
        try:
            instance_id = checked_instance_id(candidate)
            tags = tags_by_key(candidate)
            if (
                tags.get("ManagedBy") != MANAGED_BY
                or tags.get("Purpose") != "api-key-e2e"
                or tags.get("Repository") != repository
                or candidate.get("InstanceType") != INSTANCE_TYPE
            ):
                raise AwsRunnerError("managed identity is incomplete")
            run_id = validate_run_id(tags.get("RunId", ""))
            run_attempt = validate_run_attempt(tags.get("RunAttempt", ""))
            validate_commit_sha(tags.get("TargetSHA", ""))
            validate_commit_sha(tags.get("ControllerSHA", ""))
            expiry = parse_utc_timestamp(tags.get("ExpiresAt", ""))
            launched = _launch_time(candidate)
            if launched > current + LAUNCH_TIME_SKEW:
                raise AwsRunnerError("managed instance launch time is in the future")
            hard_expiry = (
                launched + timedelta(seconds=MAX_TTL_SECONDS) + LAUNCH_TIME_SKEW
            )
            invalid_expiry = (
                expiry < launched - LAUNCH_TIME_SKEW or expiry > hard_expiry
            )
            if expiry <= current or hard_expiry <= current:
                expired.append((instance_id, run_id, run_attempt))
            if invalid_expiry:
                invalid.append(instance_id)
        except AwsRunnerError:
            try:
                invalid.append(checked_instance_id(candidate))
            except AwsRunnerError:
                invalid.append("unknown")

    terminated: list[str] = []
    for instance_id, run_id, run_attempt in expired:
        try:
            terminated.extend(
                terminate(
                    aws,
                    repository=repository,
                    run_id=run_id,
                    run_attempt=run_attempt,
                    instance_id=instance_id,
                    wait=wait,
                )
            )
        except AwsRunnerError:
            invalid.append(instance_id)
    if invalid:
        unique = ",".join(sorted(set(invalid)))
        raise AwsRunnerError(f"reaper refused unsafe managed instances: {unique}")
    return sorted(set(terminated))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", required=True)
    parser.add_argument("--profile")
    parser.add_argument(
        "--repository", "--github-repository", dest="repository", required=True
    )
    parser.add_argument("--no-wait", action="store_true")
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
        ids = reap(
            aws,
            repository=args.repository,
            wait=not args.no_wait,
        )
        print(json.dumps({"instance_ids": ids}, separators=(",", ":"), sort_keys=True))
    except AwsRunnerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
