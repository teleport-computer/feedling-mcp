"""Regression: no identity write path may persist a non-integer dimension value.

Root cause of the prod "identity decrypt failed" loop: self-hosted BYOK weak
models emit dimension values on a 0–1 probability scale (0.95, 0.9, ...). The
server-side card builders let those floats through into the encrypted envelope,
and iOS' integer JSONDecoder then raised dataCorrupted on the decrypted plaintext
JSON, failing the whole card. These tests lock every server-side plaintext card
builder to the card_policy 0–100 integer contract WITHOUT a DB (pure functions).
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from identity import actions as identity_actions_mod  # noqa: E402
from genesis import service as genesis_service  # noqa: E402


def _values(payload):
    return {d["name"]: d["value"] for d in payload["dimensions"]}


def test_actions_payload_from_plain_normalizes_float_values():
    # init / profile_patch / dimension_nudge all build the plaintext card through
    # _identity_payload_from_plain; a 0–1-scale float must be rescaled to an int.
    plain = {"agent_name": "阿锐", "dimensions": [
        {"name": "锐利", "value": 0.95, "description": "x"},
        {"name": "温情", "value": 0.6, "description": "y"},
        {"name": "已是整数", "value": 88, "description": "z"},
    ]}
    payload = identity_actions_mod._identity_payload_from_plain(plain)
    assert _values(payload) == {"锐利": 95, "温情": 60, "已是整数": 88}
    for d in payload["dimensions"]:
        assert type(d["value"]) is int


def test_genesis_payload_from_output_restores_0_1_scale():
    # The genesis distill/import path previously int()-truncated (0.95 -> 0),
    # silently zeroing a BYOK 0–1-scale card. It must rescale to 0–100 ints.
    output = {"identity": {"agent_name": "阿锐", "dimensions": [
        {"name": "锐利", "value": 0.95, "description": "锐"},
        {"name": "温情", "value": 0.6, "description": "温"},
        {"name": "已是整数", "value": 88, "description": "整"},
        {"name": "文字分值", "value": "高", "description": "落回默认"},
    ]}}
    payload = genesis_service._identity_payload_from_output(output)
    got = _values(payload)
    assert got["锐利"] == 95
    assert got["温情"] == 60
    assert got["已是整数"] == 88
    assert got["文字分值"] == 50  # non-numeric falls back to the 50 default (unchanged)
    for d in payload["dimensions"]:
        assert type(d["value"]) is int
