"""Backend contract for resident decrypt-health self-reporting and rollout."""

from __future__ import annotations

import threading
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from chat import consumer


class _Store:
    user_id = "usr_decrypt_health"

    def __init__(self, *, first_chat_ok_at: str = ""):
        self.consumer_state_lock = threading.Lock()
        self._first_chat_ok_at = first_chat_ok_at

    def first_chat_ok_at(self) -> str:
        return self._first_chat_ok_at


def _validation(monkeypatch, state: dict, *, now: float = 1_000.0) -> dict:
    store = _Store()
    monkeypatch.setattr(consumer, "_load_consumer_state", lambda _store: dict(state))
    return consumer._consumer_validation_state(store, now_epoch=now)


def test_consumer_headers_parse_decrypt_health_and_clear_missing_values():
    info = consumer._consumer_headers_from_map(
        {
            "X-Feedling-Consumer": "feedling-chat-resident",
            "X-Feedling-Decrypt-Status": " DeGrAdEd ",
            "X-Feedling-Decrypt-Checked-At": "123.5",
        }
    )
    assert info["decrypt_status"] == "degraded"
    assert info["decrypt_checked_at_epoch"] == "123.5"

    old_consumer = consumer._consumer_headers_from_map(
        {"X-Feedling-Consumer": "feedling-chat-resident"}
    )
    assert old_consumer["decrypt_status"] == ""
    assert old_consumer["decrypt_checked_at_epoch"] == ""


def test_fresh_ok_decrypt_health_passes_independently_of_poll_freshness(monkeypatch):
    validation = _validation(
        monkeypatch,
        {
            "official": True,
            "last_poll_epoch": 999.0,
            "decrypt_status": "ok",
            "decrypt_checked_at_epoch": "995.0",
        },
    )
    assert validation["passing"] is True
    assert validation["decrypt_health"] == {
        "passing": True,
        "status": "ok",
        "reported_status": "ok",
        "checked_at_epoch": 995.0,
        "age_sec": 5.0,
        "fresh_window_sec": consumer._DECRYPT_HEALTH_RECENT_SEC,
        "reported": True,
        "fresh": True,
        "reason": "",
        "required": "",
        "unknown_since_epoch": 0.0,
    }


def test_stale_ok_and_invalid_reports_normalize_to_unknown(monkeypatch):
    stale = _validation(
        monkeypatch,
        {
            "official": True,
            "last_poll_epoch": 999.0,
            "decrypt_status": "ok",
            "decrypt_checked_at_epoch": str(
                1_000.0 - consumer._DECRYPT_HEALTH_RECENT_SEC - 1
            ),
        },
    )["decrypt_health"]
    assert stale["status"] == "unknown"
    assert stale["reported_status"] == "ok"
    assert stale["reason"] == "decrypt_health_stale"
    assert stale["passing"] is False

    invalid = _validation(
        monkeypatch,
        {
            "official": True,
            "last_poll_epoch": 999.0,
            "decrypt_status": "healthy",
            "decrypt_checked_at_epoch": "999",
        },
    )["decrypt_health"]
    assert invalid["status"] == "unknown"
    assert invalid["reported_status"] == ""
    assert invalid["reason"] == "decrypt_health_unknown"


def test_poll_records_unknown_since_and_clears_it_after_valid_report(monkeypatch):
    store = _Store()
    state: dict = {}
    saved: list[dict] = []
    monkeypatch.setattr(consumer.time, "time", lambda: 1_000.0)
    monkeypatch.setattr(consumer, "_load_consumer_state", lambda _store: dict(state))

    def _save(_store, value):
        state.clear()
        state.update(value)
        saved.append(dict(value))

    monkeypatch.setattr(consumer, "_save_consumer_state", _save)
    monkeypatch.setattr(consumer, "_touch_resident_binding_seen", lambda *a, **k: True)

    consumer._record_consumer_event(
        store,
        "poll",
        info={
            "official": True,
            "decrypt_status": "",
            "decrypt_checked_at_epoch": "",
        },
    )
    assert saved[-1]["decrypt_health_unknown_since_epoch"] == 1_000.0

    monkeypatch.setattr(consumer.time, "time", lambda: 1_010.0)
    consumer._record_consumer_event(
        store,
        "poll",
        info={
            "official": True,
            "decrypt_status": "ok",
            "decrypt_checked_at_epoch": "1010",
        },
    )
    assert "decrypt_health_unknown_since_epoch" not in saved[-1]


def test_response_event_preserves_latest_poll_decrypt_health(monkeypatch):
    store = _Store()
    state = {
        "official": True,
        "decrypt_status": "ok",
        "decrypt_checked_at_epoch": "995",
    }
    monkeypatch.setattr(consumer.time, "time", lambda: 1_000.0)
    monkeypatch.setattr(consumer, "_load_consumer_state", lambda _store: dict(state))

    def _save(_store, value):
        state.clear()
        state.update(value)

    monkeypatch.setattr(consumer, "_save_consumer_state", _save)

    consumer._record_consumer_event(
        store,
        "response",
        info={
            "official": True,
            # chat_resident_consumer posts static identity headers on replies;
            # missing poll-only health fields parse as these empty values.
            "decrypt_status": "",
            "decrypt_checked_at_epoch": "",
        },
    )

    assert state["decrypt_status"] == "ok"
    assert state["decrypt_checked_at_epoch"] == "995"
    assert state["last_event"] == "response"


def test_rollout_blocks_new_unknown_but_graces_established_unknown(monkeypatch):
    monkeypatch.setattr(
        consumer, "_DECRYPT_HEALTH_EXISTING_UNKNOWN_GRACE_SEC", 100
    )
    unknown = {
        "decrypt_health": {
            "passing": False,
            "status": "unknown",
            "reason": "decrypt_health_unknown",
            "unknown_since_epoch": 950.0,
        }
    }

    new_policy = consumer._decrypt_health_enforcement_state(
        _Store(), unknown, now_epoch=1_000.0
    )
    assert new_policy["mode"] == "new_onboarding_blocked"
    assert new_policy["blocks_onboarding"] is True
    assert new_policy["blocks_verify"] is True
    assert new_policy["blocks_chat"] is True

    established_policy = consumer._decrypt_health_enforcement_state(
        _Store(first_chat_ok_at="2026-07-20T00:00:00"),
        unknown,
        now_epoch=1_000.0,
    )
    assert established_policy["mode"] == "established_unknown_grace"
    assert established_policy["warning_only"] is True
    assert established_policy["blocks_onboarding"] is False
    assert established_policy["blocks_verify"] is True
    assert established_policy["blocks_chat"] is False
    assert established_policy["grace_remaining_sec"] == 50


def test_rollout_expires_unknown_grace_and_never_graces_explicit_failure(monkeypatch):
    monkeypatch.setattr(
        consumer, "_DECRYPT_HEALTH_EXISTING_UNKNOWN_GRACE_SEC", 100
    )
    store = _Store(first_chat_ok_at="2026-07-20T00:00:00")
    expired = consumer._decrypt_health_enforcement_state(
        store,
        {
            "decrypt_health": {
                "passing": False,
                "status": "unknown",
                "reason": "decrypt_health_stale",
                "unknown_since_epoch": 800.0,
            }
        },
        now_epoch=1_000.0,
    )
    assert expired["mode"] == "established_unknown_expired"
    assert expired["blocks_onboarding"] is True
    assert expired["blocks_verify"] is True
    assert expired["blocks_chat"] is False

    degraded = consumer._decrypt_health_enforcement_state(
        store,
        {
            "decrypt_health": {
                "passing": False,
                "status": "degraded",
                "reason": "decrypt_source_degraded",
                "unknown_since_epoch": 0.0,
            }
        },
        now_epoch=1_000.0,
    )
    assert degraded["mode"] == "established_explicit_failure"
    assert degraded["grace_active"] is False
    assert degraded["blocks_onboarding"] is True
    assert degraded["blocks_verify"] is True
    assert degraded["blocks_chat"] is False
