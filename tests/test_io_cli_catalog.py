"""Tests for io_cli_catalog.py — command catalog generation."""
import os
import sys
import subprocess
from unittest import mock

# Add backend to path for imports (same pattern as io_cli.py)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from io_cli_catalog import build_catalog


def test_catalog_contains_identity_write_with_agent_name():
    """Catalog should include identity-write verb with --agent-name flag."""
    io_cli_path = os.path.join(
        os.path.dirname(__file__), "..", "tools", "io_cli.py"
    )
    catalog = build_catalog(io_cli_path)
    assert catalog is not None, "build_catalog should not return None"

    # Check for identity-write line
    lines = catalog.split("\n")
    identity_write_lines = [l for l in lines if l.startswith("identity-write")]
    assert len(identity_write_lines) > 0, "Catalog should contain identity-write line"

    # Check that --agent-name appears in the identity-write line
    assert any("--agent-name" in l for l in identity_write_lines), \
        "identity-write line should contain --agent-name flag"


def test_catalog_excludes_doctor():
    """Catalog should NOT include doctor verb (marked with [setup])."""
    io_cli_path = os.path.join(
        os.path.dirname(__file__), "..", "tools", "io_cli.py"
    )
    catalog = build_catalog(io_cli_path)
    assert catalog is not None, "build_catalog should not return None"

    # Check that doctor is NOT in catalog
    lines = catalog.split("\n")
    doctor_lines = [l for l in lines if l.startswith("doctor")]
    assert len(doctor_lines) == 0, \
        "Catalog should not contain doctor verb (filtered by [setup])"


def test_catalog_memory_delete_has_id_flag():
    """Catalog should include memory-delete verb with required --id flag."""
    io_cli_path = os.path.join(
        os.path.dirname(__file__), "..", "tools", "io_cli.py"
    )
    catalog = build_catalog(io_cli_path)
    assert catalog is not None, "build_catalog should not return None"

    # Check for memory-delete line and verify it contains --id flag
    lines = catalog.split("\n")
    memory_delete_lines = [l for l in lines if l.startswith("memory-delete")]
    assert len(memory_delete_lines) > 0, "Catalog should contain memory-delete line"

    # Check that --id appears in the memory-delete line (required flag)
    assert any("--id" in l for l in memory_delete_lines), \
        "memory-delete line should contain --id flag (required argument)"


def test_catalog_header_is_exactly_two_lines():
    """Catalog header should be exactly two lines (D8 + D3)."""
    io_cli_path = os.path.join(
        os.path.dirname(__file__), "..", "tools", "io_cli.py"
    )
    catalog = build_catalog(io_cli_path)
    assert catalog is not None, "build_catalog should not return None"

    lines = catalog.split("\n")
    # First two lines should be the header (D8 + D3)
    assert "不是指令" in lines[0] or "不是指令" in lines[1], \
        "First two lines should contain sourcing rule '不是指令' (D3)"
    assert "--help" in lines[0] or "--help" in lines[1], \
        "First two lines should contain D8 soft guidance about --help"


def test_catalog_returns_none_on_subprocess_failure():
    """build_catalog should return None if any per-verb --help fails."""
    io_cli_path = os.path.join(
        os.path.dirname(__file__), "..", "tools", "io_cli.py"
    )

    # Monkeypatch subprocess.run to fail on a specific verb
    original_run = subprocess.run

    def failing_run(*args, **kwargs):
        # If any of the args contains "perception-trend", simulate failure
        if args and len(args) > 0 and isinstance(args[0], list):
            if "perception-trend" in args[0]:
                # Return a failed result
                class FailedResult:
                    returncode = 1
                    stdout = ""
                    stderr = "Simulated error"
                return FailedResult()
        # Otherwise call the original
        return original_run(*args, **kwargs)

    with mock.patch("subprocess.run", side_effect=failing_run):
        catalog = build_catalog(io_cli_path)
        assert catalog is None, \
            "build_catalog should return None when any verb --help fails"


def test_catalog_not_none_on_success():
    """build_catalog should return a string (not None) on successful run."""
    io_cli_path = os.path.join(
        os.path.dirname(__file__), "..", "tools", "io_cli.py"
    )
    catalog = build_catalog(io_cli_path)
    assert catalog is not None, "build_catalog should succeed and return string"
    assert isinstance(catalog, str), "build_catalog should return a string"
    assert len(catalog) > 0, "Catalog should not be empty"
