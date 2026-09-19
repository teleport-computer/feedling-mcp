"""Shared identity predicate and current garden foreground-mode configuration."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from genesis import plaintext, worker  # noqa: E402


def test_merged_has_identity_rule():
    assert plaintext._merged_has_identity({"identity": {"agent_name": "小柒", "dimensions": []}})
    assert plaintext._merged_has_identity({"identity": {"agent_name": "", "dimensions": [{"name": "温柔"}]}})
    assert not plaintext._merged_has_identity({"identity": {"agent_name": "", "dimensions": []}})
    assert not plaintext._merged_has_identity({"memories": []})


def test_genesis_v2_flag_gate_off_by_default(monkeypatch):
    monkeypatch.delenv("FEEDLING_GENESIS_V2_ENABLED", raising=False)
    assert worker.genesis_v2_enabled() is False                 # default off -> single-pass garden path
    monkeypatch.setenv("FEEDLING_GENESIS_V2_ENABLED", "true")
    assert worker.genesis_v2_enabled() is True
    monkeypatch.setenv("FEEDLING_GENESIS_V2_ENABLED", "0")
    assert worker.genesis_v2_enabled() is False
