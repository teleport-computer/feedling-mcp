"""T245: admin parse/read failures must not masquerade as true empty state."""

from __future__ import annotations

import contextlib
from copy import deepcopy
from types import SimpleNamespace

import pytest

from admin import data_track as dt
from core.reqctx import bind
from hosted import agent_runtime_cutover, config_store
from model_api_runtime.v2 import jobs_store, profile_store


def test_d10_failure_vocabulary_partial_is_visible_and_not_negative_cached(
    monkeypatch,
):
    state = {"worker_available": False}

    def load_jobs():
        return {"known_job_failure"}

    def load_worker():
        if not state["worker_available"]:
            raise RuntimeError("worker import failed")
        return {"known_worker_failure"}

    monkeypatch.setattr(dt, "_KNOWN_FAILURE_CODES_CACHE", None)
    monkeypatch.setattr(
        dt,
        "_FAILURE_VOCABULARY_LOADERS",
        (("jobs_store", load_jobs), ("worker", load_worker)),
    )

    codes, unavailable = dt._failure_vocabulary()
    partial = dt._failure_vocabulary_observability()

    assert codes == frozenset({"known_job_failure"})
    assert unavailable == frozenset({"worker"})
    assert partial == {
        "status": "partial",
        "unavailable_sources": ["worker"],
    }
    assert dt._KNOWN_FAILURE_CODES_CACHE is None
    partial_html = dt._render_failure_vocabulary_warning(partial)
    assert "失败码词表仅部分可用" in partial_html
    assert "worker" in partial_html
    debug_payload = {
        "summary": {
            "generated_at": "2026-08-26T00:00:00Z",
            "users_scanned": 0,
            "users_with_events": 0,
            "events_total": 0,
            "turns_total": 0,
            "events_returned": 0,
            "turns_returned": 0,
            "stalled_turns": 0,
            "error_turns": 0,
            "scan_truncated": False,
        },
        "filters": {"mode": "flat", "view": "debug"},
        "options": {},
        "observability": {
            "trace_vocabulary": "ok",
            "failure_vocabulary": partial,
        },
        "pagination": {
            "limit": 100,
            "offset": 0,
            "total": 0,
            "returned": 0,
            "next_offset": None,
        },
        "users": [],
        "turns": [],
        "events": [],
    }
    with bind("view=debug"):
        debug_html = dt._render_data_track_debug_page(debug_payload)
    assert "失败码词表仅部分可用" in debug_html
    assert "worker" in debug_html

    state["worker_available"] = True
    recovered_codes, recovered_unavailable = dt._failure_vocabulary()
    recovered = dt._failure_vocabulary_observability()

    assert recovered_codes == frozenset(
        {"known_job_failure", "known_worker_failure"}
    )
    assert recovered_unavailable == frozenset()
    assert recovered == {"status": "ok", "unavailable_sources": []}
    assert dt._render_failure_vocabulary_warning(recovered) == ""
    debug_payload["observability"]["failure_vocabulary"] = recovered
    with bind("view=debug"):
        recovered_html = dt._render_data_track_debug_page(debug_payload)
    assert "失败码词表仅部分可用" not in recovered_html

    # A successfully loaded but genuinely empty vocabulary is still healthy.
    monkeypatch.setattr(dt, "_KNOWN_FAILURE_CODES_CACHE", None)
    monkeypatch.setattr(
        dt,
        "_FAILURE_VOCABULARY_LOADERS",
        (("optional", lambda: set()),),
    )
    empty_codes, empty_unavailable = dt._failure_vocabulary()
    assert empty_codes == frozenset()
    assert empty_unavailable == frozenset()
    assert dt._failure_vocabulary_observability() == {
        "status": "ok",
        "unavailable_sources": [],
    }


def test_d1_memory_capture_bad_counts_are_distinct_from_true_zero(
    monkeypatch,
):
    store = SimpleNamespace(user_id="usr_quality")
    monkeypatch.setattr(dt.memory_service, "_load_moments", lambda _store: [])

    def bad_logs(_user_id, stream):
        if stream == "memory_capture_jobs":
            return [{"actions_written": float("inf")}]
        return []

    monkeypatch.setattr(dt.db, "log_read_all", bad_logs)
    bad_stats = dt._memory_stats(store)
    assert bad_stats["capture_actions_written"] == 0
    assert bad_stats["capture_actions_written_status"] == "invalid"
    assert bad_stats["capture_actions_written_invalid_rows"] == 1

    capture_store = SimpleNamespace(
        list_proactive_jobs=lambda limit=0: [{
            "job_id": "capture_bad",
            "job_kind": "memory_capture",
            "capture_result": {
                "skipped": {"duplicate": "not-a-count"},
                "applied": {"added": 0, "superseded": 0},
            },
        }]
    )
    bad_validation = dt._memory_capture_validation_detail(capture_store)
    assert bad_validation["counts_status"] == "invalid"
    assert bad_validation["invalid_count_fields"] == ["skipped.duplicate"]
    bad_html = dt._render_data_quality_warnings({
        "memory": bad_stats,
        "memory_capture_validation": bad_validation,
    })
    assert "actions_written 有坏值" in bad_html
    assert "validation 有坏计数" in bad_html

    monkeypatch.setattr(dt.db, "log_read_all", lambda *_args: [])
    empty_stats = dt._memory_stats(store)
    empty_validation = dt._memory_capture_validation_detail(
        SimpleNamespace(list_proactive_jobs=lambda limit=0: [])
    )
    assert empty_stats["capture_actions_written"] == 0
    assert empty_stats["capture_actions_written_status"] == "ok"
    assert empty_validation["counts_status"] == "ok"
    assert dt._render_data_quality_warnings({
        "memory": empty_stats,
        "memory_capture_validation": empty_validation,
    }) == ""


@pytest.mark.parametrize("bad_value", (-1, float("inf")), ids=("negative", "inf"))
def test_d1_actions_written_rejects_each_nonnegative_int_failure_half(
    monkeypatch,
    bad_value,
):
    store = SimpleNamespace(user_id="usr_quality")
    monkeypatch.setattr(dt.memory_service, "_load_moments", lambda _store: [])
    rows = [{"actions_written": bad_value}]
    monkeypatch.setattr(
        dt.db,
        "log_read_all",
        lambda _user_id, stream: rows if stream == "memory_capture_jobs" else [],
    )

    bad = dt._memory_stats(store)
    assert bad["capture_actions_written"] == 0
    assert bad["capture_actions_written_status"] == "invalid"
    assert bad["capture_actions_written_invalid_rows"] == 1

    rows.clear()
    empty = dt._memory_stats(store)
    assert empty["capture_actions_written"] == 0
    assert empty["capture_actions_written_status"] == "ok"


@pytest.mark.parametrize(
    ("section", "field", "bad_value", "invalid_field"),
    (
        ("skipped", "duplicate", -1, "skipped.duplicate"),
        ("skipped", "duplicate", float("inf"), "skipped.duplicate"),
        ("applied", "added", -1, "applied.added"),
        ("applied", "added", float("inf"), "applied.added"),
    ),
    ids=("skipped-negative", "skipped-inf", "applied-negative", "applied-inf"),
)
def test_d1_capture_validation_rejects_each_nonnegative_int_failure_half(
    section,
    field,
    bad_value,
    invalid_field,
):
    result = {
        "skipped": {"duplicate": 0},
        "applied": {"added": 0, "superseded": 0},
    }
    result[section][field] = bad_value
    rows = [{
        "job_id": "capture_numeric_bad",
        "job_kind": "memory_capture",
        "capture_result": result,
    }]
    store = SimpleNamespace(list_proactive_jobs=lambda limit=0: list(rows))

    bad = dt._memory_capture_validation_detail(store)
    assert bad["counts_status"] == "invalid"
    assert bad["invalid_count_fields"] == [invalid_field]

    rows.clear()
    empty = dt._memory_capture_validation_detail(store)
    assert empty["counts_status"] == "ok"
    assert empty["invalid_count_fields"] == []


def test_d3_snapshot_app_and_day_bad_values_are_distinct_from_true_empty():
    bad_memory = dt._data_track_memory_from_snapshot({
        "memory": {"by_source": {"agent": "not-a-count"}},
    })
    assert bad_memory["by_source"]["agent"] == 0
    assert bad_memory["counts_status"] == "invalid"
    assert bad_memory["invalid_count_fields"] == ["memory.by_source.agent"]

    bad_app = dt._data_track_app_usage_from_snapshot({
        "app_usage": {"foreground_sec": "not-a-count", "last_at": "nan"},
    })
    assert bad_app["foreground_sec"] == 0
    assert bad_app["last_at"] == ""
    assert bad_app["fields_status"] == "invalid"
    assert bad_app["invalid_fields"] == ["foreground_sec", "last_at"]

    invalid_days: list[str] = []
    selected = dt._default_usage_histogram_day(
        [{"day": "not-a-day"}, {"day": "2020-01-02"}],
        invalid_days=invalid_days,
    )
    assert selected == "2020-01-02"
    assert invalid_days == ["not-a-day"]
    bad_html = dt._render_data_quality_warnings({
        "memory": bad_memory,
        "app_usage": bad_app,
    })
    assert "memory snapshot 的计数字段有坏值" in bad_html
    assert "App usage 有字段读不出来" in bad_html

    empty_memory = dt._data_track_memory_from_snapshot({})
    empty_app = dt._data_track_app_usage_from_snapshot({})
    empty_days: list[str] = []
    fallback_day = dt._default_usage_histogram_day([], invalid_days=empty_days)
    assert empty_memory["counts_status"] == "ok"
    assert empty_app["fields_status"] == "ok"
    assert empty_app["foreground_sec"] == 0
    assert empty_days == []
    assert len(fallback_day) == 10
    assert dt._render_data_quality_warnings({
        "memory": empty_memory,
        "app_usage": empty_app,
    }) == ""


@pytest.mark.parametrize("bad_value", (-1, float("inf")), ids=("negative", "inf"))
def test_d3_snapshot_count_dict_rejects_each_nonnegative_int_failure_half(
    bad_value,
):
    bad = dt._data_track_memory_from_snapshot({
        "memory": {"by_source": {"agent": bad_value}},
    })
    empty = dt._data_track_memory_from_snapshot({})

    assert bad["by_source"]["agent"] == 0
    assert bad["counts_status"] == "invalid"
    assert bad["invalid_count_fields"] == ["memory.by_source.agent"]
    assert empty["by_source"] == {}
    assert empty["counts_status"] == "ok"


@pytest.mark.parametrize(
    "bad_epoch",
    (-1, float("inf"), 1e20),
    ids=("negative", "inf", "fromtimestamp-overflow"),
)
def test_d3_app_usage_rejects_each_epoch_failure_half(bad_epoch):
    bad = dt._data_track_app_usage_from_snapshot({
        "app_usage": {"last_at": bad_epoch},
    })
    empty = dt._data_track_app_usage_from_snapshot({})

    assert bad["last_at_epoch"] == 0
    assert bad["last_at"] == ""
    assert bad["fields_status"] == "invalid"
    assert bad["invalid_fields"] == ["last_at"]
    assert empty["last_at_epoch"] == 0
    assert empty["fields_status"] == "ok"


@pytest.mark.parametrize(
    ("field", "bad_value"),
    (
        ("foreground_sec", -1),
        ("foreground_sec", float("inf")),
        ("sessions", -1),
        ("sessions", float("inf")),
    ),
    ids=("foreground-negative", "foreground-inf", "sessions-negative", "sessions-inf"),
)
def test_d3_app_usage_rejects_each_count_failure_half(field, bad_value):
    bad = dt._data_track_app_usage_from_snapshot({
        "app_usage": {field: bad_value},
    })
    empty = dt._data_track_app_usage_from_snapshot({})

    assert bad[field] == 0
    assert bad["fields_status"] == "invalid"
    assert bad["invalid_fields"] == [field]
    assert empty[field] == 0
    assert empty["fields_status"] == "ok"


@pytest.mark.parametrize(
    ("payload_key", "build", "bad_snapshot", "invalid_field"),
    (
        (
            "chat",
            lambda snap: dt._data_track_chat_from_snapshot(snap),
            {"chat": {"by_role": {"user": "not-a-count"}}},
            "chat.by_role.user",
        ),
        (
            "proactive",
            lambda snap: dt._data_track_proactive_from_snapshot(snap, {}),
            {"proactive_extra": {"jobs_by_status": {"failed": "not-a-count"}}},
            "proactive.jobs_by_status.failed",
        ),
        (
            "tracking",
            lambda snap: dt._data_track_tracking_from_snapshot(snap),
            {"log_counts": {"tracking_by_type": {"open": "not-a-count"}}},
            "tracking.by_type.open",
        ),
        (
            "bootstrap_events",
            lambda snap: dt._data_track_bootstrap_from_snapshot(snap),
            {"log_counts": {"bootstrap_by_type": {"start": "not-a-count"}}},
            "bootstrap.by_type.start",
        ),
    ),
)
def test_d3_each_snapshot_counter_family_has_failure_and_empty_mirrors(
    payload_key,
    build,
    bad_snapshot,
    invalid_field,
):
    bad = build(bad_snapshot)
    empty = build({})

    assert bad["counts_status"] == "invalid"
    assert bad["invalid_count_fields"] == [invalid_field]
    assert empty["counts_status"] == "ok"
    assert empty["invalid_count_fields"] == []
    assert "观测数据异常" in dt._render_data_quality_warnings({payload_key: bad})
    assert dt._render_data_quality_warnings({payload_key: empty}) == ""


def test_t367_snapshot_timeout_marks_all_snapshot_labels_unknown():
    snap = {
        "snapshot_read_status": {
            "level": "timeout",
            "message": "取数超时（记了，但这里读不出来）",
        }
    }
    blocks = {
        "app_usage": dt._data_track_app_usage_from_snapshot(snap),
        "memory": dt._data_track_memory_from_snapshot(snap),
        "chat": dt._data_track_chat_from_snapshot(snap),
        "proactive": dt._data_track_proactive_from_snapshot(snap, {}),
        "tracking": dt._data_track_tracking_from_snapshot(snap),
        "bootstrap_events": dt._data_track_bootstrap_from_snapshot(snap),
    }

    assert blocks["app_usage"]["fields_status"] == "unknown"
    for key in ("memory", "chat", "proactive", "tracking", "bootstrap_events"):
        assert blocks[key]["counts_status"] == "unknown", key
    for key in ("changes_breakdowns_status", "capture_breakdowns_status"):
        assert blocks["memory"][key] == "unknown"
    for key in ("proactive", "tracking", "bootstrap_events"):
        assert blocks[key]["breakdowns_status"] == "unknown", key

    html = dt._render_data_quality_warnings({
        "snapshot_read_status": snap["snapshot_read_status"],
        **blocks,
    })
    assert "取数超时（记了，但这里读不出来）" in html


@pytest.mark.parametrize(
    "snap",
    (
        {},
        {"legacy_background_breakdowns_status": ""},
        {"legacy_background_breakdowns_status": 0},
        {"legacy_background_breakdowns_status": []},
    ),
)
def test_t367_breakdown_coverage_never_defaults_unknown_input_to_available(snap):
    blocks = (
        dt._data_track_memory_from_snapshot(snap),
        dt._data_track_proactive_from_snapshot(snap, {}),
        dt._data_track_tracking_from_snapshot(snap),
        dt._data_track_bootstrap_from_snapshot(snap),
    )

    assert blocks[0]["changes_breakdowns_status"] == "unknown"
    assert blocks[0]["capture_breakdowns_status"] == "unknown"
    assert blocks[1]["breakdowns_status"] == "unknown"
    assert blocks[2]["breakdowns_status"] == "unknown"
    assert blocks[3]["breakdowns_status"] == "unknown"


@pytest.mark.parametrize("raw", ("available", "omitted"))
def test_t367_breakdown_coverage_preserves_explicit_known_state(raw):
    snap = {"legacy_background_breakdowns_status": raw}

    memory = dt._data_track_memory_from_snapshot(snap)
    assert memory["changes_breakdowns_status"] == raw
    assert memory["capture_breakdowns_status"] == raw
    assert dt._data_track_proactive_from_snapshot(snap, {})["breakdowns_status"] == raw
    assert dt._data_track_tracking_from_snapshot(snap)["breakdowns_status"] == raw
    assert dt._data_track_bootstrap_from_snapshot(snap)["breakdowns_status"] == raw


def test_t367_responder_read_failure_marks_poll_evidence_unknown():
    responder = dt._effective_responder(
        route="resident",
        consumer_state=None,
        runtime=None,
        snapshot_read_status={"level": "timeout", "message": "timed out"},
    )

    assert responder["effective_responder"] == "unknown"
    assert responder["basis"] == "snapshot_read_failed"
    assert responder["read_status"] == "read_failed"
    assert responder["poll_evidence_status"] == "unknown"
    warning = dt._render_data_quality_warnings({"responder": responder})
    assert "不能判为确实没有轮询证据" in warning


def test_d3_dau_bad_day_is_visible_in_json_and_html(monkeypatch):
    rows = [{
        "day": "not-a-day",
        "session_dau": 0,
        "chat_dau": 0,
        "tracking_dau": 0,
        "active_events": 0,
        "user_messages": 0,
        "tracking_events": 0,
        "foreground_sec": 0,
        "median_user_sec": 0,
        "session_count": 0,
    }]
    monkeypatch.setattr(dt.db, "admin_dau_snapshot_bounds", lambda: {})
    monkeypatch.setattr(
        dt.db,
        "admin_data_track_dau",
        lambda **_kwargs: list(rows),
    )
    monkeypatch.setattr(
        dt.db,
        "admin_data_track_usage_histogram",
        lambda **kwargs: {
            "day": kwargs["day"],
            "total_users": 0,
            "buckets": [],
        },
    )

    with bind("view=dau"):
        bad = dt._data_track_dau_payload()
        bad_html = dt._render_data_track_dau_page(bad)
    assert bad["observability"] == {
        "dau_query": "ok",
        "day_values": "invalid",
        "invalid_day_rows": 1,
    }
    assert "DAU 行里有 day 值读不出来" in bad_html

    rows.clear()
    with bind("view=dau"):
        empty = dt._data_track_dau_payload()
        empty_html = dt._render_data_track_dau_page(empty)
    assert empty["observability"] == {
        "dau_query": "ok",
        "day_values": "ok",
        "invalid_day_rows": 0,
    }
    assert "DAU 行里有 day 值读不出来" not in empty_html


def test_t428_dau_query_failure_is_distinct_from_true_zero(monkeypatch):
    state = {"failed": False}

    class EmptyResult:
        def fetchone(self):
            return None, None

        def fetchall(self):
            return []

    class EmptyConnection:
        def execute(self, *_args, **_kwargs):
            return EmptyResult()

    @contextlib.contextmanager
    def connection():
        if state["failed"]:
            raise RuntimeError("injected query failure")
        yield EmptyConnection()

    monkeypatch.setattr(dt.db, "admin_dau_snapshot_bounds", lambda: {})
    monkeypatch.setattr(dt.db, "_admin_data_track_connection", connection)
    monkeypatch.setattr(
        dt.db,
        "admin_data_track_usage_histogram",
        lambda **kwargs: {
            "day": kwargs["day"],
            "total_users": 0,
            "buckets": [],
        },
    )

    with bind("view=dau"):
        true_zero = dt._data_track_dau_payload()
        true_zero_html = dt._render_data_track_dau_page(true_zero)
    state["failed"] = True
    with bind("view=dau"):
        query_failed = dt._data_track_dau_payload()
        query_failed_html = dt._render_data_track_dau_page(query_failed)

    assert true_zero["rows"] == query_failed["rows"] == []
    assert true_zero["observability"] != query_failed["observability"]
    assert true_zero["observability"]["dau_query"] == "ok"
    assert query_failed["observability"]["dau_query"] == "failed"
    assert "DAU 查询失败" not in true_zero_html
    assert "DAU 查询失败" in query_failed_html


def test_d4_bad_consumer_poll_is_distinct_from_no_poll():
    common = {
        "route": "resident",
        "chat": {},
        "memory": {},
        "identity": None,
        "history_import": {},
        "model_api_config": None,
        "bootstrap_events": {},
    }
    bad_fast = dt._data_track_fast_validation(
        **common,
        consumer_state={"last_poll_epoch": "not-an-epoch", "official": True},
    )
    empty_fast = dt._data_track_fast_validation(
        **common,
        consumer_state={},
    )
    assert bad_fast["consumer_poll_status"] == "invalid"
    assert empty_fast["consumer_poll_status"] == "missing"

    bad_responder = dt._effective_responder(
        route="resident",
        consumer_state={
            "poll_consumers": {
                "resident-a": {"last_poll_epoch": "not-an-epoch"},
            },
        },
        runtime=None,
        now_epoch=1000,
    )
    empty_responder = dt._effective_responder(
        route="resident",
        consumer_state={"poll_consumers": {}},
        runtime=None,
        now_epoch=1000,
    )
    assert bad_responder["poll_evidence_status"] == "invalid"
    assert bad_responder["poll_observations"][0]["last_poll_status"] == "invalid"
    assert empty_responder["poll_evidence_status"] == "missing"
    assert "last_poll_epoch 读不出来" in dt._render_data_quality_warnings({
        "onboarding": bad_fast,
        "responder": bad_responder,
    })
    assert dt._render_data_quality_warnings({
        "onboarding": empty_fast,
        "responder": empty_responder,
    }) == ""


@pytest.mark.parametrize("bad_epoch", (-1, float("inf")), ids=("negative", "inf"))
def test_d4_poll_epochs_reject_each_finite_nonnegative_failure_half(bad_epoch):
    common = {
        "route": "resident",
        "chat": {},
        "memory": {},
        "identity": None,
        "history_import": {},
        "model_api_config": None,
        "bootstrap_events": {},
    }
    bad_fast = dt._data_track_fast_validation(
        **common,
        consumer_state={"last_poll_epoch": bad_epoch, "official": True},
    )
    empty_fast = dt._data_track_fast_validation(
        **common,
        consumer_state={},
    )
    assert bad_fast["consumer_poll_status"] == "invalid"
    assert empty_fast["consumer_poll_status"] == "missing"

    bad_responder = dt._effective_responder(
        route="resident",
        consumer_state={
            "poll_consumers": {"resident-a": {"last_poll_epoch": bad_epoch}},
        },
        runtime=None,
        now_epoch=1000,
    )
    empty_responder = dt._effective_responder(
        route="resident",
        consumer_state={"poll_consumers": {}},
        runtime=None,
        now_epoch=1000,
    )
    assert bad_responder["poll_evidence_status"] == "invalid"
    assert bad_responder["poll_observations"][0]["last_poll_status"] == "invalid"
    assert empty_responder["poll_evidence_status"] == "missing"


def test_d5_bad_notice_occurrences_are_distinct_from_real_zero(monkeypatch):
    monkeypatch.setattr(
        dt.db,
        "log_read_all",
        lambda *_args: [{"occurrences": "not-a-count", "last_ts": 1}],
    )
    bad = dt._notice_summaries("usr_quality")
    assert bad[0]["occurrences"] == 0
    assert bad[0]["occurrences_status"] == "invalid"
    assert "notice occurrences 有坏值" in dt._render_data_quality_warnings({
        "notice_summaries": bad,
    })

    monkeypatch.setattr(
        dt.db,
        "log_read_all",
        lambda *_args: [{"occurrences": 0, "last_ts": 1}],
    )
    empty = dt._notice_summaries("usr_quality")
    assert empty[0]["occurrences"] == 0
    assert empty[0]["occurrences_status"] == "ok"
    assert dt._render_data_quality_warnings({"notice_summaries": empty}) == ""


@pytest.mark.parametrize("bad_value", (-1, float("inf")), ids=("negative", "inf"))
def test_d5_occurrences_rejects_each_nonnegative_int_failure_half(
    monkeypatch,
    bad_value,
):
    rows = [{"occurrences": bad_value, "last_ts": 1}]
    monkeypatch.setattr(dt.db, "log_read_all", lambda *_args: list(rows))

    bad = dt._notice_summaries("usr_quality")
    assert bad[0]["occurrences"] == 0
    assert bad[0]["occurrences_status"] == "invalid"

    rows[0]["occurrences"] = 0
    empty = dt._notice_summaries("usr_quality")
    assert empty[0]["occurrences"] == 0
    assert empty[0]["occurrences_status"] == "ok"


def test_d6_runtime_load_and_derive_failures_are_distinct_from_empty_config(
    monkeypatch,
):
    def fail_load(_store):
        raise RuntimeError("config storage unavailable")

    monkeypatch.setattr(config_store, "_load_model_api_config", fail_load)
    unavailable = dt._runtime_summary(SimpleNamespace())
    assert unavailable["config_status"] == "unavailable"
    assert unavailable["provider"] == ""
    assert "runtime config 暂不可读" in dt._render_data_quality_warnings({
        "runtime": unavailable,
    })

    monkeypatch.setattr(config_store, "_load_model_api_config", lambda _store: {})
    empty = dt._runtime_summary(SimpleNamespace())
    assert empty["config_status"] == "ok"
    assert empty["driver_status"] == "not_applicable"
    assert dt._render_data_quality_warnings({"runtime": empty}) == ""

    monkeypatch.setattr(config_store, "_load_model_api_config", lambda _store: [])
    invalid = dt._runtime_summary(SimpleNamespace())
    assert invalid["config_status"] == "invalid"
    assert "config 形状无效" in dt._render_data_quality_warnings({
        "runtime": invalid,
    })

    monkeypatch.setattr(
        config_store,
        "_load_model_api_config",
        lambda _store: {"provider": "openai", "model": "gpt-test"},
    )

    def fail_driver(_provider):
        raise RuntimeError("driver registry unavailable")

    monkeypatch.setattr(agent_runtime_cutover, "driver_for_provider", fail_driver)
    bad_driver = dt._runtime_summary(SimpleNamespace())
    assert bad_driver["config_status"] == "ok"
    assert bad_driver["driver_status"] == "unavailable"
    assert "driver/transport 推导失败" in dt._render_data_quality_warnings({
        "runtime": bad_driver,
    })


def test_d7_bad_wake_epoch_is_distinct_from_unset_schedule(monkeypatch):
    monkeypatch.setattr(
        jobs_store,
        "get_wake_schedule",
        lambda _user_id: {"next_heartbeat_at": "not-an-epoch"},
    )
    monkeypatch.setattr(
        jobs_store,
        "heartbeat_due_diagnosis",
        lambda _user_id: {"blocked_by": []},
    )
    bad = dt._v2_wake_schedule_detail("usr_quality", {})
    assert bad["next_heartbeat_at"] == ""
    assert bad["fields_status"] == "invalid"
    assert bad["field_status"]["next_heartbeat_at"] == "invalid"
    assert "wake schedule 有坏时间" in dt._render_data_quality_warnings({
        "v2_wake_schedule": bad,
    })

    monkeypatch.setattr(jobs_store, "get_wake_schedule", lambda _user_id: {})
    empty = dt._v2_wake_schedule_detail("usr_quality", {})
    assert empty["next_heartbeat_at"] == ""
    assert empty["fields_status"] == "ok"
    assert empty["field_status"]["next_heartbeat_at"] == "missing"
    assert dt._render_data_quality_warnings({"v2_wake_schedule": empty}) == ""


@pytest.mark.parametrize("bad_epoch", (-1, float("inf")), ids=("negative", "inf"))
def test_d7_wake_epoch_rejects_each_finite_nonnegative_failure_half(
    monkeypatch,
    bad_epoch,
):
    rows = {"next_heartbeat_at": bad_epoch}
    monkeypatch.setattr(jobs_store, "get_wake_schedule", lambda _user_id: rows)
    monkeypatch.setattr(
        jobs_store,
        "heartbeat_due_diagnosis",
        lambda _user_id: {"blocked_by": []},
    )

    bad = dt._v2_wake_schedule_detail("usr_quality", {})
    assert bad["next_heartbeat_at"] == ""
    assert bad["fields_status"] == "invalid"
    assert bad["field_status"]["next_heartbeat_at"] == "invalid"

    rows.clear()
    empty = dt._v2_wake_schedule_detail("usr_quality", {})
    assert empty["next_heartbeat_at"] == ""
    assert empty["fields_status"] == "ok"
    assert empty["field_status"]["next_heartbeat_at"] == "missing"


def test_d8_invalid_profile_count_is_distinct_from_missing_profile(monkeypatch):
    valid = profile_store.build_profile_document(
        "usr_quality",
        state="empty",
        source={
            "card_count": 0,
            "max_updated_at": "",
            "generated_at": "2026-08-26T00:00:00Z",
        },
        last_attempt={
            "at": "2026-08-26T00:00:00Z",
            "reject_code": "",
            "attempts": 0,
            "retry_disposition": "",
            "retry_family": "",
            "retry_attempts": 0,
            "retry_not_before": 0,
        },
    )
    malformed = deepcopy(valid)
    malformed["source"]["card_count"] = "not-a-count"
    monkeypatch.setattr(dt.db, "get_blob_strict", lambda *_args: malformed)

    bad = dt._v2_profile_detail("usr_quality")
    assert bad == {
        "state": "read_error",
        "document_status": "invalid",
        "invalid_reason": "invalid_source_card_count",
    }
    assert "producer schema" in dt._render_data_quality_warnings({
        "v2_profile": bad,
    })

    monkeypatch.setattr(dt.db, "get_blob_strict", lambda *_args: None)
    missing = dt._v2_profile_detail("usr_quality")
    assert missing == {"state": "missing", "document_status": "missing"}
    assert dt._render_data_quality_warnings({"v2_profile": missing}) == ""
