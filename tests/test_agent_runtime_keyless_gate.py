"""Pure-unit tests for the cold-start credential spawn-gate (no DB).

A freshly-discovered host-all (zero-roster) user can be handed to the supervisor
before ``_resolve_one`` has fetched+decrypted their provider key. Spawning then
produces a keyless consumer whose first CLI turn fails ("Not logged in · Please
run /login") and posts a premature "有点慢" fallback (prod/test 2026-07-25). The
gate defers such a spawn until the key resolves, bounded by a cap so a
permanently unresolvable key still falls through to the existing fallback.

DB-free like test_agent_runtime_genesis_gate: exercises only the pure decisions.
The DB-backed tick wiring lives in tests/test_agent_runtime_supervisor.py.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from agent_runtime import supervisor


def test_no_credential_true_only_when_both_keys_absent():
    # Zero-roster host-all entry before its key resolves → keyless.
    assert supervisor._entry_has_no_credential({}) is True
    assert supervisor._entry_has_no_credential({"user_id": "u"}) is True
    assert supervisor._entry_has_no_credential({"api_key": "", "provider_key": "  "}) is True


def test_has_credential_when_either_key_present():
    # Dev roster carries api_key; a resolved host-all entry carries provider_key.
    assert supervisor._entry_has_no_credential({"api_key": "key-u"}) is False
    assert supervisor._entry_has_no_credential({"provider_key": "sk-ant-xxx"}) is False
    assert supervisor._entry_has_no_credential(
        {"api_key": "k", "provider_key": "p"}) is False


def test_defer_expires_only_after_cap():
    cap = 90.0
    first = 1000.0
    assert supervisor._keyless_defer_expired(first, first, cap) is False        # just seen
    assert supervisor._keyless_defer_expired(first, first + 30.0, cap) is False  # 1-2 ticks
    assert supervisor._keyless_defer_expired(first, first + 89.9, cap) is False  # still within
    assert supervisor._keyless_defer_expired(first, first + 90.0, cap) is True   # at cap
    assert supervisor._keyless_defer_expired(first, first + 120.0, cap) is True  # past cap
