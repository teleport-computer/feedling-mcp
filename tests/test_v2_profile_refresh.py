from datetime import datetime, timezone
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import profile_store, worker


def _seal(_uid, text):
    return {"body_ct": "ct-" + str(len(text)), "nonce": "n"}


def _ok(*, generated_at: str, count: int = 5, updated: str = "u5"):
    return profile_store.build_profile_document(
        "u",
        state="ok",
        source={
            "card_count": count,
            "max_updated_at": updated,
            "generated_at": generated_at,
        },
        last_attempt={
            "at": generated_at,
            "reject_code": "",
            "attempts": 1,
            "retry_not_before": 0,
        },
        memory_text="共同事实",
        user_text="沟通方式",
        seal_text=_seal,
    )


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(worker, "_PROFILE_ENABLED", True)
    monkeypatch.setattr(worker, "_PROFILE_MAX_AGE_SEC", 7 * 24 * 60 * 60)


@pytest.mark.parametrize(
    ("age_days", "stats", "expected"),
    [
        (6, (6, "u6"), False),  # fresh + changed
        (8, (5, "u5"), False),  # stale + unchanged
        (8, (6, "u5"), True),  # stale + count changed
        (8, (5, "u6"), True),  # stale + max(updated_at) changed
    ],
)
def test_stale_floor_four_quadrants(monkeypatch, age_days, stats, expected):
    now = 2_000_000_000.0
    document = _ok(generated_at=_iso(now - age_days * 86400))
    monkeypatch.setattr(worker.db, "get_blob_strict", lambda *_args: document)
    monkeypatch.setattr(
        worker.db,
        "memory_profile_source_stats",
        lambda _uid: stats,
    )

    assert worker._profile_refresh_due("u", now=now) is expected


def test_degraded_respects_retry_not_before(monkeypatch):
    now = 2_000_000_000.0
    document = profile_store.build_profile_document(
        "u",
        state="degraded",
        source={"card_count": 5, "max_updated_at": "u5", "generated_at": ""},
        last_attempt={
            "at": _iso(now),
            "reject_code": "provider_unavailable",
            "attempts": 2,
            "retry_not_before": now + 60,
        },
        seal_text=_seal,
    )
    monkeypatch.setattr(worker.db, "get_blob_strict", lambda *_args: document)

    assert worker._profile_refresh_due("u", now=now) is False
    assert worker._profile_refresh_due("u", now=now + 61) is True


def _failed_profile(
    *,
    disposition: str,
    family: str,
    retry_not_before: float = 0,
    count: int = 5,
    updated: str = "u5",
):
    return profile_store.build_profile_document(
        "u",
        state="pending",
        source={"card_count": count, "max_updated_at": updated, "generated_at": ""},
        last_attempt={
            "at": _iso(1),
            "reject_code": "reply_not_json",
            "attempts": 1,
            "retry_disposition": disposition,
            "retry_family": family,
            "retry_attempts": 1,
            "retry_not_before": retry_not_before,
        },
        seal_text=_seal,
    )


def test_scheduled_profile_refresh_respects_persisted_retry_time(monkeypatch):
    document = _failed_profile(
        disposition="scheduled",
        family="shape",
        retry_not_before=1060,
    )
    monkeypatch.setattr(worker.db, "get_blob_strict", lambda *_args: document)

    assert worker._profile_refresh_due("u", now=1000) is False
    assert worker._profile_refresh_due("u", now=1061) is True


@pytest.mark.parametrize("disposition", ["provider_config", "terminal"])
def test_non_timed_profile_failure_waits_for_explicit_repair(
    monkeypatch, disposition
):
    document = _failed_profile(
        disposition=disposition,
        family="provider_config" if disposition == "provider_config" else "terminal",
    )
    monkeypatch.setattr(worker.db, "get_blob_strict", lambda *_args: document)
    monkeypatch.setattr(
        worker.db,
        "memory_profile_source_stats",
        lambda _uid: pytest.fail("non-timed retry must not query Garden"),
    )

    assert worker._profile_refresh_due("u", now=2_000_000_000) is False


def test_source_change_retry_only_requeues_after_garden_witness_changes(monkeypatch):
    document = _failed_profile(disposition="source_change", family="source")
    monkeypatch.setattr(worker.db, "get_blob_strict", lambda *_args: document)
    monkeypatch.setattr(
        worker.db,
        "memory_profile_source_stats",
        lambda _uid: (5, "u5"),
    )
    assert worker._profile_refresh_due("u", now=2_000_000_000) is False

    monkeypatch.setattr(
        worker.db,
        "memory_profile_source_stats",
        lambda _uid: (6, "u6"),
    )
    assert worker._profile_refresh_due("u", now=2_000_000_000) is True


def test_stale_floor_is_independent_of_dream_setting(monkeypatch):
    now = 2_000_000_000.0
    document = _ok(generated_at=_iso(now - 8 * 86400))
    monkeypatch.setattr(worker.db, "get_blob_strict", lambda *_args: document)
    monkeypatch.setattr(
        worker.db,
        "memory_profile_source_stats",
        lambda _uid: (6, "u6"),
    )
    # No dream-enabled callback or setting participates in this decision.
    assert worker._profile_refresh_due("u", now=now) is True


def test_empty_profile_only_requeues_when_garden_changes(monkeypatch):
    document = profile_store.build_profile_document(
        "u",
        state="empty",
        source={"card_count": 0, "max_updated_at": "", "generated_at": _iso(1)},
        last_attempt={
            "at": _iso(1),
            "reject_code": "",
            "attempts": 1,
            "retry_not_before": 0,
        },
        seal_text=_seal,
    )
    monkeypatch.setattr(worker.db, "get_blob_strict", lambda *_args: document)
    monkeypatch.setattr(
        worker.db,
        "memory_profile_source_stats",
        lambda _uid: (0, ""),
    )
    assert worker._profile_refresh_due("u", now=2) is False
    monkeypatch.setattr(
        worker.db,
        "memory_profile_source_stats",
        lambda _uid: (1, "u1"),
    )
    assert worker._profile_refresh_due("u", now=2) is True


def test_empty_profile_with_only_ineligible_rows_does_not_loop(monkeypatch):
    document = profile_store.build_profile_document(
        "u",
        state="empty",
        source={"card_count": 4, "max_updated_at": "u4", "generated_at": _iso(1)},
        last_attempt={
            "at": _iso(1),
            "reject_code": "",
            "attempts": 1,
            "retry_not_before": 0,
        },
        seal_text=_seal,
    )
    monkeypatch.setattr(worker.db, "get_blob_strict", lambda *_args: document)
    monkeypatch.setattr(
        worker.db,
        "memory_profile_source_stats",
        lambda _uid: (4, "u4"),
    )

    assert worker._profile_refresh_due("u", now=2) is False


def test_missing_profile_requeues_without_garden_read(monkeypatch):
    monkeypatch.setattr(worker.db, "get_blob_strict", lambda *_args: None)
    monkeypatch.setattr(
        worker.db,
        "memory_profile_source_stats",
        lambda _uid: pytest.fail("missing profile needs no freshness query"),
    )
    assert worker._profile_refresh_due("u", now=2) is True


def test_retry_backoff_starts_at_five_minutes_and_caps_at_six_hours(monkeypatch):
    monkeypatch.setattr(worker, "_PROFILE_RETRY_BASE_SEC", 300.0)
    monkeypatch.setattr(worker, "_PROFILE_RETRY_CAP_SEC", 21600.0)

    assert worker._profile_retry_not_before(1, now=1000.0) == 1300.0
    assert worker._profile_retry_not_before(2, now=1000.0) == 1600.0
    assert worker._profile_retry_not_before(99, now=1000.0) == 22600.0


def test_post_chat_missing_profile_enqueues_but_healthy_unchanged_does_not(monkeypatch):
    calls = []
    monkeypatch.setattr(worker, "_PROFILE_ENABLED", True)
    monkeypatch.setattr(worker, "_profile_refresh_due", lambda _uid: True)
    monkeypatch.setattr(
        worker.jobs_store,
        "enqueue_job",
        lambda uid, lane, **kwargs: calls.append((uid, lane, kwargs["reason"]))
        or (9, False),
    )
    monkeypatch.setattr(worker.core_wake_bus, "notify", lambda *_args: None)

    assert asyncio.run(
        worker._enqueue_profile_if_due("u", reason="post_turn_refresh")
    ) is True
    assert calls == [("u", "profile", "post_turn_refresh")]

    monkeypatch.setattr(worker, "_profile_refresh_due", lambda _uid: False)
    assert asyncio.run(
        worker._enqueue_profile_if_due("u", reason="post_turn_refresh")
    ) is False
    assert calls == [("u", "profile", "post_turn_refresh")]


def test_dream_force_refresh_bypasses_healthy_profile_due_check(monkeypatch):
    calls = []
    monkeypatch.setattr(worker, "_PROFILE_ENABLED", True)
    monkeypatch.setattr(
        worker,
        "_profile_refresh_due",
        lambda _uid: pytest.fail("force refresh must bypass due check"),
    )
    monkeypatch.setattr(
        worker.jobs_store,
        "enqueue_job",
        lambda uid, lane, **kwargs: calls.append((uid, lane, kwargs["reason"]))
        or (10, False),
    )
    monkeypatch.setattr(worker.core_wake_bus, "notify", lambda *_args: None)

    assert asyncio.run(
        worker._enqueue_profile_if_due("u", reason="dream_refresh", force=True)
    ) is True
    assert calls == [("u", "profile", "dream_refresh")]
