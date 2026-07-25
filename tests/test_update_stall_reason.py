"""
Tests for the self-update stall-reason self-diagnosis in
tools/chat_resident_consumer.py.

``_self_update_stall_reason()`` is a cheap, non-blocking read of a module var
that ``_run_self_update`` sets as a side effect of the checks it already runs
(dirty tree / AUTO_UPDATE / fetch result) on every idle poll. It must never run
git itself. Covers the reason transitions (dirty/disabled/fetch_failed/"") and
that it clears on a successful update or no-op, mirroring the existing
_should_self_update / _run_self_update coverage in
tests/test_chat_resident_self_update.py.

Run with: pytest tests/test_update_stall_reason.py -v
"""

import os
import sys
from pathlib import Path

# Module bootstrap — set required env vars before the module is imported,
# matching tests/test_chat_resident_consumer.py / test_chat_resident_self_update.py.
_REQUIRED_ENV = {
    "FEEDLING_API_URL": "http://localhost:5001",
    "FEEDLING_API_KEY": "test_key_00000000",
}
for k, v in _REQUIRED_ENV.items():
    os.environ.setdefault(k, v)

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import tools.chat_resident_consumer as crc  # noqa: E402  (after env setup)

from _fake_clock import freeze_monotonic  # noqa: E402


def _refuse_git(*_a, **_k):
    raise AssertionError("_self_update_stall_reason() must not run git")


def _refuse_dirty_check():
    raise AssertionError("_self_update_stall_reason() must not run git")


def test_self_update_stall_reason_is_a_cheap_dict_read(monkeypatch):
    # Even if the git seams would raise, reading the reason must never call
    # them — it only returns what _run_self_update last computed.
    monkeypatch.setattr(crc, "_git", _refuse_git)
    monkeypatch.setattr(crc, "_git_tree_dirty", _refuse_dirty_check)
    monkeypatch.setattr(crc, "_git_fetch", _refuse_git)
    monkeypatch.setitem(crc._self_update_stall, "value", "dirty")
    assert crc._self_update_stall_reason() == "dirty"


def test_reason_defaults_to_empty():
    crc._self_update_stall["value"] = ""
    assert crc._self_update_stall_reason() == ""


def test_update_stall_headers_only_present_when_non_empty(monkeypatch):
    monkeypatch.setitem(crc._self_update_stall, "value", "")
    assert crc._update_stall_headers() == {}
    monkeypatch.setitem(crc._self_update_stall, "value", "fetch_failed")
    assert crc._update_stall_headers() == {
        "X-Feedling-Update-Stall": "fetch_failed"
    }


# ---------------------------------------------------------------------------
# _run_self_update — reason set/cleared at each skip/success branch (mirrors
# the update_seams fixture in tests/test_chat_resident_self_update.py).
# ---------------------------------------------------------------------------


def _apply_update_seams(monkeypatch):
    applied = []
    # Pin the clock — `_last_self_update_mono = 0.0` is only outside the
    # throttle window on a host with real uptime (see tests/_fake_clock.py).
    freeze_monotonic(monkeypatch)
    monkeypatch.setattr(crc, "AUTO_UPDATE", True)
    monkeypatch.setattr(crc, "_HOSTED", False)
    monkeypatch.setattr(crc, "_last_self_update_mono", 0.0)
    monkeypatch.setattr(crc, "RUNNING_COMMIT", "local00")
    monkeypatch.setattr(crc, "_consumer_commit", lambda: "local00")
    monkeypatch.setitem(crc._compat_commit, "value", "")
    monkeypatch.setitem(crc._self_update_stall, "value", "")
    monkeypatch.setattr(crc, "_git_fetch", lambda target: True)
    monkeypatch.setattr(crc, "_git_tree_dirty", lambda: False)
    monkeypatch.setattr(crc, "_runtime_repo_files", lambda: {"tools/io_cli.py"})
    monkeypatch.setattr(
        crc, "_git_changed_files", lambda local, target: {"tools/io_cli.py"}
    )
    monkeypatch.setattr(
        crc, "_apply_self_update", lambda local, target, changed: applied.append(target)
    )
    return applied


def test_dirty_tree_sets_dirty_reason(monkeypatch):
    _apply_update_seams(monkeypatch)
    monkeypatch.setattr(crc, "_git_tree_dirty", lambda: True)
    crc._run_self_update("target99")
    assert crc._self_update_stall_reason() == "dirty"


def test_disabled_sets_disabled_reason(monkeypatch):
    _apply_update_seams(monkeypatch)
    monkeypatch.setattr(crc, "AUTO_UPDATE", False)
    crc._run_self_update("target99")
    assert crc._self_update_stall_reason() == "disabled"


def test_hosted_never_reports_disabled(monkeypatch):
    # Hosted (immutable image) never self-updates by design — that's not a
    # stall a self-hoster can act on, so it must not surface "disabled".
    _apply_update_seams(monkeypatch)
    monkeypatch.setattr(crc, "AUTO_UPDATE", False)
    monkeypatch.setattr(crc, "_HOSTED", True)
    crc._run_self_update("target99")
    assert crc._self_update_stall_reason() == ""


def test_fetch_failure_sets_fetch_failed_reason(monkeypatch):
    _apply_update_seams(monkeypatch)
    monkeypatch.setattr(crc, "_git_fetch", lambda target: False)
    crc._run_self_update("target99")
    assert crc._self_update_stall_reason() == "fetch_failed"


def test_successful_update_clears_reason(monkeypatch):
    applied = _apply_update_seams(monkeypatch)
    crc._self_update_stall["value"] = "fetch_failed"  # stale from a prior poll
    crc._run_self_update("target99")
    assert applied == ["target99"]
    assert crc._self_update_stall_reason() == ""


def test_already_on_target_clears_reason(monkeypatch):
    _apply_update_seams(monkeypatch)
    crc._self_update_stall["value"] = "dirty"  # stale from a prior poll
    fetched = []
    monkeypatch.setattr(crc, "_git_fetch", lambda target: fetched.append(target) or True)
    crc._run_self_update("local00abc")  # local is a prefix of target -> no-op
    assert fetched == []
    assert crc._self_update_stall_reason() == ""


def test_irrelevant_release_clears_reason(monkeypatch):
    _apply_update_seams(monkeypatch)
    crc._self_update_stall["value"] = "dirty"  # stale from a prior poll
    monkeypatch.setattr(
        crc, "_git_changed_files", lambda local, target: {"docs/CHANGELOG.md"}
    )
    crc._run_self_update("target99")
    assert crc._self_update_stall_reason() == ""


def test_self_heal_dirty_tree_sets_dirty_reason(monkeypatch):
    # External checkout already at target (Seven-VPS wedge shape), but the
    # working tree has uncommitted changes blocking the re-exec.
    _apply_update_seams(monkeypatch)
    monkeypatch.setattr(crc, "_consumer_commit", lambda: "target99")  # disk moved
    monkeypatch.setattr(crc, "_git_tree_dirty", lambda: True)
    crc._run_self_update("target99")
    assert crc._self_update_stall_reason() == "dirty"


def test_self_heal_irrelevant_release_clears_reason(monkeypatch):
    # Sibling of test_irrelevant_release_clears_reason, but for the self-heal
    # path (disk already checked out at target): a release that touches
    # nothing this consumer loads must sign compat AND clear any stale reason,
    # not just skip.
    _apply_update_seams(monkeypatch)
    crc._self_update_stall["value"] = "dirty"  # stale from a prior poll
    monkeypatch.setattr(crc, "_consumer_commit", lambda: "target99")  # disk moved
    monkeypatch.setattr(
        crc, "_git_changed_files", lambda local, target: {"docs/CHANGELOG.md"}
    )
    crc._run_self_update("target99")
    assert crc._self_update_stall_reason() == ""
    assert crc._compat_commit["value"] == "target99"


def test_self_heal_reexec_clears_reason(monkeypatch):
    applied = _apply_update_seams(monkeypatch)
    crc._self_update_stall["value"] = "dirty"  # stale from a prior poll
    monkeypatch.setattr(crc, "_consumer_commit", lambda: "target99")  # disk moved
    crc._run_self_update("target99")
    assert applied == ["target99"]
    assert crc._self_update_stall_reason() == ""
