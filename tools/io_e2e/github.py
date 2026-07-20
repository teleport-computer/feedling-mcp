"""Narrow GitHub CLI adapter used by the IO E2E client.

Authentication is delegated to ``gh``.  This module never reads an OAuth token,
GitHub token, provider key, or Codex credential from arguments, files, or the
environment.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import quote

from .contracts import (
    CANONICAL_REPOSITORY,
    CONTROLLER_BRANCH,
    IoE2EError,
    RunPlan,
    WORKFLOW_FILE,
    WORKFLOW_PATH,
    validate_commit_sha,
    validate_canonical_repository,
    validate_repository,
    validate_request_id,
    validate_run_identifier,
)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[Sequence[str]], CommandResult]
_HTTP_STATUS = re.compile(r"\bHTTP\s+([1-5][0-9]{2})\b")
_RUN_TITLE = re.compile(
    r"^IO E2E · "
    r"([0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})"
    r" · (?:deployed_test|branch_preview)"
    r" · (?:deployed_current|hosted_resident)"
    r" · persona x(?:1|3)$"
)


def _default_runner(command: Sequence[str]) -> CommandResult:
    try:
        completed = subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except FileNotFoundError as exc:
        raise IoE2EError(
            "GH_NOT_INSTALLED",
            "GitHub CLI (gh) is required",
            exit_code=127,
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise IoE2EError("GH_TIMEOUT", "GitHub CLI command timed out") from exc
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def _effective_review_rules_are_strict(value: Any) -> bool:
    """Validate the effective rules needed to make a branch a trust anchor."""

    if not isinstance(value, list) or not value:
        return False
    rules = [item for item in value if isinstance(item, dict)]
    types = {item.get("type") for item in rules}
    if not {"deletion", "non_fast_forward", "pull_request"}.issubset(types):
        return False
    pull_requests = [item for item in rules if item.get("type") == "pull_request"]
    for rule in pull_requests:
        parameters = rule.get("parameters")
        if not isinstance(parameters, dict):
            continue
        approvals = parameters.get("required_approving_review_count")
        if (
            type(approvals) is int
            and approvals >= 1
            and parameters.get("dismiss_stale_reviews_on_push") is True
            and parameters.get("require_last_push_approval") is True
            and parameters.get("required_review_thread_resolution") is True
        ):
            return True
    return False


def _environment_is_exactly_scoped(
    metadata: Any,
    policies: Any,
    *,
    environment: str,
    branch: str,
) -> bool:
    if not isinstance(metadata, dict) or metadata.get("name") != environment:
        return False
    policy = metadata.get("deployment_branch_policy")
    protection_rules = metadata.get("protection_rules")
    if (
        not isinstance(policy, dict)
        or policy.get("protected_branches") is not False
        or policy.get("custom_branch_policies") is not True
        or not isinstance(protection_rules, list)
        or len(protection_rules) != 1
        or not isinstance(protection_rules[0], dict)
        or protection_rules[0].get("type") != "branch_policy"
    ):
        return False
    if not isinstance(policies, dict) or policies.get("total_count") != 1:
        return False
    rows = policies.get("branch_policies")
    return bool(
        isinstance(rows, list)
        and len(rows) == 1
        and isinstance(rows[0], dict)
        and rows[0].get("type") == "branch"
        and rows[0].get("name") == branch
    )


class GitHubClient:
    """A testable, token-blind wrapper around the authenticated ``gh`` CLI."""

    def __init__(self, runner: Runner | None = None) -> None:
        if runner is None and shutil.which("gh") is None:
            raise IoE2EError(
                "GH_NOT_INSTALLED", "GitHub CLI (gh) is required", exit_code=127
            )
        self._runner = runner or _default_runner

    def _run(self, command: Sequence[str], *, check: bool = True) -> CommandResult:
        result = self._runner(tuple(command))
        if check and result.returncode != 0:
            message = result.stderr.strip().splitlines()
            safe_tail = message[-1][:500] if message else "GitHub CLI command failed"
            status_match = _HTTP_STATUS.search(safe_tail)
            details: dict[str, Any] = {"exit_code": result.returncode}
            if status_match is not None:
                details["http_status"] = int(status_match.group(1))
            raise IoE2EError(
                "GH_COMMAND_FAILED",
                safe_tail,
                details=details,
            )
        return result

    def _json(self, endpoint: str) -> Any:
        result = self._run(("gh", "api", endpoint))
        try:
            return json.loads(result.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise IoE2EError(
                "GH_INVALID_RESPONSE", "GitHub returned invalid JSON"
            ) from exc

    def infer_repository(self) -> str:
        result = self._run(
            ("gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner")
        )
        return validate_repository(result.stdout.strip())

    def repository_metadata(self, repository: str) -> dict[str, Any]:
        repository = validate_canonical_repository(repository)
        payload = self._json(f"repos/{repository}")
        if not isinstance(payload, dict):
            raise IoE2EError(
                "GH_INVALID_RESPONSE", "GitHub repository response is invalid"
            )
        return payload

    def require_write_permission(self, repository: str) -> dict[str, Any]:
        metadata = self.repository_metadata(repository)
        permissions = metadata.get("permissions")
        if not isinstance(permissions, dict) or permissions.get("push") is not True:
            raise IoE2EError(
                "WRITE_PERMISSION_REQUIRED",
                f"write access to {repository} is required to control qualification runs",
                exit_code=3,
            )
        default_branch = metadata.get("default_branch")
        if default_branch != CONTROLLER_BRANCH:
            raise IoE2EError(
                "UNTRUSTED_REPOSITORY",
                f"canonical repository default branch must be {CONTROLLER_BRANCH}",
            )
        return metadata

    def require_protected_trust_branches(self, repository: str) -> None:
        """Refuse a secret-bearing run when either trust anchor is unprotected."""

        repository = validate_canonical_repository(repository)
        for branch in (CONTROLLER_BRANCH, "test"):
            encoded_branch = quote(branch, safe="")
            payload = self._json(f"repos/{repository}/branches/{encoded_branch}")
            if (
                not isinstance(payload, dict)
                or payload.get("name") != branch
                or payload.get("protected") is not True
            ):
                raise IoE2EError(
                    "UNPROTECTED_TRUST_BRANCH",
                    f"IO E2E requires protected {branch}",
                    details={"branch": branch},
                    exit_code=3,
                )
            rules = self._json(f"repos/{repository}/rules/branches/{encoded_branch}")
            if not _effective_review_rules_are_strict(rules):
                raise IoE2EError(
                    "INSUFFICIENT_TRUST_RULES",
                    f"IO E2E requires reviewed, non-deletable, non-force-push {branch}",
                    details={"branch": branch},
                    exit_code=3,
                )

    def require_scoped_qa_environments(self, repository: str) -> None:
        """Require exact branch scoping and no per-run human approval."""

        repository = validate_canonical_repository(repository)
        expected = (
            ("io-e2e-agent-driven-test", CONTROLLER_BRANCH),
            ("io-test-deploy", "test"),
        )
        for environment, branch in expected:
            encoded_environment = quote(environment, safe="")
            try:
                metadata = self._json(
                    f"repos/{repository}/environments/{encoded_environment}"
                )
                policies = self._json(
                    f"repos/{repository}/environments/{encoded_environment}/"
                    "deployment-branch-policies"
                )
            except IoE2EError as exc:
                if (
                    exc.code == "GH_COMMAND_FAILED"
                    and exc.details.get("http_status") == 404
                ):
                    raise IoE2EError(
                        "UNSCOPED_QA_ENVIRONMENT",
                        f"{environment} must allow only {branch} with no reviewer",
                        details={"environment": environment, "branch": branch},
                        exit_code=3,
                    ) from None
                raise
            if not _environment_is_exactly_scoped(
                metadata,
                policies,
                environment=environment,
                branch=branch,
            ):
                raise IoE2EError(
                    "UNSCOPED_QA_ENVIRONMENT",
                    f"{environment} must allow only {branch} with no reviewer",
                    details={"environment": environment, "branch": branch},
                    exit_code=3,
                )
    def require_trusted_workflow(self, repository: str, default_branch: str) -> None:
        repository = validate_canonical_repository(repository)
        if default_branch != CONTROLLER_BRANCH:
            raise IoE2EError(
                "UNTRUSTED_CONTROLLER",
                f"controller branch must be {CONTROLLER_BRANCH}",
            )
        try:
            payload = self._json(
                f"repos/{repository}/actions/workflows/{WORKFLOW_FILE}"
            )
        except IoE2EError as exc:
            if (
                exc.code == "GH_COMMAND_FAILED"
                and exc.details.get("http_status") == 404
            ):
                raise IoE2EError(
                    "TRUSTED_WORKFLOW_UNAVAILABLE",
                    f"active {WORKFLOW_PATH} is required on {default_branch}",
                ) from None
            raise
        if not isinstance(payload, dict):
            raise IoE2EError("GH_INVALID_RESPONSE", "workflow metadata is invalid")
        if payload.get("path") != WORKFLOW_PATH or payload.get("state") != "active":
            raise IoE2EError(
                "TRUSTED_WORKFLOW_UNAVAILABLE",
                f"active {WORKFLOW_PATH} is required on {default_branch}",
            )

        encoded_ref = quote(default_branch, safe="")
        encoded_path = quote(WORKFLOW_PATH, safe="/")
        try:
            content = self._json(
                f"repos/{repository}/contents/{encoded_path}?ref={encoded_ref}"
            )
        except IoE2EError as exc:
            if (
                exc.code == "GH_COMMAND_FAILED"
                and exc.details.get("http_status") == 404
            ):
                raise IoE2EError(
                    "TRUSTED_WORKFLOW_UNAVAILABLE",
                    f"{WORKFLOW_PATH} is not available on {default_branch}",
                ) from None
            raise
        if not isinstance(content, dict) or content.get("type") != "file":
            raise IoE2EError(
                "TRUSTED_WORKFLOW_UNAVAILABLE",
                f"{WORKFLOW_PATH} is not a file on {default_branch}",
            )

    def resolve_commit(self, repository: str, target_ref: str) -> str:
        repository = validate_canonical_repository(repository)
        encoded_ref = quote(target_ref, safe="")
        payload = self._json(f"repos/{repository}/commits/{encoded_ref}")
        if not isinstance(payload, dict) or not isinstance(payload.get("sha"), str):
            raise IoE2EError(
                "TARGET_REF_NOT_FOUND", "target ref did not resolve to a commit"
            )
        return validate_commit_sha(payload["sha"])

    def dispatch(self, plan: RunPlan) -> None:
        command = [
            "gh",
            "workflow",
            "run",
            WORKFLOW_FILE,
            "--repo",
            plan.repository,
            "--ref",
            plan.controller_ref,
        ]
        for key, value in plan.workflow_inputs().items():
            command.extend(("-f", f"{key}={value}"))
        self._run(command)

    def _workflow_runs(self, repository: str) -> list[dict[str, Any]]:
        repository = validate_canonical_repository(repository)
        payload = self._json(
            f"repos/{repository}/actions/workflows/{WORKFLOW_FILE}/runs"
            "?event=workflow_dispatch&per_page=100"
        )
        if not isinstance(payload, dict) or not isinstance(
            payload.get("workflow_runs"), list
        ):
            raise IoE2EError("GH_INVALID_RESPONSE", "workflow run list is invalid")
        return [item for item in payload["workflow_runs"] if isinstance(item, dict)]

    def find_request(self, repository: str, request_id: str) -> dict[str, Any] | None:
        repository = validate_canonical_repository(repository)
        request_id = validate_request_id(request_id)
        matches = [
            run
            for run in self._workflow_runs(repository)
            if _is_trusted_run(run, repository)
            and _request_id_from_run(run) == request_id
        ]
        if not matches:
            return None
        if len(matches) != 1:
            raise IoE2EError(
                "RUN_REQUEST_AMBIGUOUS",
                "multiple trusted controller runs claim the same request UUID",
                details={"request_id": request_id, "found": len(matches)},
                exit_code=4,
            )
        matches.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
        return matches[0]

    def resolve_run(self, repository: str, identifier: int | str) -> dict[str, Any]:
        repository = validate_canonical_repository(repository)
        identifier = validate_run_identifier(str(identifier))
        if isinstance(identifier, int):
            payload = self._json(f"repos/{repository}/actions/runs/{identifier}")
            if not isinstance(payload, dict):
                raise IoE2EError(
                    "GH_INVALID_RESPONSE", "workflow run response is invalid"
                )
            run = payload
        else:
            found = self.find_request(repository, identifier)
            if found is None:
                raise IoE2EError(
                    "RUN_NOT_FOUND",
                    f"no {WORKFLOW_FILE} run was found for request {identifier}",
                    exit_code=4,
                )
            run = found

        _require_trusted_run(run, repository)
        return run

    def await_dispatch(
        self,
        repository: str,
        request_id: str,
        *,
        timeout_seconds: float = 30.0,
        interval_seconds: float = 1.0,
    ) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout_seconds
        while True:
            found = self.find_request(repository, request_id)
            if found is not None:
                return found
            if time.monotonic() >= deadline:
                return None
            time.sleep(interval_seconds)

    def watch(
        self, repository: str, run_id: int, interval_seconds: int
    ) -> CommandResult:
        # Do not put a five-minute subprocess timeout around a qualification that
        # legitimately runs for hours.  Each individual API request retains the
        # narrow command timeout; the wait itself is a body-free status poll.
        while True:
            run = self.resolve_run(repository, run_id)
            status = run.get("status")
            if status == "completed":
                return CommandResult(
                    0 if run.get("conclusion") == "success" else 1, "", ""
                )
            time.sleep(interval_seconds)

    def download(
        self,
        repository: str,
        run_id: int,
        run_attempt: int,
        request_id: str,
        destination: Path,
    ) -> None:
        repository = validate_canonical_repository(repository)
        if type(run_attempt) is not int or run_attempt <= 0:
            raise IoE2EError("GH_INVALID_RESPONSE", "workflow run attempt is invalid")
        request_id = validate_request_id(request_id)
        # The ordinary results path deliberately excludes the encrypted debug
        # bundle. Exact identifiers require a separate, explicit operator flow.
        artifact_names = (
            f"io-e2e-request-{request_id}-{run_id}-{run_attempt}",
            f"io-e2e-team-report-api-key-e2e-{run_id}-{run_attempt}",
        )
        for artifact_name in artifact_names:
            self._run(
                (
                    "gh",
                    "run",
                    "download",
                    str(run_id),
                    "--repo",
                    repository,
                    "--name",
                    artifact_name,
                    "--dir",
                    str(destination),
                )
            )

    def open(self, repository: str, run_id: int) -> None:
        repository = validate_canonical_repository(repository)
        self._run(("gh", "run", "view", str(run_id), "--repo", repository, "--web"))

    def cancel(self, repository: str, run_id: int) -> None:
        repository = validate_canonical_repository(repository)
        self._run(("gh", "run", "cancel", str(run_id), "--repo", repository))


def run_projection(run: dict[str, Any]) -> dict[str, Any]:
    """Return the stable, body-free subset exposed to agents and people."""

    _require_trusted_run(run, CANONICAL_REPOSITORY)
    run_id = run.get("id")
    if not isinstance(run_id, int) or run_id <= 0:
        raise IoE2EError("GH_INVALID_RESPONSE", "workflow run ID is invalid")
    request_id = _request_id_from_run(run)
    if request_id is None:
        raise IoE2EError("UNTRUSTED_RUN", "workflow run title is invalid", exit_code=4)
    return {
        "run_id": run_id,
        "request_id": request_id,
        "request_title": run.get("display_title"),
        "status": run.get("status"),
        "conclusion": run.get("conclusion"),
        "url": run.get("html_url"),
        "controller_sha": run.get("head_sha"),
        "controller_branch": run.get("head_branch"),
        "event": run.get("event"),
        "workflow_path": run.get("path"),
        "repository": CANONICAL_REPOSITORY,
        "created_at": run.get("created_at"),
        "updated_at": run.get("updated_at"),
        "run_attempt": run.get("run_attempt"),
    }


def _repository_name(run: dict[str, Any], field: str) -> str | None:
    if field not in run:
        return None
    value = run[field]
    if not isinstance(value, dict) or not isinstance(value.get("full_name"), str):
        return ""
    return value["full_name"]


def _request_id_from_run(run: Mapping[str, Any]) -> str | None:
    title = run.get("display_title")
    if not isinstance(title, str):
        return None
    match = _RUN_TITLE.fullmatch(title)
    if match is None:
        return None
    try:
        return validate_request_id(match.group(1))
    except IoE2EError:
        return None


def _is_trusted_run(run: dict[str, Any], repository: str) -> bool:
    """Authenticate run provenance before looking at caller-controlled titles."""

    try:
        repository = validate_canonical_repository(repository)
    except IoE2EError:
        return False
    controller_sha = run.get("head_sha")
    if not isinstance(controller_sha, str):
        return False
    try:
        normalized_sha = validate_commit_sha(controller_sha)
    except IoE2EError:
        return False
    if (
        controller_sha != normalized_sha
        or run.get("path") != WORKFLOW_PATH
        or run.get("event") != "workflow_dispatch"
        or run.get("head_branch") != CONTROLLER_BRANCH
        or _request_id_from_run(run) is None
    ):
        return False
    for field in ("repository", "head_repository"):
        observed = _repository_name(run, field)
        if observed != repository:
            return False
    return True


def _require_trusted_run(run: dict[str, Any], repository: str) -> None:
    if not _is_trusted_run(run, repository):
        raise IoE2EError(
            "UNTRUSTED_RUN",
            "run is not a protected main workflow_dispatch in the canonical repository",
            exit_code=4,
        )
