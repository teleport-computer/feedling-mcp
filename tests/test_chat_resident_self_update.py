"""
Tests for the consumer self-update logic in tools/chat_resident_consumer.py
===========================================================================

Covers the pure decision function ``_should_self_update`` and the runtime
dependency-set helper ``_runtime_repo_files``. These are side-effect-free and
do not need a backend or DB.

Run with: pytest tests/test_chat_resident_self_update.py -v
"""

import os
import sys
from pathlib import Path

import pytest

# Module bootstrap — set required env vars before the module is imported,
# matching tests/test_chat_resident_consumer.py.
_REQUIRED_ENV = {
    "FEEDLING_API_URL": "http://localhost:5001",
    "FEEDLING_API_KEY": "test_key_00000000",
}
for k, v in _REQUIRED_ENV.items():
    os.environ.setdefault(k, v)

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

# Ensure a known in-repo backend module is loaded so _runtime_repo_files() can
# observe it via sys.modules.
import content_encryption  # noqa: F401,E402

import tools.chat_resident_consumer as crc  # noqa: E402  (after env setup)


# ---------------------------------------------------------------------------
# _should_self_update — pure decision truth table
# ---------------------------------------------------------------------------

_BASE = dict(
    local="aaaaaaa",
    target="bbbbbbb",
    dirty=False,
    enabled=True,
    hosted=False,
    relevant_changed=True,
)


def _call(**overrides):
    kwargs = {**_BASE, **overrides}
    return crc._should_self_update(**kwargs)


def test_updates_when_all_conditions_met():
    assert _call() is True


def test_skips_when_disabled():
    assert _call(enabled=False) is False


def test_skips_when_hosted():
    # Supervisor-managed CVM: immutable image, must never self-mutate.
    assert _call(hosted=True) is False


def test_skips_when_target_empty():
    assert _call(target="") is False


def test_skips_when_target_is_dev():
    # Backend reports "dev" when FEEDLING_GIT_COMMIT is unset — not a real target.
    assert _call(target="dev") is False


def test_skips_when_local_unknown():
    # Can't resolve our own commit (not a git checkout) -> never touch the tree.
    assert _call(local="") is False


def test_skips_when_already_on_target_short_prefix_of_long():
    # local is the short hash, target the full hash of the same commit.
    assert _call(local="abc1234", target="abc1234def5678") is False


def test_skips_when_already_on_target_long_prefix_of_short():
    assert _call(local="abc1234def5678", target="abc1234") is False


def test_skips_when_working_tree_dirty():
    # User has uncommitted local edits — protect them, only warn elsewhere.
    assert _call(dirty=True) is False


def test_skips_when_no_relevant_paths_changed():
    # Backend shipped a release that doesn't touch any file the consumer loads.
    assert _call(relevant_changed=False) is False


# ---------------------------------------------------------------------------
# _runtime_repo_files — auto-derived dependency whitelist
# ---------------------------------------------------------------------------

def test_runtime_files_includes_consumer_module_itself():
    files = crc._runtime_repo_files()
    assert "tools/chat_resident_consumer.py" in files


def test_runtime_files_includes_loaded_backend_module():
    # content_encryption was imported above; it lives under backend/ and the
    # consumer imports it at runtime, so it must be in the dependency set.
    files = crc._runtime_repo_files()
    assert "backend/content_encryption.py" in files


def test_runtime_files_includes_explicit_subprocess_and_requirements():
    # io_cli is shelled out (never in sys.modules) but is distributed in the
    # same checkout; requirements files gate the pip-install fallback.
    files = crc._runtime_repo_files()
    assert "tools/io_cli.py" in files
    assert "tools/chat_resident_requirements.txt" in files
    assert "backend/requirements.txt" in files


def test_runtime_files_excludes_stdlib_and_site_packages():
    files = crc._runtime_repo_files()
    # All entries are repo-relative (no absolute paths, no escaping the repo).
    for f in files:
        assert not f.startswith("/"), f
        assert ".." not in f.split("/"), f
        assert "site-packages" not in f, f


# ---------------------------------------------------------------------------
# _run_self_update — orchestration over the git seams (mocked)
# ---------------------------------------------------------------------------

@pytest.fixture()
def update_seams(monkeypatch):
    """Wire deterministic git seams + capture whether an update is applied."""
    applied = []
    monkeypatch.setattr(crc, "AUTO_UPDATE", True)
    monkeypatch.setattr(crc, "_HOSTED", False)
    monkeypatch.setattr(crc, "_last_self_update_mono", 0.0)
    monkeypatch.setattr(crc, "_consumer_commit", lambda: "local00")
    monkeypatch.setattr(crc, "_git_fetch", lambda target: True)
    monkeypatch.setattr(crc, "_git_tree_dirty", lambda: False)
    # Pin the dependency set deterministically — the real one depends on what
    # this shared test process happened to import (the Flask app pulls in half
    # the backend), which would make relevance non-deterministic.
    monkeypatch.setattr(crc, "_runtime_repo_files", lambda: {"tools/io_cli.py"})
    # default: a change that IS in the runtime file set
    monkeypatch.setattr(crc, "_git_changed_files", lambda local, target: {"tools/io_cli.py"})
    monkeypatch.setattr(crc, "_apply_self_update", lambda local, target, changed: applied.append(target))
    return applied


def test_run_self_update_applies_when_relevant_and_clean(update_seams):
    crc._run_self_update("target99")
    assert update_seams == ["target99"]


def test_run_self_update_skips_when_dirty(update_seams, monkeypatch):
    monkeypatch.setattr(crc, "_git_tree_dirty", lambda: True)
    crc._run_self_update("target99")
    assert update_seams == []  # protect uncommitted edits


def test_run_self_update_skips_when_no_relevant_paths(update_seams, monkeypatch):
    # Backend release only touched files the consumer never loads.
    monkeypatch.setattr(crc, "_git_changed_files", lambda local, target: {"docs/CHANGELOG.md"})
    crc._run_self_update("target99")
    assert update_seams == []


def test_run_self_update_skips_when_already_on_target(update_seams, monkeypatch):
    fetched = []
    monkeypatch.setattr(crc, "_git_fetch", lambda target: fetched.append(target) or True)
    crc._run_self_update("local00abc")  # local is a prefix of target
    assert update_seams == []
    assert fetched == []  # cheap pre-check returns before any network/git fetch


def test_run_self_update_skips_when_disabled(update_seams, monkeypatch):
    monkeypatch.setattr(crc, "AUTO_UPDATE", False)
    crc._run_self_update("target99")
    assert update_seams == []


def test_run_self_update_skips_when_hosted(update_seams, monkeypatch):
    monkeypatch.setattr(crc, "_HOSTED", True)
    crc._run_self_update("target99")
    assert update_seams == []


def test_run_self_update_throttles_repeated_attempts(update_seams):
    crc._run_self_update("target99")
    crc._run_self_update("target99")  # within the throttle window
    assert update_seams == ["target99"]  # applied at most once


def test_run_self_update_applies_for_lazily_imported_proactive_module(update_seams, monkeypatch):
    # The consumer imports proactive.adapters_v2 / runtime_v2 lazily, so a fresh
    # consumer that hasn't run a proactive job yet won't have them in sys.modules
    # (here _runtime_repo_files is mocked to {tools/io_cli.py}). A release that
    # only touches that lazy surface must still update — it's genuinely our code.
    monkeypatch.setattr(crc, "_git_changed_files", lambda local, target: {"backend/proactive/adapters_v2.py"})
    crc._run_self_update("target99")
    assert update_seams == ["target99"]


# ---------------------------------------------------------------------------
# _apply_self_update — checkout must precede pip so pip reads the NEW reqs
# ---------------------------------------------------------------------------

def test_apply_checks_out_before_pip_install(monkeypatch):
    order = []
    monkeypatch.setattr(crc, "_git_checkout", lambda target: order.append("checkout") or True)
    monkeypatch.setattr(crc, "_pip_install", lambda req: order.append(f"pip:{req}"))
    monkeypatch.setattr(crc.os, "execv", lambda *a: order.append("exec"))
    crc._apply_self_update("local00", "target99", {"backend/requirements.txt"})
    # checkout first (so the working tree — and thus requirements.txt — is the
    # target commit), THEN pip install, THEN re-exec.
    assert order == ["checkout", "pip:backend/requirements.txt", "exec"]


def test_apply_skips_pip_and_exec_when_checkout_fails(monkeypatch):
    order = []
    monkeypatch.setattr(crc, "_git_checkout", lambda target: order.append("checkout") or False)
    monkeypatch.setattr(crc, "_pip_install", lambda req: order.append("pip"))
    monkeypatch.setattr(crc.os, "execv", lambda *a: order.append("exec"))
    crc._apply_self_update("local00", "target99", {"backend/requirements.txt"})
    assert order == ["checkout"]  # bailed; never pip-installed or re-exec'd
