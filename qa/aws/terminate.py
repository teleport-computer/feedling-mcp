#!/usr/bin/env python3
"""Idempotently terminate runners matching one exact GitHub run attempt."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from qa.aws.common import (  # noqa: E402
    INSTANCE_TYPE,
    MANAGED_BY,
    AwsCli,
    AwsRunnerError,
    checked_instance_id,
    instance_state,
    instances_from_response,
    parse_utc_timestamp,
    tags_by_key,
    validate_commit_sha,
    validate_instance_id,
    validate_region,
    validate_repository,
    validate_run_attempt,
    validate_run_id,
)


ACTIVE_STATES = frozenset({"pending", "running", "stopping", "stopped"})
TERMINAL_STATES = frozenset({"shutting-down", "terminated"})
KNOWN_STATES = ACTIVE_STATES | TERMINAL_STATES


def _identity_filters(repository: str, run_id: str, run_attempt: int) -> list[str]:
    return [
        f"Name=tag:ManagedBy,Values={MANAGED_BY}",
        "Name=tag:Purpose,Values=api-key-e2e",
        f"Name=tag:Repository,Values={repository}",
        f"Name=tag:RunId,Values={run_id}",
        f"Name=tag:RunAttempt,Values={run_attempt}",
        "Name=instance-state-name,Values=" + ",".join(sorted(KNOWN_STATES)),
    ]


def _describe(
    aws: AwsCli,
    *,
    repository: str,
    run_id: str,
    run_attempt: int,
    instance_id: str | None,
) -> list[dict[str, Any]]:
    if instance_id is None:
        arguments = _identity_filters(repository, run_id, run_attempt)
    else:
        arguments = [f"Name=instance-id,Values={validate_instance_id(instance_id)}"]
    return instances_from_response(
        aws.json("ec2", "describe-instances", "--filters", *arguments)
    )


def _validate_identity(
    instance: Mapping[str, Any],
    *,
    repository: str,
    run_id: str,
    run_attempt: int,
) -> tuple[str, str]:
    instance_id = checked_instance_id(instance)
    state = instance_state(instance)
    if state not in KNOWN_STATES:
        raise AwsRunnerError("managed instance has an unknown state")
    tags = tags_by_key(instance)
    expected = {
        "ManagedBy": MANAGED_BY,
        "Purpose": "api-key-e2e",
        "Repository": repository,
        "RunId": run_id,
        "RunAttempt": str(run_attempt),
    }
    if any(tags.get(key) != value for key, value in expected.items()):
        raise AwsRunnerError("refusing to terminate an instance with mismatched tags")
    if instance.get("InstanceType") != INSTANCE_TYPE:
        raise AwsRunnerError("refusing to terminate an instance of the wrong type")
    validate_commit_sha(tags.get("TargetSHA", ""))
    validate_commit_sha(tags.get("ControllerSHA", ""))
    parse_utc_timestamp(tags.get("ExpiresAt", ""))
    return instance_id, state


def terminate(
    aws: AwsCli,
    *,
    repository: str,
    run_id: str,
    run_attempt: int,
    instance_id: str | None = None,
    wait: bool = True,
) -> list[str]:
    repository = validate_repository(repository)
    run_id = validate_run_id(run_id)
    run_attempt = validate_run_attempt(run_attempt)
    if instance_id is not None:
        instance_id = validate_instance_id(instance_id)
    candidates = _describe(
        aws,
        repository=repository,
        run_id=run_id,
        run_attempt=run_attempt,
        instance_id=instance_id,
    )
    if instance_id is not None and len(candidates) > 1:
        raise AwsRunnerError("AWS returned duplicate instances for one id")

    active_ids: list[str] = []
    wait_ids: list[str] = []
    for candidate in candidates:
        candidate_id, state = _validate_identity(
            candidate,
            repository=repository,
            run_id=run_id,
            run_attempt=run_attempt,
        )
        if instance_id is not None and candidate_id != instance_id:
            raise AwsRunnerError("AWS returned the wrong instance id")
        if state in ACTIVE_STATES:
            active_ids.append(candidate_id)
            wait_ids.append(candidate_id)
        elif state == "shutting-down":
            wait_ids.append(candidate_id)

    active_ids = sorted(set(active_ids))
    wait_ids = sorted(set(wait_ids))
    if active_ids:
        # Re-read exact IDs and tags immediately before the mutating call. IAM
        # should independently require ManagedBy/Repository tags as defense in
        # depth against a tag-change race outside this controller.
        for candidate_id in active_ids:
            fresh = _describe(
                aws,
                repository=repository,
                run_id=run_id,
                run_attempt=run_attempt,
                instance_id=candidate_id,
            )
            if len(fresh) != 1:
                raise AwsRunnerError("instance changed during guarded cleanup")
            fresh_id, _ = _validate_identity(
                fresh[0],
                repository=repository,
                run_id=run_id,
                run_attempt=run_attempt,
            )
            if fresh_id != candidate_id:
                raise AwsRunnerError("AWS returned the wrong instance during cleanup")
        aws.run("ec2", "terminate-instances", "--instance-ids", *active_ids)
    if wait and wait_ids:
        aws.run("ec2", "wait", "instance-terminated", "--instance-ids", *wait_ids)
    return sorted(set(active_ids + wait_ids))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", required=True)
    parser.add_argument("--profile")
    parser.add_argument(
        "--repository", "--github-repository", dest="repository", required=True
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True, type=int)
    parser.add_argument("--instance-id")
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
        ids = terminate(
            aws,
            repository=args.repository,
            run_id=args.run_id,
            run_attempt=args.run_attempt,
            instance_id=args.instance_id,
            wait=not args.no_wait,
        )
        print(json.dumps({"instance_ids": ids}, separators=(",", ":"), sort_keys=True))
    except AwsRunnerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
