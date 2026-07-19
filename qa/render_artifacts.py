#!/usr/bin/env python3
"""Render public qualification views from the canonical structured result.

The coding agent owns only ``run-result.json``.  This module is the trusted,
mechanical boundary that validates that JSON against the checked-in schema and
then derives every other public artifact.  It never copies chat, trace, failure
prose, or diagnostic prose into a report; the schema permits fixed codes only.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence
from xml.etree import ElementTree


PROFILE_IDS = (
    "official-deepseek",
    "official-anthropic",
    "official-openai",
    "official-gemini",
    "openrouter-claude",
    "openrouter-openai",
    "openrouter-glm",
    "openrouter-kimi",
    "relay-kongbeiqie",
)
SCENARIO_IDS = tuple(f"P0-{index:02d}" for index in range(1, 14))
REQUIRED_TRACE_STAGES = (
    "routing",
    "queue",
    "provider",
    "persistence",
    "delivery",
)
SUMMARY_FIELDS = (
    ("pass", "PASS"),
    ("product_fail", "PRODUCT_FAIL"),
    ("blocked_credential", "BLOCKED_CREDENTIAL"),
    ("blocked_evidence", "BLOCKED_EVIDENCE"),
    ("blocked_deployment", "BLOCKED_DEPLOYMENT"),
    ("agent_error", "AGENT_ERROR"),
    ("security_fail", "SECURITY_FAIL"),
)
FAILURE_STATUSES = frozenset(("PRODUCT_FAIL", "SECURITY_FAIL"))
MAX_JSON_BYTES = 20 * 1024 * 1024


class RenderInputError(RuntimeError):
    """A fixed renderer diagnostic safe to print in CI."""


def _reject_nonstandard_number(_value: str) -> None:
    raise ValueError


def _read_json(path: Path, label: str) -> Any:
    if path.is_symlink():
        raise RenderInputError(f"{label} is missing or unreadable")
    try:
        if path.stat().st_size > MAX_JSON_BYTES:
            raise RenderInputError(f"{label} exceeds the size limit")
        raw = path.read_text(encoding="utf-8")
        return json.loads(raw, parse_constant=_reject_nonstandard_number)
    except RenderInputError:
        raise
    except (OSError, UnicodeError, ValueError, RecursionError):
        raise RenderInputError(f"{label} is missing or invalid") from None


def _validate_schema(schema: Any, result: Any) -> None:
    try:
        from jsonschema import Draft202012Validator, FormatChecker
        from jsonschema.exceptions import SchemaError
    except ImportError:
        raise RenderInputError(
            "JSON Schema validator dependency is unavailable"
        ) from None

    if not isinstance(schema, dict):
        raise RenderInputError("result schema is invalid")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError:
        raise RenderInputError("result schema is invalid") from None
    try:
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        if next(validator.iter_errors(result), None) is not None:
            raise RenderInputError("run result does not satisfy the result schema")
    except RenderInputError:
        raise
    except Exception:
        raise RenderInputError(
            "run result schema validation could not be completed"
        ) from None


def _ordered_profiles(result: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    profiles = result.get("profiles")
    if not isinstance(profiles, list):
        raise RenderInputError("run result profile set is incomplete")
    by_id: dict[str, Mapping[str, Any]] = {}
    for profile in profiles:
        if not isinstance(profile, dict):
            raise RenderInputError("run result profile set is malformed")
        profile_id = profile.get("profile_id")
        if profile_id not in PROFILE_IDS or profile_id in by_id:
            raise RenderInputError("run result profile set is not exact")
        scenarios = profile.get("scenarios")
        if not isinstance(scenarios, list):
            raise RenderInputError("run result scenario set is incomplete")
        scenario_ids = [
            row.get("scenario_id") if isinstance(row, dict) else None
            for row in scenarios
        ]
        if len(scenario_ids) != len(SCENARIO_IDS) or set(scenario_ids) != set(
            SCENARIO_IDS
        ):
            raise RenderInputError("run result scenario set is not exact")
        by_id[profile_id] = profile
    if set(by_id) != set(PROFILE_IDS):
        raise RenderInputError("run result profile set is not exact")
    return [by_id[profile_id] for profile_id in PROFILE_IDS]


def _scenario_map(profile: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        str(scenario["scenario_id"]): scenario
        for scenario in profile["scenarios"]
        if isinstance(scenario, dict)
    }


def _render_matrix(
    result: Mapping[str, Any], profiles: Sequence[Mapping[str, Any]]
) -> str:
    target = result["target"]
    lines = [
        "# Feedling API-key deployed-runtime qualification",
        "",
        f"- Run ID: `{result['run_id']}`",
        f"- Overall status: `{result['overall_status']}`",
        f"- Expected deployment: `{target['expected_deployment_sha']}`",
        f"- Expected runtime: `{target['expected_runtime']}`",
        "- Canonical source: `run-result.json`",
        "",
    ]
    header = ["Profile", "Route", "Provider", "Model family", "Profile status"] + list(
        SCENARIO_IDS
    )
    lines.append("| " + " | ".join(header) + " |")
    lines.append("| " + " | ".join("---" for _ in header) + " |")
    for profile in profiles:
        scenarios = _scenario_map(profile)
        row = [
            str(profile["profile_id"]),
            str(profile["route_family"]),
            str(profile["provider"]),
            str(profile["model_family"]),
            str(profile["status"]),
            *(str(scenarios[scenario_id]["status"]) for scenario_id in SCENARIO_IDS),
        ]
        lines.append("| " + " | ".join(row) + " |")

    lines.extend(
        [
            "",
            "## Profile terminal-status counts",
            "",
            "| Status | Profiles |",
            "| --- | ---: |",
        ]
    )
    summary = result["summary"]
    for field, status in SUMMARY_FIELDS:
        lines.append(f"| {status} | {summary[field]} |")
    return "\n".join(lines) + "\n"


def _number(value: int | float) -> str:
    return json.dumps(value, allow_nan=False, separators=(",", ":"))


def _render_latency(profiles: Sequence[Mapping[str, Any]]) -> str:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(
        (
            "record_type",
            "profile_id",
            "scenario_id",
            "turn_index",
            "request_id",
            "turn_id",
            "trace_id",
            "metric",
            "stage",
            "value_ms",
        )
    )
    scenario_order = {
        scenario_id: index for index, scenario_id in enumerate(SCENARIO_IDS)
    }
    for profile in profiles:
        profile_id = str(profile["profile_id"])
        turns = sorted(
            profile["turns"],
            key=lambda row: (
                scenario_order[str(row["scenario_id"])],
                int(row["turn_index"]),
            ),
        )
        for turn in turns:
            for field in ("ack_latency_ms", "reply_latency_ms"):
                value = turn[field]
                if value is not None:
                    writer.writerow(
                        (
                            "turn",
                            profile_id,
                            turn["scenario_id"],
                            turn["turn_index"],
                            turn["request_id"],
                            turn["turn_id"],
                            turn["trace_id"],
                            field,
                            "",
                            _number(value),
                        )
                    )
            for stage, value in sorted(turn["stage_latency_ms"].items()):
                if value is not None:
                    writer.writerow(
                        (
                            "turn_stage",
                            profile_id,
                            turn["scenario_id"],
                            turn["turn_index"],
                            turn["request_id"],
                            turn["turn_id"],
                            turn["trace_id"],
                            "stage_latency_ms",
                            stage,
                            _number(value),
                        )
                    )

        latency = profile["latency"]
        for field in ("ack_p50_ms", "reply_p50_ms", "reply_p95_ms"):
            value = latency[field]
            if value is not None:
                writer.writerow(
                    (
                        "profile_summary",
                        profile_id,
                        "",
                        "",
                        "",
                        "",
                        "",
                        field,
                        "",
                        _number(value),
                    )
                )
        for stage, value in sorted(latency["stage_p50_ms"].items()):
            if value is not None:
                writer.writerow(
                    (
                        "stage_summary",
                        profile_id,
                        "",
                        "",
                        "",
                        "",
                        "",
                        "stage_p50_ms",
                        stage,
                        _number(value),
                    )
                )
    return stream.getvalue()


def _render_junit(profiles: Sequence[Mapping[str, Any]]) -> str:
    total_tests = len(profiles) * len(SCENARIO_IDS)
    failures = 0
    errors = 0
    for profile in profiles:
        for scenario in profile["scenarios"]:
            status = scenario["status"]
            if status in FAILURE_STATUSES:
                failures += 1
            elif status != "PASS":
                errors += 1

    suites = ElementTree.Element(
        "testsuites",
        {
            "name": "io-e2e-agent-driven-test-p0",
            "tests": str(total_tests),
            "failures": str(failures),
            "errors": str(errors),
        },
    )
    for profile in profiles:
        scenarios = _scenario_map(profile)
        profile_failures = sum(
            scenarios[item]["status"] in FAILURE_STATUSES for item in SCENARIO_IDS
        )
        profile_errors = sum(
            scenarios[item]["status"] not in FAILURE_STATUSES
            and scenarios[item]["status"] != "PASS"
            for item in SCENARIO_IDS
        )
        suite_name = f"feedling.api-key.{profile['profile_id']}"
        suite = ElementTree.SubElement(
            suites,
            "testsuite",
            {
                "name": suite_name,
                "tests": str(len(SCENARIO_IDS)),
                "failures": str(profile_failures),
                "errors": str(profile_errors),
            },
        )
        for scenario_id in SCENARIO_IDS:
            scenario = scenarios[scenario_id]
            testcase = ElementTree.SubElement(
                suite,
                "testcase",
                {"classname": suite_name, "name": scenario_id},
            )
            status = scenario["status"]
            if status == "PASS":
                continue
            failure = scenario["failure"]
            child_tag = "failure" if status in FAILURE_STATUSES else "error"
            ElementTree.SubElement(
                testcase,
                child_tag,
                {
                    "type": status,
                    "message": f"{failure['stage_code']}:{failure['failure_code']}",
                },
            )

    ElementTree.indent(suites, space="  ")
    xml = ElementTree.tostring(
        suites, encoding="unicode", xml_declaration=True, short_empty_elements=True
    )
    return xml + "\n"


def _profile_json(profile: Mapping[str, Any]) -> str:
    return (
        json.dumps(
            profile,
            allow_nan=False,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def _atomic_write(path: Path, content: str) -> None:
    temporary: Path | None = None
    fd = -1
    try:
        fd, raw_temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(raw_temporary)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            fd = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError:
        raise RenderInputError("derived artifacts could not be written") from None
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def render_artifacts(
    *, schema_path: Path, result_path: Path, artifact_root: Path
) -> None:
    """Validate the canonical result and atomically write all derived artifacts."""
    if artifact_root.is_symlink():
        raise RenderInputError("artifact root is missing or unreadable")
    try:
        root = artifact_root.resolve(strict=True)
    except (OSError, RuntimeError):
        raise RenderInputError("artifact root is missing or unreadable") from None
    if not root.is_dir():
        raise RenderInputError("artifact root is not a directory")
    if result_path.is_symlink():
        raise RenderInputError("canonical run result is missing or unreadable")
    try:
        canonical_result = result_path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise RenderInputError(
            "canonical run result is missing or unreadable"
        ) from None
    if canonical_result != root / "run-result.json":
        raise RenderInputError(
            "canonical run result must be run-result.json in the artifact root"
        )

    schema = _read_json(schema_path, "result schema")
    result = _read_json(canonical_result, "canonical run result")
    _validate_schema(schema, result)
    if not isinstance(result, dict):
        raise RenderInputError("canonical run result must be a JSON object")
    profiles = _ordered_profiles(result)

    profiles_dir = root / "profiles"
    if profiles_dir.is_symlink():
        raise RenderInputError("profiles artifact directory is unsafe")
    try:
        profiles_dir.mkdir(mode=0o700, exist_ok=True)
    except OSError:
        raise RenderInputError(
            "profiles artifact directory could not be created"
        ) from None
    if not profiles_dir.is_dir():
        raise RenderInputError("profiles artifact directory is unsafe")
    allowed_profile_names = {f"{profile_id}.json" for profile_id in PROFILE_IDS}
    try:
        if any(
            path.name not in allowed_profile_names for path in profiles_dir.iterdir()
        ):
            raise RenderInputError(
                "profiles artifact directory contains unexpected entries"
            )
    except OSError:
        raise RenderInputError("profiles artifact directory is unreadable") from None

    outputs = {
        root / "matrix.md": _render_matrix(result, profiles),
        root / "latency.csv": _render_latency(profiles),
        root / "junit.xml": _render_junit(profiles),
    }
    outputs.update(
        {
            profiles_dir / f"{profile['profile_id']}.json": _profile_json(profile)
            for profile in profiles
        }
    )
    for path, content in outputs.items():
        _atomic_write(path, content)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render trusted public views from the canonical E2E result"
    )
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        render_artifacts(
            schema_path=args.schema,
            result_path=args.result,
            artifact_root=args.artifacts,
        )
    except RenderInputError as exc:
        print("artifact render: FAIL", file=sys.stderr)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception:
        print("artifact render: FAIL", file=sys.stderr)
        print("ERROR: artifact renderer encountered an internal error", file=sys.stderr)
        return 1
    print("artifact render: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
