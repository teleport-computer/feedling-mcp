"""Behavior tests for the GitHub Actions TEE replication request guard."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).parent.parent / "scripts/tee/replication_workflow_guard.py"


def _run(*args: str, env: dict[str, str] | None = None):
    merged = os.environ.copy()
    merged.update(env or {})
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        env=merged,
        text=True,
        capture_output=True,
        check=False,
    )


def test_build_emits_typed_reflow_request():
    result = _run(
        "build",
        env={
            "ACTION": "reflow",
            "TABLE_IN": "voice_transcripts",
            "DRY_RUN_IN": "false",
            "CONFIRM_IN": "MIGRATE",
            "QPS_IN": "20.5",
            "EXPECTED_STALE_IN": "21",
        },
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "action": "reflow",
        "table": "voice_transcripts",
        "dry_run": False,
        "confirm": "MIGRATE",
        "qps": 20.5,
        "expected_stale": 21,
    }


def test_build_rejects_malformed_or_nonfinite_numbers():
    for name, value in (
        ("QPS_IN", "twenty"),
        ("QPS_IN", "NaN"),
        ("QPS_IN", "Infinity"),
        ("EXPECTED_STALE_IN", "1.5"),
        ("EXPECTED_STALE_IN", "-1"),
    ):
        result = _run(
            "build",
            env={"ACTION": "prune", "DRY_RUN_IN": "true", name: value},
        )
        assert result.returncode != 0


def test_check_rejects_green_http_with_failed_apply(tmp_path):
    response = tmp_path / "response.json"
    response.write_text(
        json.dumps({"ok": False, "failures": 1, "refused": "count changed"})
    )
    result = _run(
        "check",
        "200",
        str(response),
        env={"ACTION": "prune", "DRY_RUN_IN": "false"},
    )
    assert result.returncode != 0


def test_check_accepts_successful_apply_and_read_only_dry_run(tmp_path):
    success = tmp_path / "success.json"
    success.write_text(json.dumps({"ok": True, "failures": 0}))
    assert _run(
        "check", "200", str(success),
        env={"ACTION": "prune", "DRY_RUN_IN": "false"},
    ).returncode == 0

    preview = tmp_path / "preview.json"
    preview.write_text(json.dumps({"ok": False, "failures": 1, "refused": "preview"}))
    assert _run(
        "check", "200", str(preview),
        env={"ACTION": "prune", "DRY_RUN_IN": "true"},
    ).returncode == 0
