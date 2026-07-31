"""Pure provider-health state-machine coverage (no PostgreSQL required)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import provider_health  # noqa: E402


def _failure(
    state: dict,
    *,
    now: float,
    selected_at: float,
    error_class: str,
    blame: str,
) -> dict:
    return provider_health.evolve_failure(
        state,
        error_class=error_class,
        blame=blame,
        now=now,
        route_selected_at=selected_at,
    )


def test_transient_failures_with_success_never_need_user_action():
    selected_at = 100.0
    state: dict = {}
    for index in range(15):
        state = _failure(
            state,
            now=selected_at + index * 4 * 60 * 60,
            selected_at=selected_at,
            error_class="upstream_unavailable" if index % 2 else "rate_limited",
            blame="provider_transient",
        )
    state = provider_health.evolve_success(
        state,
        now=selected_at + 60 * 60 * 60,
    )
    for index in range(15, 30):
        state = _failure(
            state,
            now=selected_at + (index + 1) * 4 * 60 * 60,
            selected_at=selected_at,
            error_class="upstream_unavailable" if index % 2 else "rate_limited",
            blame="provider_transient",
        )

    assert state["provider_state"] == provider_health.PROVIDER_STATE_OK
    assert state["last_provider_success_at"] == selected_at + 60 * 60 * 60


def test_single_user_provider_failure_after_50h_transient_does_not_enter():
    success_at = 1_000.0
    state = provider_health.evolve_success({}, now=success_at)
    for index in range(1, 26):
        state = _failure(
            state,
            now=success_at + index * 2 * 60 * 60,
            selected_at=100.0,
            error_class="upstream_unavailable",
            blame="provider_transient",
        )

    first_user_failure = _failure(
        state,
        now=success_at + 51 * 60 * 60,
        selected_at=100.0,
        error_class="auth_invalid",
        blame="user_provider",
    )
    confirmed_user_failure = _failure(
        first_user_failure,
        now=success_at + 52 * 60 * 60,
        selected_at=100.0,
        error_class="auth_invalid",
        blame="user_provider",
    )

    assert first_user_failure["provider_state"] == provider_health.PROVIDER_STATE_OK
    assert (
        confirmed_user_failure["provider_state"]
        == provider_health.PROVIDER_STATE_NEEDS_USER_ACTION
    )


def test_48h_user_provider_failures_after_last_success_enter():
    success_at = 500.0
    state = provider_health.evolve_success({}, now=success_at)

    state = _failure(
        state,
        now=success_at + 48 * 60 * 60,
        selected_at=100.0,
        error_class="quota_insufficient",
        blame="user_provider",
    )

    assert (
        state["provider_state"]
        == provider_health.PROVIDER_STATE_NEEDS_USER_ACTION
    )
    assert state["last_probe_at"] == success_at + 48 * 60 * 60


def test_47h_does_not_enter_but_49h_does():
    success_at = 1_000.0
    base = provider_health.evolve_success({}, now=success_at)

    at_47h = _failure(
        base,
        now=success_at + 47 * 60 * 60,
        selected_at=100.0,
        error_class="auth_invalid",
        blame="user_provider",
    )
    at_49h = _failure(
        base,
        now=success_at + 49 * 60 * 60,
        selected_at=100.0,
        error_class="auth_invalid",
        blame="user_provider",
    )

    assert at_47h["provider_state"] == provider_health.PROVIDER_STATE_OK
    assert (
        at_49h["provider_state"]
        == provider_health.PROVIDER_STATE_NEEDS_USER_ACTION
    )


def test_never_succeeded_uses_route_selected_at():
    selected_at = 2_000.0

    state = _failure(
        {},
        now=selected_at + 49 * 60 * 60,
        selected_at=selected_at,
        error_class="model_not_found",
        blame="user_provider",
    )

    assert (
        state["provider_state"]
        == provider_health.PROVIDER_STATE_NEEDS_USER_ACTION
    )
    assert not state.get("last_provider_success_at")


def test_success_immediately_restores_ok():
    state = {
        "provider_state": provider_health.PROVIDER_STATE_NEEDS_USER_ACTION,
        "last_probe_at": 10.0,
        "last_provider_error_class": "quota_insufficient",
        "last_provider_error_blame": "user_provider",
        "user_provider_failure_started_at": 5.0,
    }

    restored = provider_health.evolve_success(state, now=20.0)

    assert restored["provider_state"] == provider_health.PROVIDER_STATE_OK
    assert restored["last_provider_success_at"] == 20.0
    assert restored["last_probe_at"] == 0.0
    assert restored["user_provider_failure_started_at"] == 0.0


# --- latency health ---------------------------------------------------------
#
# The failure fields above catch a provider that BREAKS. They cannot see one
# that merely answers very slowly, because a slow answer is still a success.
# usr_90184 on prod ran a relay averaging 293 s for five days: every fold hit
# compaction's own 60 s timeout, the backlog grew to 1201, and no field in this
# table ever changed to say why. The latency EWMA is that missing signal.


def test_first_success_seeds_the_latency_average():
    out = provider_health.evolve_success({}, now=10.0, latency_ms=4200)
    assert out["recent_latency_ms"] == 4200


def test_latency_average_smooths_instead_of_tracking_the_last_sample():
    state = provider_health.evolve_success({}, now=1.0, latency_ms=1000)
    state = provider_health.evolve_success(state, now=2.0, latency_ms=9000)
    # A single slow answer must move the average without becoming it.
    assert 1000 < state["recent_latency_ms"] < 9000


def test_a_fast_provider_is_not_slow():
    state = {}
    for index in range(8):
        state = provider_health.evolve_success(
            state, now=float(index), latency_ms=8_000
        )
    assert not provider_health.provider_is_slow(state)


def test_a_relay_answering_far_past_the_fold_timeout_is_slow():
    # usr_90184's shape: every answer takes minutes, none of them fail outright.
    state = {}
    for index in range(8):
        state = provider_health.evolve_success(
            state, now=float(index), latency_ms=293_000
        )
    assert provider_health.provider_is_slow(state)
    assert state["recent_latency_ms"] > provider_health.SLOW_PROVIDER_MS


def test_switching_to_a_fast_provider_clears_slow():
    state = {}
    for index in range(8):
        state = provider_health.evolve_success(
            state, now=float(index), latency_ms=293_000
        )
    assert provider_health.provider_is_slow(state)
    # The user moves to a fast route (openrouter, ~11 s on prod).
    for index in range(20):
        state = provider_health.evolve_success(
            state, now=float(100 + index), latency_ms=11_000
        )
    assert not provider_health.provider_is_slow(state)


def test_success_without_a_latency_sample_leaves_the_average_untouched():
    # Callers that cannot measure must not be able to erase the signal.
    state = provider_health.evolve_success({}, now=1.0, latency_ms=250_000)
    carried = provider_health.evolve_success(state, now=2.0)
    assert carried["recent_latency_ms"] == state["recent_latency_ms"]
    assert provider_health.provider_is_slow(carried)


def test_slow_is_independent_of_provider_state():
    # A slow provider is still `ok` for admission purposes — this signal
    # explains latency, it does not gate anything.
    state = provider_health.evolve_success({}, now=1.0, latency_ms=250_000)
    assert state["provider_state"] == provider_health.PROVIDER_STATE_OK
    assert provider_health.provider_is_slow(state)
