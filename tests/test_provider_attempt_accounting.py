"""Content-free domain values for the canonical provider-attempt ledger."""

import sys
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

try:
    from provider_attempt_accounting import (  # noqa: E402
        AttemptCompleteness,
        AttemptLane,
        AttemptOutcome,
        AttemptRetryKind,
        AttemptSource,
        AttemptState,
        AttemptUsageUnknownReason,
        ProviderAttemptEvent,
        stable_attempt_id,
    )
except ImportError:
    AttemptCompleteness = AttemptLane = AttemptOutcome = AttemptRetryKind = None
    AttemptSource = AttemptState = AttemptUsageUnknownReason = None
    ProviderAttemptEvent = stable_attempt_id = None


def test_stable_attempt_id_is_deterministic_across_replay_and_distinguishes_ordinals():
    """Changing the stable call identity or either retry ordinal must change the row."""
    assert stable_attempt_id is not None
    first = stable_attempt_id("call-a", 2, 1)
    assert re.fullmatch(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", first)
    assert first == stable_attempt_id("call-a", 2, 1)
    assert first != stable_attempt_id("call-a", 3, 1)
    assert first != stable_attempt_id("call-a", 2, 2)
    assert first != stable_attempt_id("call-b", 2, 1)


def test_profile_is_a_first_class_provider_attempt_lane():
    assert AttemptLane.PROFILE.value == "profile"


def test_attempt_event_accepts_only_safe_typed_metadata():
    """A prompt-like field is rejected instead of becoming ledger content."""
    assert ProviderAttemptEvent is not None
    event = ProviderAttemptEvent.create(
        user_id="usr_1",
        call_id="call-1",
        outer_attempt_ordinal=1,
        inner_attempt_ordinal=0,
        source=AttemptSource.RUNTIME_RECORDER,
        lane=AttemptLane.CHAT,
        state=AttemptState.STARTED,
        outcome=AttemptOutcome.UNKNOWN,
        completeness=AttemptCompleteness.STARTED_ONLY,
        requested_provider="openai",
        requested_model="gpt-test",
        resolved_provider="openai",
        resolved_model="gpt-test",
        transport="responses",
    )
    assert event.attempt_id == stable_attempt_id("call-1", 1, 0)
    assert event.as_row()["state"] == "started"
    with pytest.raises(TypeError):
        ProviderAttemptEvent.create(
            user_id="usr_1", call_id="call-1", outer_attempt_ordinal=1,
            inner_attempt_ordinal=0, source=AttemptSource.RUNTIME_RECORDER,
            lane=AttemptLane.CHAT, state=AttemptState.STARTED,
            outcome=AttemptOutcome.UNKNOWN,
            completeness=AttemptCompleteness.STARTED_ONLY,
            requested_provider="openai", requested_model="gpt-test",
            resolved_provider="openai", resolved_model="gpt-test",
            transport="responses", prompt="secret",
        )


def test_attempt_event_rejects_an_enum_from_the_wrong_domain():
    """A lane cannot silently stand in for the distinct source enum."""
    assert ProviderAttemptEvent is not None
    with pytest.raises(TypeError):
        ProviderAttemptEvent.create(
            user_id="usr_1", call_id="call-1", outer_attempt_ordinal=1,
            inner_attempt_ordinal=0, source=AttemptLane.CHAT,
            lane=AttemptLane.CHAT, state=AttemptState.STARTED,
            outcome=AttemptOutcome.UNKNOWN,
            completeness=AttemptCompleteness.STARTED_ONLY,
            requested_provider="openai", requested_model="gpt-test",
            resolved_provider="openai", resolved_model="gpt-test",
            transport="responses",
        )


def test_attempt_event_uses_a_typed_safe_usage_unknown_reason():
    """Unknown usage is a bounded lifecycle code, never a provider error body."""
    kwargs = _valid_event_kwargs()
    kwargs["usage_unknown_reason"] = AttemptUsageUnknownReason.TIMEOUT
    event = ProviderAttemptEvent.create(**kwargs)
    assert event.as_row()["usage_unknown_reason"] == "timeout"

    for untyped_reason in ("timeout", "provider returned secret body"):
        kwargs["usage_unknown_reason"] = untyped_reason
        with pytest.raises(TypeError):
            ProviderAttemptEvent.create(**kwargs)


def test_completed_attempt_event_carries_only_allowlisted_usage_and_timing_facts():
    """Dropping any completed measurement must make the durable full-row fact incomplete."""
    kwargs = _valid_event_kwargs()
    kwargs.update(
        state=AttemptState.COMPLETED,
        outcome=AttemptOutcome.SUCCEEDED,
        completeness=AttemptCompleteness.COMPLETE,
        started_at=datetime(2026, 8, 3, 1, 2, 3, tzinfo=timezone.utc),
        finished_at=datetime(2026, 8, 3, 1, 2, 4, tzinfo=timezone.utc),
        input_tokens=11,
        output_tokens=7,
        reasoning_tokens=3,
        cache_read_tokens=5,
        cache_write_tokens=2,
        cache_miss_tokens=6,
        usage_known=True,
        possibly_billed=False,
        latency_ms=1000.25,
        ttft_ms=125.5,
        revision=2,
    )

    row = ProviderAttemptEvent.create(**kwargs).as_row()

    assert row["started_at"] == kwargs["started_at"]
    assert row["finished_at"] == kwargs["finished_at"]
    assert {
        name: row[name]
        for name in (
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "cache_miss_tokens",
            "usage_known",
            "possibly_billed",
            "latency_ms",
            "ttft_ms",
        )
    } == {
        "input_tokens": 11,
        "output_tokens": 7,
        "reasoning_tokens": 3,
        "cache_read_tokens": 5,
        "cache_write_tokens": 2,
        "cache_miss_tokens": 6,
        "usage_known": True,
        "possibly_billed": False,
        "latency_ms": 1000.25,
        "ttft_ms": 125.5,
    }
    assert row["revision"] == 2
    assert "prompt" not in row
    assert "response" not in row


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("started_at", datetime(2026, 8, 3, 1, 2, 3)),
        ("finished_at", "2026-08-03T01:02:04Z"),
        ("input_tokens", -1),
        ("output_tokens", True),
        ("reasoning_tokens", 1.5),
        ("cache_read_tokens", float("inf")),
        ("cache_write_tokens", "2"),
        ("cache_miss_tokens", -3),
        ("usage_known", 1),
        ("possibly_billed", "false"),
        ("latency_ms", -0.1),
        ("ttft_ms", float("nan")),
        ("revision", -1),
        ("revision", True),
    ],
)
def test_attempt_event_rejects_unsafe_usage_and_timing_scalars(field, value):
    """Measurements are bounded typed facts, never coercion channels for content."""
    kwargs = _valid_event_kwargs()
    kwargs[field] = value
    with pytest.raises((TypeError, ValueError)):
        ProviderAttemptEvent.create(**kwargs)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("call_id", "call id"),
        ("requested_provider", "openai\nheader"),
        ("requested_model", "Bearer secret"),
        ("resolved_provider", "https://provider.example"),
        ("resolved_model", "sk-live-secret"),
        ("transport", "response body"),
        ("runtime", "runtime\tvalue"),
        ("turn_id", "Authorization: Bearer token"),
        ("round_id", "round\x00one"),
        ("provider_request_id", "api_key=secret"),
        ("installation_id", "installation\nsecret"),
    ],
)
def test_attempt_event_rejects_content_or_credential_shaped_text(field, value):
    """Every free-form text input must stay inside the ledger identifier allowlist."""
    kwargs = _valid_event_kwargs()
    kwargs[field] = value
    with pytest.raises(ValueError):
        ProviderAttemptEvent.create(**kwargs)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("outer_attempt_ordinal", True),
        ("inner_attempt_ordinal", False),
        ("job_id", True),
        ("job_id", -1),
        ("retry_kind", "initial"),
    ],
)
def test_attempt_event_rejects_invalid_optional_scalars_and_untyped_retry_kind(field, value):
    """Boolean ordinals and untyped retry strings cannot silently enter the ledger."""
    kwargs = _valid_event_kwargs()
    kwargs[field] = value
    with pytest.raises((TypeError, ValueError)):
        ProviderAttemptEvent.create(**kwargs)


def _valid_event_kwargs() -> dict:
    assert AttemptRetryKind is not None and AttemptUsageUnknownReason is not None
    return {
        "user_id": "usr_1",
        "call_id": "call-1:attempt.1",
        "outer_attempt_ordinal": 1,
        "inner_attempt_ordinal": 0,
        "source": AttemptSource.RUNTIME_RECORDER,
        "lane": AttemptLane.CHAT,
        "state": AttemptState.STARTED,
        "outcome": AttemptOutcome.UNKNOWN,
        "completeness": AttemptCompleteness.STARTED_ONLY,
        "requested_provider": "openrouter/openai",
        "requested_model": "openai/gpt-4o-mini",
        "resolved_provider": "openai",
        "resolved_model": "gpt-4o-mini",
        "transport": "responses",
        "retry_kind": AttemptRetryKind.INITIAL,
        "runtime": "v2-runtime",
        "job_id": 1,
        "turn_id": "turn-1",
        "round_id": "round:1",
        "provider_request_id": "req_1",
        "installation_id": "install-1",
    }
