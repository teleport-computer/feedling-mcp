"""Terminal ciphertext preservation safety contracts."""

from __future__ import annotations

import re

import pytest

from tee_replicator import terminal_preservation as preservation


def test_preserved_marker_round_trips_original_reason():
    """Dropping the original reason would make guarded revert impossible."""
    reason = "decrypt_failed:enclave_http_403"
    encoded = preservation.encode_preserved_reason("a" * 64, reason)

    assert preservation.parse_preserved_reason(encoded) == ("a" * 64, reason)
    assert preservation.is_terminal_reason(encoded) is False


@pytest.mark.parametrize(
    "reason",
    ["decrypt_failed:old-key", "pdm:local-only", "visibility_local_only"],
)
def test_only_unpreserved_terminal_reasons_are_eligible(reason):
    """A missing terminal prefix would strand an eligible historical row."""
    assert preservation.is_terminal_reason(reason) is True


@pytest.mark.parametrize(
    "reason",
    [
        "requeue",
        "requeue:source_updated",
        "preserved_ciphertext:v2:bad:bad",
        "preserved_ciphertext:v1:not-a-digest:bad",
        "",
    ],
)
def test_nonterminal_or_malformed_reasons_are_not_eligible(reason):
    """Treating backlog or malformed audit state as terminal would waive work."""
    assert preservation.is_terminal_reason(reason) is False
    assert preservation.parse_preserved_reason(reason) is None


def test_canonical_digest_is_stable_across_json_key_order():
    """Equivalent JSON objects must not invalidate an operator plan digest."""
    first = preservation.canonical_row_sha256(
        "chat_messages", ("u", "m", 1.0, {"b": 2, "a": 1}, 7, 0)
    )
    second = preservation.canonical_row_sha256(
        "chat_messages", ("u", "m", 1.0, {"a": 1, "b": 2}, 7, 0)
    )

    assert first == second
    assert re.fullmatch(r"[0-9a-f]{64}", first)


def test_contracts_are_exactly_the_four_approved_families():
    """Adding an unreviewed table must not silently widen destructive scope."""
    assert set(preservation.CONTRACTS) == {
        "chat_messages",
        "memory_moments",
        "identity",
        "frame_envelopes",
    }
