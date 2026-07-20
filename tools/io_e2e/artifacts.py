"""Safe projection of the sanitized artifacts downloaded for one IO E2E run."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any, Mapping

from .contracts import (
    CANONICAL_REPOSITORY,
    CONTROLLER_BRANCH,
    IoE2EError,
    RUNTIME_TARGETS,
    SCHEMA_VERSION,
    SUPPORTED_LANES,
    SUPPORTED_SUITES,
    WORKFLOW_PATH,
    validate_commit_sha,
    validate_canonical_repository,
    validate_ref,
    validate_repository,
    validate_request_id,
)


MAX_REQUEST_MANIFEST_BYTES = 64 * 1024
MAX_TEAM_MARKDOWN_BYTES = 512 * 1024
MAX_FAILURE_INDEX_BYTES = 4 * 1024 * 1024
MAX_ARTIFACT_ENTRIES = 10_000
MAX_ARTIFACT_DEPTH = 8
_REQUIRED_NAMES = frozenset(
    {"request-manifest.json", "team-summary.md", "matrix.md", "failure-index.json"}
)
_REQUEST_KEYS = frozenset(
    {
        "schema_version",
        "request_id",
        "repository",
        "controller_sha",
        "target_ref",
        "target_sha",
        "deployed_sha",
        "lane",
        "suite",
        "runtime_target",
        "persona_repetitions",
    }
)
_FAILURE_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "run_id",
        "failure_count",
        "api_key_failure_count",
        "persona_memory_failure_count",
        "exact_id_failure_count",
        "failures",
        "redaction",
    }
)
_REDACTION_KEYS = frozenset(
    {
        "synthetic_users_only",
        "credentials_omitted",
        "user_identifiers_omitted",
        "raw_correlation_identifiers_omitted",
        "raw_chat_omitted",
        "raw_persona_omitted",
        "raw_trace_omitted",
        "raw_reasoning_omitted",
    }
)


def _duplicate_rejecting_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _safe_root(path: Path) -> Path:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise IoE2EError(
            "UNSAFE_RESULT_ARTIFACTS", "results directory is missing or unsafe"
        ) from None
    if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise IoE2EError(
            "UNSAFE_RESULT_ARTIFACTS", "results directory is missing or unsafe"
        )
    return resolved


def _locate_required(root: Path) -> dict[str, Path]:
    found: dict[str, list[Path]] = {name: [] for name in _REQUIRED_NAMES}
    stack = [(root, 0)]
    entry_count = 0
    while stack:
        directory, depth = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            raise IoE2EError(
                "UNSAFE_RESULT_ARTIFACTS", "results directory is unreadable"
            ) from None
        for entry in entries:
            entry_count += 1
            if entry_count > MAX_ARTIFACT_ENTRIES:
                raise IoE2EError(
                    "UNSAFE_RESULT_ARTIFACTS", "results contain too many entries"
                )
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError:
                raise IoE2EError(
                    "UNSAFE_RESULT_ARTIFACTS", "results contain an unreadable entry"
                ) from None
            if stat.S_ISLNK(metadata.st_mode):
                raise IoE2EError(
                    "UNSAFE_RESULT_ARTIFACTS", "results contain a symbolic link"
                )
            path = Path(entry.path)
            if stat.S_ISDIR(metadata.st_mode):
                if depth >= MAX_ARTIFACT_DEPTH:
                    raise IoE2EError(
                        "UNSAFE_RESULT_ARTIFACTS",
                        "results directory nesting is too deep",
                    )
                stack.append((path, depth + 1))
            elif stat.S_ISREG(metadata.st_mode) and entry.name in found:
                found[entry.name].append(path)

    manifest_count = len(found["request-manifest.json"])
    if manifest_count != 1:
        raise IoE2EError(
            "REQUEST_MANIFEST_AMBIGUOUS"
            if manifest_count > 1
            else "REQUEST_MANIFEST_MISSING",
            "downloaded results must contain exactly one same-run request manifest",
            details={"found": manifest_count},
        )
    for name in ("team-summary.md", "matrix.md", "failure-index.json"):
        if len(found[name]) != 1:
            raise IoE2EError(
                "TEAM_REPORT_INCOMPLETE",
                "downloaded results must contain one complete team-safe report",
                details={"artifact": name, "found": len(found[name])},
            )

    projected = {name: paths[0] for name, paths in found.items()}
    team_parent = projected["team-summary.md"].parent
    if any(
        projected[name].parent != team_parent
        for name in ("matrix.md", "failure-index.json")
    ):
        raise IoE2EError(
            "TEAM_REPORT_AMBIGUOUS",
            "team-safe report files do not share one artifact directory",
        )
    return projected


def _read_regular(path: Path, *, label: str, max_bytes: int) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        before = path.lstat()
        descriptor = os.open(path, flags)
    except OSError:
        raise IoE2EError(
            "UNSAFE_RESULT_ARTIFACTS", f"{label} is missing or unsafe"
        ) from None
    try:
        current = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or before.st_dev != current.st_dev
            or before.st_ino != current.st_ino
            or current.st_nlink != 1
            or current.st_size <= 0
            or current.st_size > max_bytes
        ):
            raise IoE2EError("UNSAFE_RESULT_ARTIFACTS", f"{label} is missing or unsafe")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        value = b"".join(chunks)
        if len(value) > max_bytes:
            raise IoE2EError(
                "UNSAFE_RESULT_ARTIFACTS", f"{label} exceeds the display limit"
            )
        return value
    finally:
        os.close(descriptor)


def _read_text(path: Path, *, label: str) -> str:
    try:
        value = _read_regular(
            path, label=label, max_bytes=MAX_TEAM_MARKDOWN_BYTES
        ).decode("utf-8")
    except UnicodeError:
        raise IoE2EError("INVALID_TEAM_REPORT", f"{label} is not valid UTF-8") from None
    if any(
        (ord(character) < 32 and character not in "\n\r\t") or ord(character) == 127
        for character in value
    ):
        raise IoE2EError("INVALID_TEAM_REPORT", f"{label} contains invalid text")
    return value


def _read_json(path: Path, *, label: str, max_bytes: int) -> Any:
    try:
        return json.loads(
            _read_regular(path, label=label, max_bytes=max_bytes).decode("utf-8"),
            object_pairs_hook=_duplicate_rejecting_object,
        )
    except IoE2EError:
        raise
    except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise IoE2EError("INVALID_TEAM_REPORT", f"{label} is invalid") from None


def _request_projection(
    value: Any,
    *,
    repository: str,
    run: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _REQUEST_KEYS:
        raise IoE2EError("INVALID_REQUEST_MANIFEST", "request manifest is invalid")
    try:
        request_id = validate_request_id(value["request_id"])
        manifest_repository = validate_repository(value["repository"])
        controller_sha = validate_commit_sha(value["controller_sha"])
        target_ref = validate_ref(value["target_ref"])
        target_sha = validate_commit_sha(value["target_sha"])
        deployed_sha = validate_commit_sha(value["deployed_sha"])
    except (IoE2EError, KeyError, TypeError):
        raise IoE2EError(
            "INVALID_REQUEST_MANIFEST", "request manifest is invalid"
        ) from None
    lane = value["lane"]
    suite = value["suite"]
    runtime_target = value["runtime_target"]
    repetitions = value["persona_repetitions"]
    expected_title = (
        f"IO E2E · {request_id} · {lane} · {runtime_target} "
        f"· persona x{repetitions}"
    )
    if (
        value["schema_version"] != 1
        or manifest_repository != repository
        or controller_sha != run.get("controller_sha")
        or request_id != run.get("request_id")
        or run.get("request_title") != expected_title
        or target_ref != "test"
        or not isinstance(lane, str)
        or lane not in SUPPORTED_LANES
        or not isinstance(suite, str)
        or suite not in SUPPORTED_SUITES
        or not isinstance(runtime_target, str)
        or runtime_target not in RUNTIME_TARGETS
        or type(repetitions) is not int
        or repetitions not in {1, 3}
    ):
        raise IoE2EError(
            "REQUEST_MANIFEST_RUN_MISMATCH",
            "request manifest does not bind to this GitHub run",
        )
    return {
        "schema_version": 1,
        "request_id": request_id,
        "repository": manifest_repository,
        "controller_sha": controller_sha,
        "target_ref": target_ref,
        "target_sha": target_sha,
        "deployed_sha": deployed_sha,
        "lane": lane,
        "suite": suite,
        "runtime_target": runtime_target,
        "persona_repetitions": repetitions,
    }


def _failure_projection(value: Any, *, run: Mapping[str, Any]) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != _FAILURE_KEYS:
        raise IoE2EError("INVALID_TEAM_REPORT", "failure index is invalid")
    failures = value["failures"]
    redaction = value["redaction"]
    count_fields = (
        "failure_count",
        "api_key_failure_count",
        "persona_memory_failure_count",
        "exact_id_failure_count",
    )
    if (
        value["schema_version"] != 1
        or value["kind"] != "io_e2e_team_failure_index"
        or not isinstance(failures, list)
        or not isinstance(redaction, dict)
        or set(redaction) != _REDACTION_KEYS
        or any(item is not True for item in redaction.values())
        or any(
            type(value[field]) is not int or value[field] < 0 for field in count_fields
        )
        or value["failure_count"] != len(failures)
        or value["failure_count"]
        != value["api_key_failure_count"] + value["persona_memory_failure_count"]
        or not value["api_key_failure_count"]
        <= value["exact_id_failure_count"]
        <= value["failure_count"]
    ):
        raise IoE2EError("INVALID_TEAM_REPORT", "failure index is invalid")
    expected_run_id = f"api-key-e2e-{run.get('run_id')}-{run.get('run_attempt')}"
    if value["run_id"] != expected_run_id:
        raise IoE2EError(
            "TEAM_REPORT_RUN_MISMATCH",
            "team-safe report does not bind to this GitHub run attempt",
        )
    return {field: value[field] for field in count_fields}


def project_downloaded_results(
    root: Path,
    *,
    repository: str,
    run: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate and project one downloaded run without following artifact links."""

    repository = validate_canonical_repository(repository)
    controller_sha = run.get("controller_sha")
    try:
        normalized_controller_sha = validate_commit_sha(controller_sha)
    except IoE2EError:
        normalized_controller_sha = None
    try:
        normalized_request_id = validate_request_id(run.get("request_id"))
    except (IoE2EError, TypeError):
        normalized_request_id = None
    if (
        run.get("repository") != CANONICAL_REPOSITORY
        or run.get("workflow_path") != WORKFLOW_PATH
        or run.get("event") != "workflow_dispatch"
        or run.get("controller_branch") != CONTROLLER_BRANCH
        or controller_sha != normalized_controller_sha
        or type(run.get("run_id")) is not int
        or run.get("run_id", 0) <= 0
        or type(run.get("run_attempt")) is not int
        or run.get("run_attempt", 0) <= 0
        or run.get("request_id") != normalized_request_id
        or not isinstance(run.get("request_title"), str)
    ):
        raise IoE2EError(
            "UNTRUSTED_RUN",
            "artifact projection requires a trusted canonical controller run",
            exit_code=4,
        )
    safe_root = _safe_root(root)
    paths = _locate_required(safe_root)
    request = _request_projection(
        _read_json(
            paths["request-manifest.json"],
            label="request manifest",
            max_bytes=MAX_REQUEST_MANIFEST_BYTES,
        ),
        repository=repository,
        run=run,
    )
    failures = _failure_projection(
        _read_json(
            paths["failure-index.json"],
            label="failure index",
            max_bytes=MAX_FAILURE_INDEX_BYTES,
        ),
        run=run,
    )
    team_summary = _read_text(paths["team-summary.md"], label="team summary")
    matrix = _read_text(paths["matrix.md"], label="coverage matrix")
    return {
        "schema_version": SCHEMA_VERSION,
        "request": request,
        "failure_counts": failures,
        "artifacts": {
            "request_manifest": str(paths["request-manifest.json"]),
            "team_summary": str(paths["team-summary.md"]),
            "matrix": str(paths["matrix.md"]),
            "failure_index": str(paths["failure-index.json"]),
        },
        "team_summary_markdown": team_summary,
        "matrix_markdown": matrix,
    }
