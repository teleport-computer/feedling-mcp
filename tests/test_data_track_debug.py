from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from accounts import registry  # noqa: E402
from admin import admin_core, data_track  # noqa: E402
from core.reqctx import bind  # noqa: E402


class _Store:
    def __init__(self, user_id: str):
        self.user_id = user_id


def _event(ts: float, user_id: str, typ: str, *, trace_id: str, status: str = "ok", **extra) -> dict:
    return {
        "ts": ts,
        "user_id": user_id,
        "subsystem": typ.split(".", 1)[0],
        "type": typ,
        "actor": "backend",
        "status": status,
        "summary": extra.get("summary", ""),
        "explain": extra.get("explain", typ),
        "trace_id": trace_id,
        "turn_id": trace_id,
        "job_id": "",
        "dur_ms": extra.get("dur_ms"),
        "detail": extra.get("detail", {}),
        "content_excerpt": extra.get("content_excerpt", {}),
    }


def test_genesis_stats_surfaces_state_and_recent_jobs(monkeypatch):
    state = {
        "status": "processing",
        "job_status": "processing",
        "job_id": "gen_123",
        "updated_at": "2026-07-05T12:00:00Z",
        "memory_action_count": 0,
        "identity_status": "",
        "error": "",
    }
    jobs = [
        {
            "job_id": "gen_123",
            "status": "processing",
            "source_kind": "plaintext_multi_source",
            "total_chunks": 3,
            "processed_chunks": 1,
            "total_bytes": 2048,
            "memory_action_count": 0,
            "identity_status": "",
            "error": "",
            "updated_at": "2026-07-05T12:00:00Z",
            "metadata": {"mode": "onboarding", "history_count": 42, "window_count": 3},
            "output": {"stage": "plaintext_queued"},
        },
        {
            "job_id": "gen_old",
            "status": "failed",
            "source_kind": "history_import",
            "error": "timeout",
            "updated_at": "2026-07-05T11:00:00Z",
            "metadata": {"mode": "onboarding"},
        },
    ]
    monkeypatch.setattr(data_track.db, "get_blob", lambda _uid, kind: state if kind == "genesis_state" else None)
    monkeypatch.setattr(data_track.db, "genesis_list_jobs", lambda _uid, limit=5: jobs[:limit])

    stats = data_track._genesis_stats(_Store("user_a"), include_jobs=True)

    assert stats["has_state"] is True
    assert stats["status"] == "processing"
    assert stats["job_count"] == 2
    assert stats["latest_job"]["job_id"] == "gen_123"
    assert stats["latest_job"]["metadata"]["history_count"] == 42


def test_debug_payload_groups_multi_user_trace_and_marks_stalled(monkeypatch):
    with registry._users_lock:
        registry._users[:] = [
            {"user_id": "user_a", "principal_id": "p_a", "created_at": "2026-07-04T00:00:00Z"},
            {"user_id": "user_b", "principal_id": "p_b", "created_at": "2026-07-04T00:00:00Z"},
        ]

    blobs = {
        ("user_a", "v1_flow_trace_enabled"): {"enabled": True},
        ("user_b", "v1_flow_trace_enabled"): {"enabled": True},
        ("user_a", "v1_flow_trace"): {
            "events": [
                _event(100, "user_a", "route.chat.message", trace_id="t-ok"),
                _event(101, "user_a", "agent.model.call.start", trace_id="t-ok"),
                _event(
                    104,
                    "user_a",
                    "agent.model.call.done",
                    trace_id="t-ok",
                    dur_ms=3000,
                    detail={"input_tokens": 12, "output_tokens": 34},
                    content_excerpt={"reply": "hello from model"},
                ),
            ]
        },
        ("user_b", "v1_flow_trace"): {
            "events": [
                _event(200, "user_b", "route.chat.message", trace_id="t-stall"),
                _event(
                    201,
                    "user_b",
                    "agent.model.call.start",
                    trace_id="t-stall",
                    content_excerpt={"prompt_head": "private beta prompt"},
                ),
            ]
        },
    }

    monkeypatch.setattr(data_track.db, "get_blob", lambda uid, kind: blobs.get((uid, kind)))

    with bind("view=debug"):
        payload = data_track._data_track_debug_payload()

    assert payload["summary"]["users_with_events"] == 2
    assert payload["summary"]["events_total"] == 5
    assert [u["user_id"] for u in payload["users"]] == ["user_b", "user_a"]

    turns = {turn["trace_id"]: turn for turn in payload["turns"]}
    assert turns["t-stall"]["terminal_status"] == "stalled"
    assert turns["t-stall"]["is_stalled"] is True
    assert turns["t-ok"]["terminal_status"] == "ok"
    assert turns["t-ok"]["total_dur_ms"] == 3000


def test_debug_payload_treats_missing_trace_flag_as_enabled_by_default(monkeypatch):
    with registry._users_lock:
        registry._users[:] = [
            {"user_id": "user_a", "principal_id": "p_a", "created_at": "2026-07-04T00:00:00Z"},
        ]

    blobs = {}
    monkeypatch.delenv("FEEDLING_V1_FLOW_TRACE", raising=False)
    monkeypatch.delenv("FEEDLING_V1_FLOW_TRACE_DEFAULT", raising=False)
    monkeypatch.setattr(data_track.db, "get_blob", lambda uid, kind: blobs.get((uid, kind)))

    with bind("view=debug"):
        payload = data_track._data_track_debug_payload()

    assert payload["users"] == [
        {
            "user_id": "user_a",
            "principal_id": "p_a",
            "enabled": True,
            "events": 0,
            "last_ts": 0,
            "last_at": "",
        }
    ]


def test_debug_payload_paginates_filtered_events(monkeypatch):
    with registry._users_lock:
        registry._users[:] = [
            {"user_id": "user_a", "principal_id": "p_a", "created_at": "2026-07-04T00:00:00Z"},
        ]

    events = [
        _event(100 + idx, "user_a", "agent.reply", trace_id=f"t-{idx}", explain=f"event {idx}")
        for idx in range(5)
    ]
    blobs = {
        ("user_a", "v1_flow_trace_enabled"): {"enabled": True},
        ("user_a", "v1_flow_trace"): {"events": events},
    }
    monkeypatch.setattr(data_track.db, "get_blob", lambda uid, kind: blobs.get((uid, kind)))

    with bind("view=debug&limit=2&offset=1"):
        payload = data_track._data_track_debug_payload()

    assert [event["trace_id"] for event in payload["events"]] == ["t-3", "t-2"]
    assert payload["pagination"] == {
        "limit": 2,
        "offset": 1,
        "total": 5,
        "returned": 2,
        "next_offset": 3,
        "prev_offset": 0,
        "current_page": 1,
        "total_pages": 3,
    }
    assert payload["summary"]["events_total"] == 5
    assert payload["summary"]["events_returned"] == 2


def test_debug_page_renders_nav_filters_and_redacts_plaintext_by_default(monkeypatch):
    with registry._users_lock:
        registry._users[:] = [{"user_id": "user_a", "principal_id": "p_a"}]

    blobs = {
        ("user_a", "v1_flow_trace_enabled"): {"enabled": True},
        ("user_a", "v1_flow_trace"): {
            "events": [
                _event(
                    100,
                    "user_a",
                    "agent.reply",
                    trace_id="t-reply",
                    content_excerpt={"reply": "visible beta reply"},
                )
            ]
        },
    }
    monkeypatch.setattr(data_track.db, "get_blob", lambda uid, kind: blobs.get((uid, kind)))

    html = admin_core.page_html("view=debug&mode=flat&user_id=user_a&q=reply")

    assert "Debug" in html
    assert "user_a" in html
    assert "agent.reply" in html
    assert "visible beta reply" not in html
    assert "&quot;chars&quot;: 18" in html
    assert "Reveal plaintext" in html
    assert "copy trace" in html
    assert "Filter debug logs" in html
    assert "Flat logs" in html
    assert "Timeline" in html
    assert "<select name=\"subsystem\">" in html
    assert "trace_id 时可直接定位" in html
    assert "#event-" in html
    assert "#turn-" in html


def test_debug_page_renders_load_more_when_paginated(monkeypatch):
    with registry._users_lock:
        registry._users[:] = [{"user_id": "user_a", "principal_id": "p_a"}]

    blobs = {
        ("user_a", "v1_flow_trace_enabled"): {"enabled": True},
        ("user_a", "v1_flow_trace"): {
            "events": [
                _event(100 + idx, "user_a", "agent.reply", trace_id=f"t-{idx}")
                for idx in range(3)
            ]
        },
    }
    monkeypatch.setattr(data_track.db, "get_blob", lambda uid, kind: blobs.get((uid, kind)))

    html = admin_core.page_html("view=debug&mode=flat&user_id=user_a&limit=2&offset=0")

    assert "Showing 1-2 of 3 events" in html
    assert "Next" in html
    assert "offset=2" in html
    assert '<select name="limit">' in html


def test_debug_page_renders_numbered_pagination_controls(monkeypatch):
    with registry._users_lock:
        registry._users[:] = [{"user_id": "user_a", "principal_id": "p_a"}]

    blobs = {
        ("user_a", "v1_flow_trace_enabled"): {"enabled": True},
        ("user_a", "v1_flow_trace"): {
            "events": [
                _event(100 + idx, "user_a", "agent.reply", trace_id=f"t-{idx}")
                for idx in range(25)
            ]
        },
    }
    monkeypatch.setattr(data_track.db, "get_blob", lambda uid, kind: blobs.get((uid, kind)))

    html = admin_core.page_html("view=debug&user_id=user_a&limit=10&offset=10")

    assert "Page" in html
    assert "name='page'" in html
    assert "value='2'" in html
    assert "/ 3" in html
    assert "First" in html
    assert "Prev" in html
    assert "Next" in html
    assert "Last" in html
    assert "offset=0" in html
    assert "offset=20" in html


def test_debug_reveal_and_timeline_links_reset_pagination(monkeypatch):
    with registry._users_lock:
        registry._users[:] = [{"user_id": "user_a", "principal_id": "p_a"}]

    events = [
        _event(100 + idx, "user_a", "agent.reply", trace_id=f"t-{idx}")
        for idx in range(3)
    ]
    target = events[1]
    blobs = {
        ("user_a", "v1_flow_trace_enabled"): {"enabled": True},
        ("user_a", "v1_flow_trace"): {"events": events},
    }
    monkeypatch.setattr(data_track.db, "get_blob", lambda uid, kind: blobs.get((uid, kind)))

    key = data_track._debug_event_key(target)
    html = admin_core.page_html("view=debug&user_id=user_a&limit=1&offset=1")

    assert f"reveal={key}" in html
    assert f"reveal={key}#event-{key}" in html
    assert f"offset=1&amp;reveal={key}" not in html
    assert "mode=timeline" in html
    assert "offset=0&amp;view=debug&amp;user_id=user_a&amp;mode=timeline&amp;trace_id=t-1" in html


def test_debug_timeline_event_rows_have_event_anchors(monkeypatch):
    with registry._users_lock:
        registry._users[:] = [{"user_id": "user_a", "principal_id": "p_a"}]

    event = _event(100, "user_a", "agent.reply", trace_id="t-reply")
    blobs = {
        ("user_a", "v1_flow_trace_enabled"): {"enabled": True},
        ("user_a", "v1_flow_trace"): {"events": [event]},
    }
    monkeypatch.setattr(data_track.db, "get_blob", lambda uid, kind: blobs.get((uid, kind)))

    key = data_track._debug_event_key(event)
    html = admin_core.page_html("view=debug&mode=timeline&user_id=user_a")

    assert f'id="event-{key}"' in html or f"id='event-{key}'" in html


def test_debug_page_reveals_plaintext_for_one_event(monkeypatch):
    with registry._users_lock:
        registry._users[:] = [{"user_id": "user_a", "principal_id": "p_a"}]

    event = _event(
        100,
        "user_a",
        "agent.reply",
        trace_id="t-reply",
        detail={"model": "deepseek", "prompt": "private prompt"},
        content_excerpt={"reply": "visible beta reply"},
    )
    blobs = {
        ("user_a", "v1_flow_trace_enabled"): {"enabled": True},
        ("user_a", "v1_flow_trace"): {"events": [event]},
    }
    monkeypatch.setattr(data_track.db, "get_blob", lambda uid, kind: blobs.get((uid, kind)))

    key = data_track._debug_event_key(event)
    html = admin_core.page_html(f"view=debug&user_id=user_a&trace_id=t-reply&reveal={key}")

    assert "visible beta reply" in html
    assert "private prompt" in html
    assert "Plaintext revealed for this event only" in html
    assert 'http-equiv="refresh"' not in html
    assert f'id="event-{key}"' in html or f"id='event-{key}'" in html


def test_debug_page_can_render_timeline_mode(monkeypatch):
    with registry._users_lock:
        registry._users[:] = [{"user_id": "user_a", "principal_id": "p_a"}]

    blobs = {
        ("user_a", "v1_flow_trace_enabled"): {"enabled": True},
        ("user_a", "v1_flow_trace"): {
            "events": [
                _event(100, "user_a", "route.chat.message", trace_id="t-reply"),
                _event(101, "user_a", "agent.model.call.start", trace_id="t-reply"),
                _event(102, "user_a", "agent.model.call.done", trace_id="t-reply", dur_ms=900),
            ]
        },
    }
    monkeypatch.setattr(data_track.db, "get_blob", lambda uid, kind: blobs.get((uid, kind)))

    html = admin_core.page_html("view=debug&mode=timeline&user_id=user_a")

    assert "Turns" in html
    assert "agent.model.call.start" in html
    assert "agent.model.call.done" in html
    assert "900ms" in html
