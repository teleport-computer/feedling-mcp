"""Durable, cross-worker Runtime V2 perception signal decisions."""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db  # noqa: E402
from conftest import seed_user  # noqa: E402
from perception import signal_state_v2  # noqa: E402
from perception.differ_v2 import PerceptionDifferV2  # noqa: E402
from perception.ingress_v2 import observe_signal_v2  # noqa: E402


def _user_id(label: str) -> str:
    return f"usr_signal_{label}_{uuid.uuid4().hex[:10]}"


def _state_row(user_id: str):
    with db.get_pool().connection() as conn:
        return conn.execute(
            "SELECT value_fingerprint, last_seen_at, last_changed_at, "
            "source_event_id FROM perception_signal_state_v2 "
            "WHERE user_id=%s AND signal='wifi_anchor'",
            (user_id,),
        ).fetchone()


def test_first_anchor_observation_creates_a_non_waking_baseline():
    """A new worker seeing a user's current anchor must not call it an arrival."""
    user_id = _user_id("baseline")
    seed_user(user_id)

    decision = signal_state_v2.observe_signal_state(
        user_id,
        "wifi_anchor",
        {"anchor_id": "wifi-home", "label": "home"},
        observed_at=1_775_203_200.0,
    )

    assert decision.outcome == "baseline_created"
    assert decision.changed is False
    assert decision.error_code == ""
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT signal, last_seen_at, last_changed_at, source_event_id "
            "FROM perception_signal_state_v2 "
            "WHERE user_id=%s AND signal='wifi_anchor'",
            (user_id,),
        ).fetchone()
    assert row is not None
    assert row[0] == "wifi_anchor"
    assert row[1] == row[2]
    assert row[3] is None


def test_repeated_source_event_id_is_a_no_write_duplicate():
    """A retried device event must not mutate state or become a new arrival."""
    user_id = _user_id("duplicate")
    seed_user(user_id)
    signal_state_v2.observe_signal_state(
        user_id,
        "wifi_anchor",
        "wifi-home",
        observed_at=100.0,
        source_event_id="event-1",
    )
    before = _state_row(user_id)

    decision = signal_state_v2.observe_signal_state(
        user_id,
        "wifi_anchor",
        "wifi-work",
        observed_at=200.0,
        source_event_id="event-1",
    )

    assert decision.outcome == "duplicate"
    assert decision.changed is False
    assert _state_row(user_id) == before


def test_older_observation_is_stale_and_cannot_rewind_state():
    """Offline replay arriving late must not overwrite a newer anchor."""
    user_id = _user_id("stale")
    seed_user(user_id)
    signal_state_v2.observe_signal_state(
        user_id, "wifi_anchor", "wifi-home", observed_at=200.0
    )
    before = _state_row(user_id)

    decision = signal_state_v2.observe_signal_state(
        user_id, "wifi_anchor", "wifi-work", observed_at=100.0
    )

    assert decision.outcome == "stale"
    assert decision.changed is False
    assert _state_row(user_id) == before


def test_equal_timestamp_with_different_value_is_a_non_waking_conflict():
    """Lock acquisition order must not choose a user-visible same-time winner."""
    user_id = _user_id("conflict")
    seed_user(user_id)
    signal_state_v2.observe_signal_state(
        user_id, "wifi_anchor", "wifi-home", observed_at=100.0
    )
    before = _state_row(user_id)

    decision = signal_state_v2.observe_signal_state(
        user_id, "wifi_anchor", "wifi-work", observed_at=100.0
    )

    assert decision.outcome == "conflict_same_ts"
    assert decision.changed is False
    assert _state_row(user_id) == before


def test_equal_timestamp_with_same_value_is_unchanged():
    """Equivalent same-time delivery may record its ID but can never become changed."""
    user_id = _user_id("same_time")
    seed_user(user_id)
    signal_state_v2.observe_signal_state(
        user_id,
        "wifi_anchor",
        "wifi-home",
        observed_at=100.0,
        source_event_id="event-1",
    )

    decision = signal_state_v2.observe_signal_state(
        user_id,
        "wifi_anchor",
        "wifi-home",
        observed_at=100.0,
        source_event_id="event-2",
    )

    assert decision.outcome == "unchanged"
    assert decision.changed is False
    assert _state_row(user_id)[3] == "event-2"


def test_later_equal_value_advances_seen_without_advancing_changed():
    """A stable reading updates liveness without pretending the anchor changed."""
    user_id = _user_id("unchanged")
    seed_user(user_id)
    signal_state_v2.observe_signal_state(
        user_id, "wifi_anchor", "wifi-home", observed_at=100.0
    )

    decision = signal_state_v2.observe_signal_state(
        user_id,
        "wifi_anchor",
        "wifi-home",
        observed_at=200.0,
        source_event_id="event-2",
    )

    assert decision.outcome == "unchanged"
    assert decision.changed is False
    assert decision.last_seen_at == datetime.fromtimestamp(200.0, tz=timezone.utc)
    assert decision.last_changed_at == datetime.fromtimestamp(100.0, tz=timezone.utc)
    row = _state_row(user_id)
    assert row[1] == decision.last_seen_at
    assert row[2] == decision.last_changed_at
    assert row[3] == "event-2"


def test_later_different_value_is_the_only_changed_decision():
    """A real ordered transition must emit once and become the new baseline."""
    user_id = _user_id("changed")
    seed_user(user_id)
    signal_state_v2.observe_signal_state(
        user_id, "wifi_anchor", "wifi-home", observed_at=100.0
    )

    changed = signal_state_v2.observe_signal_state(
        user_id, "wifi_anchor", "wifi-work", observed_at=200.0
    )
    repeated = signal_state_v2.observe_signal_state(
        user_id, "wifi_anchor", "wifi-work", observed_at=300.0
    )

    assert changed.outcome == "changed"
    assert changed.changed is True
    assert changed.last_seen_at == datetime.fromtimestamp(200.0, tz=timezone.utc)
    assert changed.last_changed_at == changed.last_seen_at
    assert repeated.outcome == "unchanged"
    assert repeated.changed is False
    assert repeated.last_changed_at == changed.last_changed_at


def test_fingerprint_is_keyed_canonical_and_domain_separated(monkeypatch):
    """Plain/undomained hashes would expose low-entropy home/work anchors."""
    secret = "fixed-perception-test-secret"
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", secret)
    user_id = _user_id("fingerprint")
    other_user_id = _user_id("fingerprint_other")
    seed_user(user_id)
    seed_user(other_user_id)
    value = {"label": "home", "anchor_id": "wifi-home"}
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    expected = hmac.new(
        secret.encode("utf-8"),
        (
            "perception-signal-v2\0"
            + user_id
            + "\0wifi_anchor\0"
            + canonical
        ).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    first = signal_state_v2.observe_signal_state(
        user_id, "wifi_anchor", value, observed_at=100.0
    )
    reordered = signal_state_v2.observe_signal_state(
        user_id,
        "wifi_anchor",
        {"anchor_id": "wifi-home", "label": "home"},
        observed_at=200.0,
    )
    other_user = signal_state_v2.observe_signal_state(
        other_user_id, "wifi_anchor", value, observed_at=100.0
    )
    other_signal = signal_state_v2.observe_signal_state(
        user_id, "connectivity_anchor", value, observed_at=100.0
    )

    assert first.fingerprint == expected
    assert _state_row(user_id)[0] == expected
    assert reordered.outcome == "unchanged"
    assert reordered.fingerprint == expected
    assert other_user.fingerprint != expected
    assert other_signal.fingerprint != expected
    assert "home" not in expected
    assert "wifi-home" not in expected


def test_missing_secret_fails_closed_without_creating_state(monkeypatch):
    """Never downgrade a low-entropy anchor fingerprint to unkeyed SHA-256."""
    monkeypatch.delenv("FEEDLING_RUNTIME_TOKEN_SECRET", raising=False)
    user_id = _user_id("secret")
    seed_user(user_id)

    decision = signal_state_v2.observe_signal_state(
        user_id, "wifi_anchor", "wifi-home", observed_at=100.0
    )

    assert decision.outcome == "error"
    assert decision.changed is False
    assert decision.error_code == "secret_unset"
    assert _state_row(user_id) is None


def test_noncanonical_value_and_nonfinite_time_fail_closed():
    """Invalid ordering/fingerprint inputs must not enter durable state."""
    nan_user = _user_id("nan")
    time_user = _user_id("time")
    seed_user(nan_user)
    seed_user(time_user)

    bad_value = signal_state_v2.observe_signal_state(
        nan_user, "wifi_anchor", {"level": float("nan")}, observed_at=100.0
    )
    bad_time = signal_state_v2.observe_signal_state(
        time_user, "wifi_anchor", "wifi-home", observed_at=float("inf")
    )

    assert bad_value.outcome == "error"
    assert bad_value.changed is False
    assert bad_time.outcome == "error"
    assert bad_time.error_code == "invalid_timestamp"
    assert _state_row(nan_user) is None
    assert _state_row(time_user) is None


def test_database_failure_returns_error_without_memory_fallback(monkeypatch):
    """A broken correctness store must suppress wakes instead of using RAM."""
    user_id = _user_id("db_error")
    seed_user(user_id)
    real_pool = db.get_pool()

    def fail_pool():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(signal_state_v2.db, "get_pool", fail_pool)
    decision = signal_state_v2.observe_signal_state(
        user_id, "wifi_anchor", "wifi-home", observed_at=100.0
    )

    assert decision.outcome == "error"
    assert decision.changed is False
    assert decision.error_code == "storage_error"
    with real_pool.connection() as conn:
        assert conn.execute(
            "SELECT count(*) FROM perception_signal_state_v2 WHERE user_id=%s",
            (user_id,),
        ).fetchone() == (0,)


def test_explicit_first_event_can_opt_in_to_changed():
    """A stable-ID one-shot event must preserve its meaningful first occurrence."""
    user_id = _user_id("first_event")
    seed_user(user_id)

    decision = signal_state_v2.observe_signal_state(
        user_id,
        "unlock_after_absence",
        True,
        observed_at=100.0,
        source_event_id="unlock-1",
        allow_first_event=True,
    )

    assert decision.outcome == "changed"
    assert decision.changed is True


def test_concurrent_first_observers_create_one_baseline_without_error():
    """Two workers racing on an absent row must not turn the loser into a wake/error."""
    user_id = _user_id("concurrent_baseline")
    seed_user(user_id)
    start = threading.Barrier(2)

    def observe():
        start.wait(timeout=5)
        return signal_state_v2.observe_signal_state(
            user_id, "wifi_anchor", "wifi-home", observed_at=100.0
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        decisions = list(executor.map(lambda _index: observe(), range(2)))

    assert sorted(decision.outcome for decision in decisions) == [
        "baseline_created",
        "unchanged",
    ]
    assert all(decision.changed is False for decision in decisions)
    with db.get_pool().connection() as conn:
        assert conn.execute(
            "SELECT count(*) FROM perception_signal_state_v2 WHERE user_id=%s",
            (user_id,),
        ).fetchone() == (1,)


def test_concurrent_real_transition_returns_changed_exactly_once():
    """Repeated concurrent delivery of one move must authorize at most one wake."""
    user_id = _user_id("concurrent_changed")
    seed_user(user_id)
    signal_state_v2.observe_signal_state(
        user_id, "wifi_anchor", "wifi-home", observed_at=100.0
    )
    start = threading.Barrier(2)

    def observe(event_id: str):
        start.wait(timeout=5)
        return signal_state_v2.observe_signal_state(
            user_id,
            "wifi_anchor",
            "wifi-work",
            observed_at=200.0,
            source_event_id=event_id,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        decisions = list(executor.map(observe, ("move-1", "move-2")))

    assert sorted(decision.outcome for decision in decisions) == [
        "changed",
        "unchanged",
    ]
    assert sum(decision.changed for decision in decisions) == 1
    row = _state_row(user_id)
    assert row[1] == datetime.fromtimestamp(200.0, tz=timezone.utc)
    assert row[2] == row[1]


def test_ingress_instances_share_baseline_and_emit_only_the_real_transition():
    """Process-local differ replacement must not alter user-visible wake behavior."""
    user_id = _user_id("ingress_workers")
    seed_user(user_id)
    wakes = []

    first = observe_signal_v2(
        user_id,
        "wifi_anchor",
        "wifi-home",
        ts=100.0,
        differ=PerceptionDifferV2(),
        submit_wake=wakes.append,
    )
    repeated_on_another_worker = observe_signal_v2(
        user_id,
        "wifi_anchor",
        "wifi-home",
        ts=200.0,
        differ=PerceptionDifferV2(),
        submit_wake=wakes.append,
    )
    moved_on_a_third_worker = observe_signal_v2(
        user_id,
        "wifi_anchor",
        "wifi-work",
        ts=300.0,
        differ=PerceptionDifferV2(),
        submit_wake=wakes.append,
    )

    assert first.wake_events == ()
    assert repeated_on_another_worker.wake_events == ()
    assert [event.trigger for event in moved_on_a_third_worker.wake_events] == [
        "arrived_at_anchor"
    ]
    assert len(wakes) == 1


def test_ingress_suppresses_stale_and_equal_time_conflict_wakes():
    """Replay ordering decisions must survive the differ-to-wake adapter."""
    user_id = _user_id("ingress_order")
    seed_user(user_id)
    wakes = []

    baseline = observe_signal_v2(
        user_id,
        "wifi_anchor",
        "wifi-home",
        ts=100.0,
        differ=PerceptionDifferV2(),
        submit_wake=wakes.append,
    )
    stale = observe_signal_v2(
        user_id,
        "wifi_anchor",
        "wifi-work",
        ts=50.0,
        differ=PerceptionDifferV2(),
        submit_wake=wakes.append,
    )
    conflict = observe_signal_v2(
        user_id,
        "wifi_anchor",
        "wifi-work",
        ts=100.0,
        differ=PerceptionDifferV2(),
        submit_wake=wakes.append,
    )
    changed = observe_signal_v2(
        user_id,
        "wifi_anchor",
        "wifi-work",
        ts=200.0,
        differ=PerceptionDifferV2(),
        submit_wake=wakes.append,
    )

    assert baseline.result.changed is False
    assert stale.result.change_digest.startswith("wifi_anchor: stale")
    assert conflict.result.change_digest.startswith(
        "wifi_anchor: conflict_same_ts"
    )
    assert stale.wake_events == ()
    assert conflict.wake_events == ()
    assert len(changed.wake_events) == 1
    assert len(wakes) == 1
