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


def _patch_blob_reads(monkeypatch, blobs: dict) -> None:
    monkeypatch.setattr(data_track.db, "get_blob", lambda uid, kind: blobs.get((uid, kind)))
    monkeypatch.setattr(
        data_track.db,
        "get_blobs_for_users",
        lambda user_ids, kinds: {
            (uid, kind): blobs[(uid, kind)]
            for uid in user_ids
            for kind in kinds
            if (uid, kind) in blobs
        },
    )

    def query_events(
        *, user_id="", trace_id_contains="", subsystem="", q="",
        since_epoch=0, limit=50_000, offset=0, **_kwargs,
    ):
        events = [
            dict(event)
            for (uid, kind), doc in blobs.items()
            if kind == "trace_events" and (not user_id or uid == user_id)
            for event in doc.get("events", [])
        ]
        if trace_id_contains:
            events = [e for e in events if trace_id_contains in str(e.get("trace_id") or "")]
        if subsystem:
            events = [e for e in events if e.get("subsystem") == subsystem]
        if since_epoch:
            events = [e for e in events if float(e.get("ts") or 0) >= since_epoch]
        if q:
            events = [e for e in events if q.lower() in data_track._debug_trace_search_text(e)]
        events.sort(key=lambda e: float(e.get("ts") or 0), reverse=True)
        return events[offset:offset + limit]

    monkeypatch.setattr(data_track.db, "query_trace_events", query_events)

    def flat_page(
        *, user_id="", trace_id_contains="", subsystem="", status="", q="",
        since_epoch=0, limit=100, offset=0, candidate_limit, **_kwargs,
    ):
        candidate = query_events(
            user_id=user_id,
            trace_id_contains=trace_id_contains,
            subsystem=subsystem,
            q=q,
            since_epoch=since_epoch,
            limit=candidate_limit + 1,
        )
        truncated = len(candidate) > candidate_limit
        candidate = candidate[:candidate_limit]
        turns = data_track._debug_trace_group_turns(candidate)
        if status:
            turns = [turn for turn in turns if turn["terminal_status"] == status]
            allowed = {(turn["user_id"], turn["trace_id"]) for turn in turns}
            candidate = [
                event for event in candidate
                if (
                    str(event.get("user_id") or ""),
                    str(event.get("trace_id") or "ungrouped"),
                ) in allowed
            ]
        user_rows = []
        for uid in sorted({str(event.get("user_id") or "") for event in candidate}):
            matching = [event for event in candidate if event.get("user_id") == uid]
            user_rows.append({
                "user_id": uid,
                "events": len(matching),
                "last_ts": max(float(event.get("ts") or 0) for event in matching),
            })
        return {
            "events_total": len(candidate),
            "turns_total": len(turns),
            "stalled_turns": sum(turn["terminal_status"] == "stalled" for turn in turns),
            "error_turns": sum(turn["terminal_status"] == "error" for turn in turns),
            "scan_truncated": truncated,
            "users": user_rows,
            "subsystems": sorted({event.get("subsystem") for event in candidate}),
            "statuses": sorted({event.get("status") for event in candidate}),
            "rows": candidate[offset:offset + limit + 1],
            "limit": limit,
            "offset": offset,
        }

    def turn_rows(
        *, turn_keys, user_id="", trace_id_contains="", subsystem="", q="",
        since_epoch=0, candidate_limit, sibling_limit, **_kwargs,
    ):
        candidate = query_events(
            user_id=user_id,
            trace_id_contains=trace_id_contains,
            subsystem=subsystem,
            q=q,
            since_epoch=since_epoch,
            limit=candidate_limit,
        )
        allowed = set(turn_keys)
        rows = [
            event for event in candidate
            if (
                str(event.get("user_id") or ""),
                str(event.get("trace_id") or "ungrouped"),
            ) in allowed
        ]
        return rows[:sibling_limit], len(rows) > sibling_limit

    monkeypatch.setattr(data_track.db, "query_trace_events_flat_page", flat_page)
    monkeypatch.setattr(data_track.db, "query_trace_event_turn_rows", turn_rows)


def test_provider_tool_surface_has_explicit_admin_step_label():
    assert data_track._debug_friendly_step({
        "type": "mcp.surface.provider",
        "subsystem": "mcp",
        "detail": {},
    }) == ("🧩", "MCP Provider 实收工具面")


def test_provider_roundtrip_summary_has_explicit_admin_step_label():
    assert data_track._debug_friendly_step({
        "type": "mcp.roundtrip.provider",
        "subsystem": "mcp",
        "detail": {},
    }) == ("🔁", "Provider 本轮往返")


def test_memory_search_and_index_have_distinct_admin_labels():
    search = data_track._debug_friendly_step({
        "type": "memory.search.called",
        "subsystem": "memory",
        "detail": {},
    })
    index = data_track._debug_friendly_step({
        "type": "memory.index.called",
        "subsystem": "memory",
        "detail": {},
    })

    assert search == ("🔍", "搜索记忆")
    assert index == ("🧩", "浏览记忆总览")
    assert search != index


def test_context_truncation_has_explicit_admin_label_and_visible_counts():
    event = _event(
        1,
        "user_t047",
        "context.truncation",
        trace_id="t047",
        status="warning",
        detail={
            "counts": {
                "profile_cards_truncated": 1,
                "worldbook_truncated": 0,
            }
        },
    )

    assert data_track._debug_friendly_step(event) == ("✂️", "上下文裁剪")
    public = data_track._debug_event_public_json(event)
    assert public["detail"] == {
        "counts": {
            "profile_cards_truncated": 1,
            "worldbook_truncated": 0,
        }
    }


def test_memory_content_truncation_has_visible_route_and_counts_without_body():
    secret = "T074_ADMIN_SECRET_MUST_NOT_APPEAR"
    event = _event(
        1,
        "user_t074",
        "memory.content.truncation",
        trace_id="t074",
        status="warning",
        detail={
            "route": "genesis_history_import",
            "counts": {
                "original_chars": 5042,
                "truncated_chars": 42,
            },
        },
        content_excerpt={},
    )
    event["unexpected_upstream_content"] = secret

    assert data_track._debug_friendly_step(event) == ("✂️", "记忆卡截断")
    public = data_track._debug_event_public_json(event)
    assert public["detail"] == {
        "route": "genesis_history_import",
        "counts": {
            "original_chars": 5042,
            "truncated_chars": 42,
        },
    }
    assert secret not in str(public)


def test_identity_dimensions_set_admin_trace_exposes_only_counts():
    secret_dimension = "T085_DIMENSION_SECRET_NEVER_ADMIN"
    secret_reason = "T085_REASON_SECRET_NEVER_ADMIN"
    event = _event(
        1,
        "user_t085",
        "identity.dimensions_set",
        trace_id="t085",
        detail={
            "counts": {
                "changed_count": 4,
                "added_count": 1,
                "deleted_count": 2,
                "labels_changed": 1,
            },
        },
        content_excerpt={},
    )
    event["dimension_name"] = secret_dimension
    event["reason"] = secret_reason

    assert data_track._debug_friendly_step(event) == ("🪪", "身份维度重写")
    public = data_track._debug_event_public_json(event)
    assert public["detail"] == {
        "counts": {
            "changed_count": 4,
            "added_count": 1,
            "deleted_count": 2,
            "labels_changed": 1,
        },
    }
    serialized = str(public)
    assert secret_dimension not in serialized
    assert secret_reason not in serialized


def test_query_fingerprint_is_visible_but_plaintext_query_stays_redacted():
    fingerprint = "0123456789ab"

    redacted = data_track._debug_redact_value({
        "query_fingerprint": fingerprint,
        "query": "她的生日",
    })

    assert redacted["query_fingerprint"] == fingerprint
    assert redacted["query"] == "<redacted string len=4>"

    spoofed = data_track._debug_redact_value({
        "query_fingerprint": "她的生日",
    })
    assert spoofed["query_fingerprint"] == "<redacted string len=4>"


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
            "metadata": {
                "mode": "onboarding",
                "history_count": 42,
                "window_count": 3,
                "distill_model": "gemini-3-flash-preview",
            },
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
    assert stats["latest_job"]["distill_model"] == "gemini-3-flash-preview"
    assert stats["latest_job"]["metadata"]["distill_model"] == "gemini-3-flash-preview"
    assert stats["jobs"][0]["distill_model"] == "gemini-3-flash-preview"


def test_fast_proactive_snapshot_keeps_expired_out_of_failed_count():
    proactive = data_track._data_track_proactive_from_snapshot(
        {
            "logs": {"proactive_jobs": {"count": 5, "last_ts": 100}},
            "proactive_extra": {
                "jobs_by_status": {"failed": 3, "expired": 2},
                "jobs_failed_by_reason": {"model_timeout": 2, "unknown": 1},
            },
        },
        {},
    )

    assert proactive["failed_jobs"] == 3
    assert proactive["jobs_by_status"]["expired"] == 2
    assert proactive["job_failed_reasons"] == {"model_timeout": 2, "unknown": 1}


def test_trace_vocabulary_failure_is_explicit_retried_and_not_cached(monkeypatch):
    calls = []

    def flaky_loader():
        calls.append("load")
        if len(calls) == 1:
            raise RuntimeError("transient import failure")
        return frozenset({"heartbeat"}), frozenset({"manual_tick"})

    monkeypatch.setattr(data_track, "_TRACE_VOCABULARY_CACHE", None)
    monkeypatch.setattr(data_track, "_load_jobs_store_trace_vocabulary", flaky_loader)

    assert data_track._trace_vocabulary() is None
    assert data_track._TRACE_VOCABULARY_CACHE is None
    assert data_track._trace_vocabulary() == (
        frozenset({"heartbeat"}),
        frozenset({"manual_tick"}),
    )
    assert data_track._trace_vocabulary() == data_track._TRACE_VOCABULARY_CACHE
    assert calls == ["load", "load"]


def test_debug_payload_rejects_empty_producer_trace_vocabulary(monkeypatch):
    with registry._users_lock:
        registry._users[:] = [
            {"user_id": "user_a", "principal_id": "p_a"},
        ]
    _patch_blob_reads(monkeypatch, {})

    cases = (
        (frozenset(), frozenset({"manual_tick"})),
        (frozenset({"heartbeat"}), frozenset()),
    )
    for lanes, enqueue_reasons in cases:
        # Prove the test reached a non-raising producer read with exactly one
        # empty export; otherwise unavailable could merely mean import failure.
        assert bool(lanes) != bool(enqueue_reasons)
        monkeypatch.setattr(data_track, "_TRACE_VOCABULARY_CACHE", None)
        monkeypatch.setattr(
            data_track,
            "_load_jobs_store_trace_vocabulary",
            lambda lanes=lanes, reasons=enqueue_reasons: (lanes, reasons),
        )

        with bind("view=debug&user_id=user_a"):
            payload = data_track._data_track_debug_payload()

        assert payload["observability"]["trace_vocabulary"] == "unavailable"
        assert payload["observability"]["failure_vocabulary"]["status"] == "ok"
        assert data_track._TRACE_VOCABULARY_CACHE is None


def test_debug_payload_marks_trace_vocabulary_failure_without_negative_caching(
    monkeypatch,
):
    with registry._users_lock:
        registry._users[:] = [
            {"user_id": "user_a", "principal_id": "p_a"},
        ]
    blobs = {
        ("user_a", "trace_events"): {
            "events": [
                _event(
                    100,
                    "user_a",
                    "agent.job.enqueued",
                    trace_id="t-unavailable",
                    detail={"lane": "heartbeat", "reason": "manual_tick"},
                )
            ],
        },
    }
    _patch_blob_reads(monkeypatch, blobs)
    monkeypatch.setattr(data_track, "_TRACE_VOCABULARY_CACHE", None)

    def unavailable_loader():
        raise RuntimeError("jobs_store unavailable")

    monkeypatch.setattr(
        data_track, "_load_jobs_store_trace_vocabulary", unavailable_loader,
    )

    with bind("view=debug&mode=timeline&user_id=user_a"):
        payload = data_track._data_track_debug_payload()

    assert payload["observability"]["trace_vocabulary"] == "unavailable"
    assert payload["observability"]["failure_vocabulary"]["status"] == "ok"
    assert payload["turns"][0]["lane"] == ""
    assert data_track._TRACE_VOCABULARY_CACHE is None


def test_debug_payload_groups_multi_user_trace_and_marks_stalled(monkeypatch):
    with registry._users_lock:
        registry._users[:] = [
            {"user_id": "user_a", "principal_id": "p_a", "created_at": "2026-07-04T00:00:00Z"},
            {"user_id": "user_b", "principal_id": "p_b", "created_at": "2026-07-04T00:00:00Z"},
        ]

    blobs = {
        ("user_a", "v1_flow_trace_enabled"): {"enabled": True},
        ("user_b", "v1_flow_trace_enabled"): {"enabled": True},
        ("user_a", "trace_events"): {
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
        ("user_b", "trace_events"): {
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

    _patch_blob_reads(monkeypatch, blobs)

    with bind("view=debug"):
        payload = data_track._data_track_debug_payload()

    assert set(payload) == {
        "summary",
        "filters",
        "options",
        "observability",
        "pagination",
        "users",
        "turns",
        "events",
    }
    assert payload["observability"]["trace_vocabulary"] == "ok"
    assert payload["observability"]["failure_vocabulary"]["status"] == "ok"
    assert set(payload["summary"]) == {
        "generated_at",
        "users_scanned",
        "users_with_events",
        "events_total",
        "turns_total",
        "events_returned",
        "turns_returned",
        "stalled_turns",
        "error_turns",
        "scan_truncated",
    }
    assert payload["summary"]["users_with_events"] == 2
    assert payload["summary"]["events_total"] == 5
    assert [u["user_id"] for u in payload["users"]] == ["user_b", "user_a"]

    turns = {turn["trace_id"]: turn for turn in payload["turns"]}
    assert turns["t-stall"]["terminal_status"] == "stalled"
    assert turns["t-stall"]["is_stalled"] is True
    assert turns["t-ok"]["terminal_status"] == "ok"
    assert turns["t-ok"]["total_dur_ms"] == 3000


def test_blank_flat_payload_does_not_append_zero_event_live_users(monkeypatch):
    with registry._users_lock:
        registry._users[:] = [
            {"user_id": "user_a", "principal_id": "p_a", "created_at": "2026-07-04T00:00:00Z"},
        ]

    blobs = {}
    monkeypatch.delenv("FEEDLING_V1_FLOW_TRACE", raising=False)
    monkeypatch.delenv("FEEDLING_V1_FLOW_TRACE_DEFAULT", raising=False)
    _patch_blob_reads(monkeypatch, blobs)

    with bind("view=debug"):
        payload = data_track._data_track_debug_payload()

    assert payload["observability"]["trace_vocabulary"] == "ok"
    assert payload["observability"]["failure_vocabulary"]["status"] == "ok"
    assert payload["users"] == []


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
        ("user_a", "trace_events"): {"events": events},
    }
    _patch_blob_reads(monkeypatch, blobs)

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


def test_flat_payload_fetches_page_lookahead_not_the_old_full_scan(monkeypatch):
    """The old 50k-wide-row path must not quietly return behind green output tests."""
    with registry._users_lock:
        registry._users[:] = []
    monkeypatch.setattr(
        data_track.db,
        "query_trace_events",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("flat payload returned to the legacy full-row query")
        ),
    )
    calls = []
    sibling_calls = []

    def flat_page(**kwargs):
        calls.append(kwargs)
        rows = [
            _event(105 - idx, "user_a", "agent.reply", trace_id=f"t-{idx}")
            for idx in range(3)
        ]
        return {
            "events_total": 5,
            "turns_total": 5,
            "stalled_turns": 0,
            "error_turns": 0,
            "scan_truncated": False,
            "users": [{"user_id": "user_a", "events": 5, "last_ts": 105}],
            "subsystems": ["agent"],
            "statuses": ["ok"],
            # A limit=2 DB page carries exactly one lookahead row.
            "rows": rows,
        }

    monkeypatch.setattr(data_track.db, "query_trace_events_flat_page", flat_page)
    def sibling_rows(**kwargs):
        sibling_calls.append(kwargs)
        return [], True

    monkeypatch.setattr(data_track.db, "query_trace_event_turn_rows", sibling_rows)
    monkeypatch.setattr(data_track.db, "get_blobs_for_users", lambda *_a: {})

    with bind("view=debug&mode=flat&limit=2"):
        payload = data_track._data_track_debug_payload()

    assert len(calls) == 1
    assert calls[0]["limit"] == 2
    assert calls[0]["candidate_limit"] == data_track.DEBUG_TRACE_CANDIDATE_CAP
    assert sibling_calls[0]["candidate_limit"] == data_track.DEBUG_TRACE_CANDIDATE_CAP
    assert sibling_calls[0]["sibling_limit"] == data_track.DEBUG_TRACE_SIBLING_CAP
    assert len(payload["events"]) == 2
    assert payload["pagination"]["next_offset"] == 2
    assert payload["summary"]["scan_truncated"] is True


def test_debug_trace_query_limits_have_literal_policy_anchors():
    # These independent literals intentionally survive mutations of the source
    # constants; every query-shape test derives its runtime input from them.
    assert data_track.DEBUG_TRACE_CANDIDATE_CAP == 5_000
    assert data_track.DEBUG_TRACE_SIBLING_CAP == 5_000
    assert data_track.DEBUG_TRACE_DB_STATEMENT_TIMEOUT_MS == 2_000
    assert data_track.DEBUG_TRACE_DB_CONNECTION_TIMEOUT_SEC == 1.0


def test_debug_page_renders_nav_filters_and_redacts_plaintext_by_default(monkeypatch):
    with registry._users_lock:
        registry._users[:] = [{"user_id": "user_a", "principal_id": "p_a"}]

    blobs = {
        ("user_a", "v1_flow_trace_enabled"): {"enabled": True},
        ("user_a", "trace_events"): {
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
    _patch_blob_reads(monkeypatch, blobs)

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
    assert "Trace 词表暂不可用" not in html


def test_debug_page_renders_protocol_suppression_without_reply_content(monkeypatch):
    with registry._users_lock:
        registry._users[:] = [{"user_id": "user_protocol", "principal_id": "p_protocol"}]

    private_reply = "PRIVATE_PROTOCOL_FRAGMENT_MUST_NOT_RENDER"
    event = _event(
        101,
        "user_protocol",
        "reply.protocol_fragment_suppressed",
        trace_id="t-protocol",
        status="error",
        detail={
            "lane": "heartbeat",
            "evidence": "tool_call_tail",
            "stop_reason": "",
            "transport_cut": False,
            "final": True,
        },
        content_excerpt={},
    )
    event["private_reply"] = private_reply
    _patch_blob_reads(monkeypatch, {
        ("user_protocol", "trace_events"): {"events": [event]},
    })

    html = admin_core.page_html(
        "view=debug&mode=timeline&user_id=user_protocol&trace_id=t-protocol"
    )

    assert "回复安全 · 协议残片已压制" in html
    assert "tool_call_tail" in html
    assert "heartbeat" in html
    assert private_reply not in html


def test_debug_page_renders_transport_cut_as_distinct_content_free_observation(
    monkeypatch,
):
    with registry._users_lock:
        registry._users[:] = [{"user_id": "user_cut", "principal_id": "p_cut"}]

    private_reply = "PRIVATE_TRUNCATED_REPLY_MUST_NOT_RENDER"
    event = _event(
        102,
        "user_cut",
        "reply.transport_cut_observed",
        trace_id="t-cut",
        status="warning",
        detail={
            "lane": "heartbeat",
            "stop_reason": "max_output_tokens",
            "final": True,
        },
        content_excerpt={},
    )
    event["private_reply"] = private_reply
    _patch_blob_reads(monkeypatch, {
        ("user_cut", "trace_events"): {"events": [event]},
    })

    html = admin_core.page_html(
        "view=debug&mode=timeline&user_id=user_cut&trace_id=t-cut"
    )

    assert "Provider 输出截断 · 仅观测" in html
    assert "max_output_tokens" in html
    assert "heartbeat" in html
    assert "协议残片已压制" not in html
    assert private_reply not in html


def test_debug_page_renders_trace_vocabulary_unavailable_as_a_warning(monkeypatch):
    with registry._users_lock:
        registry._users[:] = [{"user_id": "user_a", "principal_id": "p_a"}]

    secret_lane = "secret_lane_must_not_render"
    blobs = {
        ("user_a", "trace_events"): {
            "events": [
                _event(
                    100,
                    "user_a",
                    "agent.job.enqueued",
                    trace_id="t-unavailable",
                    detail={"lane": secret_lane, "reason": "manual_tick"},
                )
            ],
        },
    }
    _patch_blob_reads(monkeypatch, blobs)
    monkeypatch.setattr(data_track, "_TRACE_VOCABULARY_CACHE", None)
    monkeypatch.setattr(data_track, "_TRACE_PUBLIC_FIELDS_CACHE", None)
    load_calls = []

    def unavailable_loader():
        load_calls.append("load")
        raise RuntimeError("jobs_store unavailable")

    monkeypatch.setattr(
        data_track, "_load_jobs_store_trace_vocabulary", unavailable_loader,
    )

    page = admin_core.page_html("view=debug&mode=timeline&user_id=user_a")

    assert "Trace 词表暂不可用" in page
    assert "不能读成“事件没有这些字段”" in page
    assert secret_lane not in page
    assert load_calls == ["load"]
    assert data_track._TRACE_VOCABULARY_CACHE is None
    assert data_track._TRACE_PUBLIC_FIELDS_CACHE is None



def test_debug_page_renders_load_more_when_paginated(monkeypatch):
    with registry._users_lock:
        registry._users[:] = [{"user_id": "user_a", "principal_id": "p_a"}]

    blobs = {
        ("user_a", "v1_flow_trace_enabled"): {"enabled": True},
        ("user_a", "trace_events"): {
            "events": [
                _event(100 + idx, "user_a", "agent.reply", trace_id=f"t-{idx}")
                for idx in range(3)
            ]
        },
    }
    _patch_blob_reads(monkeypatch, blobs)

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
        ("user_a", "trace_events"): {
            "events": [
                _event(100 + idx, "user_a", "agent.reply", trace_id=f"t-{idx}")
                for idx in range(25)
            ]
        },
    }
    _patch_blob_reads(monkeypatch, blobs)

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
        ("user_a", "trace_events"): {"events": events},
    }
    _patch_blob_reads(monkeypatch, blobs)

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
        ("user_a", "trace_events"): {"events": [event]},
    }
    _patch_blob_reads(monkeypatch, blobs)

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
        ("user_a", "trace_events"): {"events": [event]},
    }
    _patch_blob_reads(monkeypatch, blobs)

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
        ("user_a", "trace_events"): {
            "events": [
                _event(100, "user_a", "route.chat.message", trace_id="t-reply"),
                _event(101, "user_a", "agent.model.call.start", trace_id="t-reply"),
                _event(102, "user_a", "agent.model.call.done", trace_id="t-reply", dur_ms=900),
            ]
        },
    }
    _patch_blob_reads(monkeypatch, blobs)

    html = admin_core.page_html("view=debug&mode=timeline&user_id=user_a")

    assert "Turns" in html
    assert "agent.model.call.start" in html
    assert "agent.model.call.done" in html
    assert "900ms" in html
