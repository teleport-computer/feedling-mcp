from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import jobs_store
from model_api_runtime.v2 import profile_store


def _seal(_uid: str, text: str) -> dict:
    return {"body_ct": "ct:" + text}


def _source(*, count=2, updated="2026-07-01T00:00:00Z", generated="") -> dict:
    return {
        "card_count": count,
        "max_updated_at": updated,
        "generated_at": generated,
    }


def _attempt(*, retry=0) -> dict:
    return {
        "at": "",
        "reject_code": "",
        "attempts": 0,
        "retry_not_before": retry,
    }


def _ok(*, generated: str) -> dict:
    return profile_store.build_profile_document(
        "u-refresh",
        state="ok",
        source=_source(generated=generated),
        last_attempt=_attempt(),
        memory_text="memory",
        user_text="user",
        seal_text=_seal,
    )


def test_profile_lane_is_registered_at_background_priority():
    assert "profile" in jobs_store.LANES
    assert jobs_store.LANE_PRIORITY["profile"] == jobs_store.LANE_PRIORITY["dream"]


def test_healthy_profile_requires_age_floor_and_garden_change():
    now = 2_000_000.0
    fresh_generated = "1970-01-24T03:31:40Z"  # now - 100 seconds
    stale_generated = "1970-01-15T00:00:00Z"

    assert not profile_store.profile_refresh_due(
        "u-refresh",
        now=now,
        max_age_sec=604800,
        read_blob=lambda *_args: _ok(generated=fresh_generated),
        read_source=lambda _uid: _source(count=3),
    )
    assert not profile_store.profile_refresh_due(
        "u-refresh",
        now=now,
        max_age_sec=604800,
        read_blob=lambda *_args: _ok(generated=stale_generated),
        read_source=lambda _uid: _source(),
    )
    assert profile_store.profile_refresh_due(
        "u-refresh",
        now=now,
        max_age_sec=604800,
        read_blob=lambda *_args: _ok(generated=stale_generated),
        read_source=lambda _uid: _source(count=3),
    )
    assert profile_store.profile_refresh_due(
        "u-refresh",
        now=now,
        max_age_sec=604800,
        read_blob=lambda *_args: _ok(generated=stale_generated),
        read_source=lambda _uid: _source(updated="2026-07-02T00:00:00Z"),
    )


def test_failed_profile_respects_retry_not_before():
    document = profile_store.build_profile_document(
        "u-refresh",
        state="pending",
        source=_source(count=0, updated="", generated=""),
        last_attempt=_attempt(retry=500),
        seal_text=_seal,
    )
    assert not profile_store.profile_refresh_due(
        "u-refresh",
        now=499,
        read_blob=lambda *_args: document,
        read_source=lambda _uid: (_ for _ in ()).throw(
            AssertionError("backoff path must not inspect Garden")
        ),
    )
    assert profile_store.profile_refresh_due(
        "u-refresh",
        now=500,
        read_blob=lambda *_args: document,
        read_source=lambda _uid: {},
    )


def test_empty_profile_only_refreshes_when_garden_changes():
    document = profile_store.build_profile_document(
        "u-refresh",
        state="empty",
        source=_source(count=0, updated="", generated="2026-07-01T00:00:00Z"),
        last_attempt=_attempt(),
        seal_text=_seal,
    )
    assert not profile_store.profile_refresh_due(
        "u-refresh",
        read_blob=lambda *_args: document,
        read_source=lambda _uid: {"card_count": 0, "max_updated_at": ""},
    )
    assert profile_store.profile_refresh_due(
        "u-refresh",
        read_blob=lambda *_args: document,
        read_source=lambda _uid: {
            "card_count": 1,
            "max_updated_at": "2026-07-31T00:00:00Z",
        },
    )


def test_disabled_profile_never_auto_enqueues():
    document = profile_store.build_profile_document(
        "u-refresh",
        state="empty",
        source=_source(count=0, updated="", generated=""),
        last_attempt=_attempt(),
        disabled=True,
        seal_text=_seal,
    )
    assert not profile_store.profile_refresh_due(
        "u-refresh",
        read_blob=lambda *_args: document,
        read_source=lambda _uid: (_ for _ in ()).throw(
            AssertionError("disabled profile must not inspect Garden")
        ),
    )
