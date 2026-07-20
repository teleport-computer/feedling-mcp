"""Command-line entry point for trusted, self-service IO E2E runs."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any, Sequence

from .artifacts import project_downloaded_results
from .contracts import (
    IoE2EError,
    RunPlan,
    WORKFLOW_PATH,
    validate_commit_sha,
    validate_canonical_repository,
    validate_lane,
    validate_lane_target,
    validate_persona_repetitions,
    validate_ref,
    validate_repository,
    validate_run_identifier,
    validate_runtime_target,
    validate_suite,
)
from .github import GitHubClient, run_projection


class _ArgumentParser(argparse.ArgumentParser):
    """Turn usage mistakes into the same bounded error contract as runtime failures."""

    def error(self, message: str) -> None:
        raise IoE2EError("INVALID_ARGUMENTS", message, exit_code=2)


def _add_repository(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--repo",
        metavar="OWNER/REPO",
        help="GitHub repository (defaults to the current gh repository)",
    )


def _add_json(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="emit one JSON document")


def _add_plan_arguments(parser: argparse.ArgumentParser) -> None:
    _add_repository(parser)
    parser.add_argument(
        "--ref",
        required=True,
        help="target branch, tag, or commit (deployed_test currently requires test)",
    )
    parser.add_argument(
        "--sha", help="optional full commit SHA assertion for the resolved target ref"
    )
    parser.add_argument("--lane", default="deployed_test")
    parser.add_argument("--suite", default="full")
    parser.add_argument("--persona-repetitions", type=int, default=1)
    parser.add_argument("--runtime-target", default="hosted_resident")
    _add_json(parser)


def build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="io-e2e",
        description="Run trusted IO agentic E2E qualification without handling secrets.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser(
        "plan", help="resolve and validate a qualification target"
    )
    _add_plan_arguments(plan)

    run = subparsers.add_parser("run", help="dispatch a trusted qualification run")
    _add_plan_arguments(run)
    run.add_argument("--wait", action="store_true", help="wait for the dispatched run")
    run.add_argument(
        "--interval", type=int, default=10, help="wait polling interval in seconds"
    )

    for name, help_text in (
        ("status", "show a run's current state"),
        ("open", "open a run in GitHub"),
        ("cancel", "request cancellation of a run"),
    ):
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument("run", help="GitHub run ID or IO E2E request UUID")
        _add_repository(command)
        _add_json(command)

    watch = subparsers.add_parser("watch", help="wait for a run to finish")
    watch.add_argument("run", help="GitHub run ID or IO E2E request UUID")
    _add_repository(watch)
    watch.add_argument(
        "--interval", type=int, default=10, help="poll interval in seconds"
    )
    _add_json(watch)

    results = subparsers.add_parser("results", help="download a run's artifacts")
    results.add_argument("run", help="GitHub run ID or IO E2E request UUID")
    _add_repository(results)
    results.add_argument("--dir", type=Path, help="new destination directory")
    _add_json(results)

    return parser


def _repository(client: GitHubClient, requested: str | None) -> str:
    repository = (
        validate_repository(requested) if requested else client.infer_repository()
    )
    return validate_canonical_repository(repository)


def _require_access(
    client: GitHubClient, requested: str | None
) -> tuple[str, dict[str, Any]]:
    repository = _repository(client, requested)
    metadata = client.require_write_permission(repository)
    return repository, metadata


def create_plan(args: argparse.Namespace, client: GitHubClient) -> RunPlan:
    # Reject unimplemented or unsafe modes before making a dispatch possible.
    lane = validate_lane(args.lane)
    suite = validate_suite(args.suite)
    target_ref = validate_ref(args.ref)
    validate_lane_target(lane, target_ref)
    repetitions = validate_persona_repetitions(args.persona_repetitions)
    runtime_target = validate_runtime_target(args.runtime_target)

    repository, metadata = _require_access(client, args.repo)
    default_branch = validate_ref(metadata["default_branch"])
    client.require_protected_trust_branches(repository)
    client.require_scoped_qa_environments(repository)
    client.require_trusted_workflow(repository, default_branch)
    resolved_sha = client.resolve_commit(repository, target_ref)
    if args.sha is not None:
        asserted_sha = validate_commit_sha(args.sha)
        if asserted_sha != resolved_sha:
            raise IoE2EError(
                "TARGET_SHA_MISMATCH",
                "target ref no longer resolves to the asserted SHA",
                details={"asserted_sha": asserted_sha, "resolved_sha": resolved_sha},
            )

    return RunPlan(
        request_id=str(uuid.uuid4()),
        repository=repository,
        controller_ref=default_branch,
        controller_workflow=WORKFLOW_PATH,
        target_ref=target_ref,
        target_sha=resolved_sha,
        lane=lane,
        suite=suite,
        persona_repetitions=repetitions,
        runtime_target=runtime_target,
    )


def _status(
    args: argparse.Namespace, client: GitHubClient
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    repository, _ = _require_access(client, args.repo)
    identifier = validate_run_identifier(args.run)
    run = client.resolve_run(repository, identifier)
    return repository, run, run_projection(run)


def _emit(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return

    kind = payload.get("kind")
    if kind == "plan":
        print(
            f"Requested branch head: {payload['repository']}@"
            f"{payload['target_ref']} ({payload['target_sha']})"
        )
        print("Live backend image: resolved and bound separately by the controller")
        print(
            f"Controller: {payload['controller_ref']}:{payload['controller_workflow']}"
        )
        print(
            "Qualification: "
            f"{payload['lane']} / {payload['suite']} / "
            f"{payload['runtime_target']} / persona x{payload['persona_repetitions']}"
        )
        print(f"Request: {payload['request_id']}")
    elif kind == "dispatch":
        print(f"Dispatched IO E2E request {payload['request_id']}")
        if payload.get("run"):
            run = payload["run"]
            print(
                f"Run: {run['run_id']} · {run['status']} / "
                f"{run['conclusion'] or 'pending'} · {run['url']}"
            )
        else:
            print("GitHub accepted the dispatch; the run is not indexed yet.")
            print(f"Next: python3 -m tools.io_e2e status {payload['request_id']}")
    elif kind in {"status", "watch"}:
        run = payload["run"]
        print(
            f"Run {run['run_id']}: {run['status']} / {run['conclusion'] or 'pending'}"
        )
        print(run["url"])
    elif kind == "results":
        request = payload["result"]["request"]
        failures = payload["result"]["failure_counts"]
        print(
            f"Downloaded and verified run {payload['run']['run_id']} at {payload['directory']}"
        )
        print(
            f"Request {request['request_id']} · test@{request['target_sha']} · "
            f"deployed {request['deployed_sha']}"
        )
        print(f"Failures/evidence gaps: {failures['failure_count']}")
        print("\n--- team-summary.md ---\n")
        print(payload["result"]["team_summary_markdown"], end="")
        print("\n--- matrix.md ---\n")
        print(payload["result"]["matrix_markdown"], end="")
    elif kind == "open":
        print(f"Opened {payload['run']['url']}")
    elif kind == "cancel":
        print(f"Cancellation requested for run {payload['run']['run_id']}")
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def execute(
    args: argparse.Namespace,
    *,
    client: GitHubClient,
) -> tuple[dict[str, Any], int]:
    if args.command == "plan":
        plan = create_plan(args, client)
        return plan.as_dict(), 0

    if args.command == "run":
        if args.wait and (args.interval < 3 or args.interval > 300):
            raise IoE2EError(
                "INVALID_INTERVAL", "wait interval must be between 3 and 300 seconds"
            )
        plan = create_plan(args, client)
        client.dispatch(plan)
        discovered = client.await_dispatch(plan.repository, plan.request_id)
        exit_code = 0
        if args.wait and discovered is None:
            raise IoE2EError(
                "RUN_INDEX_TIMEOUT",
                "GitHub accepted the dispatch but did not index it in time; do not dispatch again",
                details={
                    "dispatch_accepted": True,
                    "request_id": plan.request_id,
                    "next_command": (
                        "python3 -m tools.io_e2e status "
                        f"{plan.request_id} --repo {plan.repository}"
                    ),
                },
                exit_code=6,
            )
        if args.wait and discovered is not None:
            run_id = run_projection(discovered)["run_id"]
            watched = client.watch(plan.repository, run_id, args.interval)
            discovered = client.resolve_run(plan.repository, run_id)
            exit_code = 0 if watched.returncode == 0 else 5
        return (
            {
                "schema_version": "io-e2e-control.v1",
                "kind": "dispatch",
                "request_id": plan.request_id,
                "repository": plan.repository,
                "target_ref": plan.target_ref,
                "target_sha": plan.target_sha,
                "wait_requested": args.wait,
                "run": run_projection(discovered) if discovered is not None else None,
            },
            exit_code,
        )

    repository, run, projection = _status(args, client)
    run_id = projection["run_id"]

    if args.command == "status":
        return {
            "schema_version": "io-e2e-control.v1",
            "kind": "status",
            "run": projection,
        }, 0

    if args.command == "watch":
        if args.interval < 3 or args.interval > 300:
            raise IoE2EError(
                "INVALID_INTERVAL", "watch interval must be between 3 and 300 seconds"
            )
        watched = client.watch(repository, run_id, args.interval)
        final_run = client.resolve_run(repository, run_id)
        final_projection = run_projection(final_run)
        exit_code = 0 if watched.returncode == 0 else 5
        return (
            {
                "schema_version": "io-e2e-control.v1",
                "kind": "watch",
                "run": final_projection,
            },
            exit_code,
        )

    if args.command == "results":
        if projection["status"] != "completed":
            raise IoE2EError(
                "RUN_NOT_COMPLETE", "artifacts are available only after completion"
            )
        destination = args.dir or Path("io-e2e-results") / str(run_id)
        destination = destination.expanduser()
        if destination.exists() or destination.is_symlink():
            raise IoE2EError(
                "RESULTS_DIRECTORY_EXISTS",
                f"results destination already exists: {destination}",
            )
        destination.mkdir(parents=True, mode=0o700)
        try:
            client.download(
                repository,
                run_id,
                projection["run_attempt"],
                projection["request_id"],
                destination,
            )
            result = project_downloaded_results(
                destination,
                repository=repository,
                run=projection,
            )
        except BaseException:
            # This directory did not exist before this command. Remove partial
            # or rejected artifacts so a retry is possible and untrusted files
            # are not left for a person or agent to open accidentally.
            shutil.rmtree(destination, ignore_errors=True)
            raise
        return (
            {
                "schema_version": "io-e2e-control.v1",
                "kind": "results",
                "run": projection,
                "directory": str(destination.resolve()),
                "result": result,
            },
            0,
        )

    if args.command == "open":
        if not args.json:
            client.open(repository, run_id)
        return (
            {
                "schema_version": "io-e2e-control.v1",
                "kind": "open",
                "run": projection,
                "opened": not args.json,
            },
            0,
        )

    if args.command == "cancel":
        if projection["status"] == "completed":
            raise IoE2EError(
                "RUN_ALREADY_COMPLETE", "a completed run cannot be cancelled"
            )
        client.cancel(repository, run_id)
        return (
            {
                "schema_version": "io-e2e-control.v1",
                "kind": "cancel",
                "run": projection,
                "accepted": True,
            },
            0,
        )

    raise IoE2EError("INVALID_COMMAND", f"unsupported command: {args.command}")


def main(
    argv: Sequence[str] | None = None, *, client: GitHubClient | None = None
) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in arguments
    parser = build_parser()
    try:
        args = parser.parse_args(arguments)
        as_json = bool(getattr(args, "json", False))
        payload, exit_code = execute(args, client=client or GitHubClient())
        _emit(payload, as_json=as_json)
        return exit_code
    except IoE2EError as exc:
        payload = {
            "schema_version": "io-e2e-control.v1",
            "ok": False,
            "error": exc.as_dict(),
        }
        if as_json:
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        else:
            print(f"io-e2e: {exc.code}: {exc.message}", file=sys.stderr)
            if isinstance(exc.details.get("request_id"), str):
                print(f"Request: {exc.details['request_id']}", file=sys.stderr)
            if isinstance(exc.details.get("next_command"), str):
                print(f"Next: {exc.details['next_command']}", file=sys.stderr)
        return exc.exit_code
