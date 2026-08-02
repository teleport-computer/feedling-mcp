"""Content-free domain values for the canonical provider-attempt ledger."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

try:
    from provider_attempt_accounting import (  # noqa: E402
        AttemptCompleteness,
        AttemptLane,
        AttemptOutcome,
        AttemptSource,
        AttemptState,
        ProviderAttemptEvent,
        stable_attempt_id,
    )
except ModuleNotFoundError:
    AttemptCompleteness = AttemptLane = AttemptOutcome = AttemptSource = AttemptState = None
    ProviderAttemptEvent = stable_attempt_id = None


def test_stable_attempt_id_is_deterministic_across_replay_and_distinguishes_ordinals():
    """Changing the stable call identity or either retry ordinal must change the row."""
    assert stable_attempt_id is not None
    first = stable_attempt_id("call-a", 2, 1)
    assert first == stable_attempt_id("call-a", 2, 1)
    assert first != stable_attempt_id("call-a", 3, 1)
    assert first != stable_attempt_id("call-a", 2, 2)
    assert first != stable_attempt_id("call-b", 2, 1)


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
