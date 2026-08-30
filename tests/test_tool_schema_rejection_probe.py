from __future__ import annotations

import importlib.util
from pathlib import Path


_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "tools"
    / "e2e"
    / "tool_schema_rejection_probe.py"
)
_SPEC = importlib.util.spec_from_file_location(
    "tool_schema_rejection_probe", _SCRIPT
)
assert _SPEC is not None and _SPEC.loader is not None
probe = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(probe)


def _payload(*, attempts, fallback_counts=None):
    detail = {"attempts": attempts}
    if fallback_counts is not None:
        detail["fallback_counts"] = fallback_counts
    return {"user": {"provider_attempt_ledger": detail}}


def test_probe_keeps_instrument_absent_impl_and_readable_distinct(capsys):
    assert probe._evaluate({}) == 3
    assert probe._evaluate(_payload(attempts=[
        {
            "parent_message_id": "v2job:1",
            "outcome": "provider_error",
            "error_class": "ProviderError",
        },
        {"parent_message_id": "v2job:1", "outcome": "ok"},
    ])) == 1
    assert probe._evaluate(_payload(attempts=[{
        "outcome": "provider_error", "status_code": 400,
        "fallback_reason": "tool_schema_rejected"
    }])) == 2
    assert probe._evaluate(_payload(
        attempts=[
            {
                "outcome": "provider_error",
                "status_code": 400,
                "fallback_reason": "tool_schema_rejected",
            },
            {
                "outcome": "provider_error",
                "status_code": 422,
                "fallback_reason": "tool_schema_rejected",
            },
        ],
        fallback_counts=[
            {"fallback_reason": "tool_schema_rejected", "status_code": 400,
             "count": 1},
            {"fallback_reason": "tool_schema_rejected", "status_code": 422,
             "count": 1},
        ],
    )) == 0
    output = capsys.readouterr().out
    assert "VERDICT=INSTRUMENT_DOWN" in output
    assert "VERDICT=FIELD_ABSENT" in output
    assert "VERDICT=IMPL_ONLY" in output
    assert "VERDICT=READABLE" in output


def test_probe_finds_semantically_named_nested_status(capsys):
    result = probe._evaluate(_payload(
        attempts=[{
            "outcome": "provider_error",
            "provider_detail": {
                "http_status_code": 422,
                "rejection_reason": "tool_schema_rejected",
            },
        }],
        fallback_counts=[{
            "fallback_reason": "tool_schema_rejected",
            "status_code": 422,
            "count": 1,
        }],
    ))

    assert result == 0
    assert "status_code distribution: {'422': 1}" in capsys.readouterr().out


def test_probe_does_not_call_unrelated_provider_status_readable(capsys):
    result = probe._evaluate(_payload(
        attempts=[{
            "outcome": "provider_error",
            "status_code": 503,
            "fallback_reason": "",
        }],
        fallback_counts=[{
            "fallback_reason": "tagged_images_rejected",
            "status_code": 404,
            "count": 1,
        }],
    ))

    assert result == 3
    assert "no_tool_schema_rejection_in_window" in capsys.readouterr().out


def test_probe_rejects_aggregate_reason_not_anchored_to_failed_row(capsys):
    result = probe._evaluate(_payload(
        attempts=[
            {"outcome": "provider_error", "status_code": 400},
            {
                "outcome": "ok",
                "status_code": 400,
                "fallback_reason": "tool_schema_rejected",
            },
        ],
        # The production summarizer correctly ignores the misplaced ok row.
        fallback_counts=[],
    ))

    assert result == 4
    assert "VERDICT=REASON_UNANCHORED" in capsys.readouterr().out


def test_probe_distinguishes_missing_aggregate_key_from_empty_count(capsys):
    attempt = {
        "outcome": "provider_error",
        "status_code": 422,
        "fallback_reason": "tool_schema_rejected",
    }

    assert probe._evaluate(_payload(attempts=[attempt])) == 2
    missing_output = capsys.readouterr().out
    assert "reason=admin_aggregate_key_absent" in missing_output

    assert probe._evaluate(
        _payload(attempts=[attempt], fallback_counts=[])
    ) == 2
    empty_output = capsys.readouterr().out
    assert "reason=admin_aggregate_has_no_matching_target_count" in empty_output


def test_probe_ignores_empty_status_and_status_on_ok_rows(capsys):
    result = probe._evaluate(_payload(
        attempts=[
            {
                "parent_message_id": "v2job:2",
                "outcome": "provider_error",
                "status_code": "",
            },
            {
                "parent_message_id": "v2job:2",
                "outcome": "ok",
                "status_code": 400,
            },
        ],
        fallback_counts=[],
    ))

    assert result == 1
    assert "VERDICT=FIELD_ABSENT" in capsys.readouterr().out


def test_probe_does_not_call_quiet_window_field_absent(capsys):
    result = probe._evaluate(_payload(
        attempts=[{
            "parent_message_id": "v2job:terminal",
            "outcome": "provider_error",
        }],
        fallback_counts=[],
    ))

    assert result == 3
    assert "phenomenon_absent_no_failed_then_ok_job" in capsys.readouterr().out
