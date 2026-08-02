from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from proactive import dream_scheduler, proactive_core  # noqa: E402


class _Store:
    user_id = "usr_dream_status"

    def __init__(self, jobs=None):
        self._jobs = list(jobs or [])

    def list_proactive_jobs(self, since_epoch=0, limit=0):
        return list(self._jobs)


def _install_dream_blob(monkeypatch, initial=None, capture_initial=None):
    blobs = {
        dream_scheduler.DREAM_STATE_KIND: dict(initial or {}),
        "capture_state": dict(capture_initial or {}),
    }

    def fake_get_blob(user_id, kind):
        assert user_id == _Store.user_id
        return dict(blobs.get(kind) or {})

    def fake_set_blob(user_id, kind, doc):
        assert user_id == _Store.user_id
        blobs.setdefault(kind, {}).clear()
        blobs[kind].update(doc)

    monkeypatch.setattr(dream_scheduler.db, "get_blob", fake_get_blob)
    monkeypatch.setattr(dream_scheduler.db, "set_blob", fake_set_blob)
    return blobs[dream_scheduler.DREAM_STATE_KIND]


def test_dream_completed_records_organized_and_merged_counts(monkeypatch):
    blob = _install_dream_blob(monkeypatch, {"pending_dream_key": "dream:1"})
    store = _Store()
    job = {
        "job_kind": "memory_dream",
        "source": "memory_dream",
        "status": "completed",
        "dream_key": "dream:1",
        "dream_stats": {"card_count": 8, "turn_count": 22, "signature": "sig1"},
        "dream_until": {"last_until": "2030-06-02T00:00:00Z"},
        "organized_count": 4,
        "merged_count": 2,
    }

    state = dream_scheduler.record_dream_job_status(store, job, status="completed", now=1234.0)

    assert state["last_dream_completed_at"] == 1234.0
    assert state["last_dream_organized_count"] == 4
    assert state["last_dream_merged_count"] == 2
    assert state["pending_dream_key"] == ""
    assert blob["last_dream_organized_count"] == 4

    assert proactive_core.dream_status(store) == {
        "dreaming": False,
        "last_completed_at": 1234.0,
        "organized_count": 4,
        "merged_count": 2,
        "capture_completed_at": 0,
        "capture_cards_added": 0,
    }


def test_dream_completed_without_consolidation_records_zero_counts(monkeypatch):
    _install_dream_blob(monkeypatch, {})
    store = _Store()
    job = {
        "job_kind": "memory_dream",
        "source": "memory_dream",
        "status": "completed",
        "dream_result": {"status": "noop", "reason": "dream_nothing_to_consolidate"},
    }

    state = dream_scheduler.record_dream_job_status(store, job, status="completed", now=2000.0)

    assert state["last_dream_completed_at"] == 2000.0
    assert state["last_dream_organized_count"] == 0
    assert state["last_dream_merged_count"] == 0
    assert proactive_core.dream_status(store)["organized_count"] == 0
    assert proactive_core.dream_status(store)["merged_count"] == 0


def test_dream_status_reports_active_dreaming(monkeypatch):
    _install_dream_blob(monkeypatch, {
        "last_dream_completed_at": 3000.0,
        "last_dream_organized_count": 7,
        "last_dream_merged_count": 3,
    })
    store = _Store(jobs=[{
        "job_id": "dream_active",
        "job_kind": "memory_dream",
        "source": "memory_dream",
        "status": "realizing",
    }])

    status = proactive_core.dream_status(store)

    assert status["dreaming"] is True
    assert status["last_completed_at"] == 3000.0
    assert status["organized_count"] == 7
    assert status["merged_count"] == 3
    assert status["capture_completed_at"] == 0
    assert status["capture_cards_added"] == 0


def test_dream_status_includes_daily_capture_banner_fields(monkeypatch):
    _install_dream_blob(
        monkeypatch,
        {},
        capture_initial={
            "last_capture_cards_added_at": 4321.0,
            "last_capture_cards_added": 6,
        },
    )

    status = proactive_core.dream_status(_Store())

    assert status["capture_completed_at"] == 4321.0
    assert status["capture_cards_added"] == 6
