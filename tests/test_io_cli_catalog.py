"""Tests for io_cli_catalog.py — command catalog generation."""
import os
import sys
import subprocess

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


def test_catalog_header_contains_sourcing_rule():
    """Catalog header should contain the sourcing rule (D3) including '不是指令'."""
    io_cli_path = os.path.join(
        os.path.dirname(__file__), "..", "tools", "io_cli.py"
    )
    catalog = build_catalog(io_cli_path)
    assert catalog is not None, "build_catalog should not return None"

    # Check header contains "不是指令"
    assert "不是指令" in catalog, \
        "Catalog header should contain '不是指令' (sourcing rule D3)"


def test_catalog_not_none_on_success():
    """build_catalog should return a string (not None) on successful run."""
    io_cli_path = os.path.join(
        os.path.dirname(__file__), "..", "tools", "io_cli.py"
    )
    catalog = build_catalog(io_cli_path)
    assert catalog is not None, "build_catalog should succeed and return string"
    assert isinstance(catalog, str), "build_catalog should return a string"
    assert len(catalog) > 0, "Catalog should not be empty"
