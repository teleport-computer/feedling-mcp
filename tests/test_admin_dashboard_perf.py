"""Ops-overview perf work: windowed queries, page cache, parallel fan-out."""
from __future__ import annotations

import logging
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db  # noqa: E402
from admin import admin_core  # noqa: E402
from conftest import seed_user  # noqa: E402
from model_api_runtime.v2 import jobs_store  # noqa: E402

# Shared render fixtures (payload shapes) from the ops-views test module.
from test_data_track_ops_views import (  # noqa: E402
    _chat,
    _imports,
    _product,
    _runtime,
    _usage,
)

requires_pg = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="windowed dashboard query tests require PostgreSQL",
)

_OLD_USER = "u_dash_perf_prev"
_NEW_USER = "u_dash_perf_cur"
_LANE_USER = "u_dash_perf_lane"
_ALL_USERS = (_OLD_USER, _NEW_USER, _LANE_USER)


@pytest.fixture(autouse=True)
def _clean_rows():
    if not os.environ.get("DATABASE_URL"):
        yield
        return

    def clean() -> None:
        with db.get_pool().connection() as conn:
            conn.execute(
                "DELETE FROM user_logs WHERE user_id IN (%s,%s,%s)", _ALL_USERS
            )
            conn.execute(
                "DELETE FROM chat_messages WHERE user_id IN (%s,%s,%s)",
                _ALL_USERS,
            )
            conn.execute(
                "DELETE FROM v2_turn_metrics WHERE user_id IN (%s,%s,%s)",
                _ALL_USERS,
            )
            conn.execute(
                "DELETE FROM users WHERE user_id IN (%s,%s,%s)", _ALL_USERS
            )

    clean()
    try:
        yield
    finally:
        clean()


# --------------------------------------------------------------------------- #
# (a) admin_onboarding_funnel registered_cutoff_ts cohort filter
# --------------------------------------------------------------------------- #


@requires_pg
def test_onboarding_funnel_cutoff_matches_unbounded_cohort():
    now = datetime.now(timezone.utc)
    seed_user(_OLD_USER, created_at=(now - timedelta(hours=100)).isoformat())
    seed_user(_NEW_USER, created_at=(now - timedelta(hours=1)).isoformat())
    with db.get_pool().connection() as conn:
        # Milestones for both users (VPS route: t1 = first activity, t3 =
        # first genuine reply) so the windowed query must reproduce full
        # rows, not just user_ids.
        conn.execute(
            "INSERT INTO chat_messages (user_id,msg_id,ts,doc) VALUES "
            "(%s,'dash-old-reply',%s,"
            " '{\"role\":\"agent\",\"source\":\"hosted_v2\"}'::jsonb),"
            "(%s,'dash-new-reply',%s,"
            " '{\"role\":\"agent\",\"source\":\"hosted_v2\"}'::jsonb)",
            (
                _OLD_USER,
                (now - timedelta(hours=99)).timestamp(),
                _NEW_USER,
                (now - timedelta(minutes=30)).timestamp(),
            ),
        )

    cutoff = (now - timedelta(hours=24)).timestamp()
    unbounded = db.admin_onboarding_funnel()
    windowed = db.admin_onboarding_funnel(registered_cutoff_ts=cutoff)

    windowed_ids = {row["user_id"] for row in windowed}
    assert _NEW_USER in windowed_ids
    assert _OLD_USER not in windowed_ids
    assert _OLD_USER in {row["user_id"] for row in unbounded}

    # The windowed result must equal the unbounded result restricted to the
    # cohort — every milestone value included, since the cohort filter
    # narrows the milestone CTEs and must not change per-user MINs.
    expected = {
        row["user_id"]: row
        for row in unbounded
        if row["t0"] is not None and row["t0"] >= cutoff
    }
    assert {row["user_id"]: row for row in windowed} == expected

    new_row = next(row for row in windowed if row["user_id"] == _NEW_USER)
    assert new_row["t1"] is not None
    assert new_row["t3"] is not None


# --------------------------------------------------------------------------- #
# (b) recent_admin_product_kpis offset window
# --------------------------------------------------------------------------- #


@requires_pg
def test_product_kpis_offset_window_excludes_current_rows():
    now = datetime.now(timezone.utc)
    before_prev = db.recent_admin_product_kpis(within_hours=24, offset_hours=24)
    before_cur = db.recent_admin_product_kpis(within_hours=24)

    # One user + session squarely inside the previous window (30h ago), one
    # squarely inside the current window (now).
    seed_user(_OLD_USER, created_at=(now - timedelta(hours=30)).isoformat())
    seed_user(_NEW_USER, created_at=now.isoformat())
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO user_logs (user_id,stream,ts,item_key,doc) VALUES "
            "(%s,'tracking_events',%s,'dash-prev-1',"
            " '{\"type\":\"app_session_end\"}'::jsonb),"
            "(%s,'tracking_events',%s,'dash-cur-1',"
            " '{\"type\":\"app_session_end\"}'::jsonb)",
            (
                _OLD_USER,
                (now - timedelta(hours=30)).timestamp(),
                _NEW_USER,
                now.timestamp(),
            ),
        )

    prev = db.recent_admin_product_kpis(within_hours=24, offset_hours=24)
    cur = db.recent_admin_product_kpis(within_hours=24)

    # Previous window sees only the 30h-old rows.
    assert prev["window_app_users"] == before_prev["window_app_users"] + 1
    assert prev["app_sessions"] == before_prev["app_sessions"] + 1
    assert (
        prev["new_registered_accounts"]
        == before_prev["new_registered_accounts"] + 1
    )
    # Current window sees only the fresh rows — the offset window excludes
    # them and vice versa.
    assert cur["window_app_users"] == before_cur["window_app_users"] + 1
    assert cur["app_sessions"] == before_cur["app_sessions"] + 1
    assert (
        cur["new_registered_accounts"]
        == before_cur["new_registered_accounts"] + 1
    )
    # Coverage invariant: the windowed funnel cohort must exactly cover the
    # SQL registration count in BOTH windows, else the rate must be None.
    for report in (prev, cur):
        onboarding = report["onboarding"]
        assert onboarding["coverage_complete"] is True
        assert onboarding["cohort_accounts"] == report["new_registered_accounts"]


# --------------------------------------------------------------------------- #
# (c) recent_token_usage_by_lane half-open offset boundary
# --------------------------------------------------------------------------- #


@requires_pg
def test_token_usage_offset_window_is_half_open_at_boundary():
    seed_user(_LANE_USER)
    before_cur = (
        jobs_store.recent_token_usage_by_lane(within_hours=24)["total"] or {}
    )
    before_prev = (
        jobs_store.recent_token_usage_by_lane(within_hours=24, offset_hours=24)[
            "total"
        ]
        or {}
    )

    # Row exactly at the shared boundary between the current window
    # [now-24h, now] and the previous window [now-48h, now-24h).
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO v2_turn_metrics (job_id,user_id,lane,prompt_tokens,"
            "completion_tokens,model_calls,retries,failed,status,"
            "usage_reported_calls,created_at) VALUES "
            "(987654321001,%s,'chat',10,5,1,0,false,'ok',1,"
            " now() - make_interval(hours => 24))",
            (_LANE_USER,),
        )

    cur = jobs_store.recent_token_usage_by_lane(within_hours=24)["total"] or {}
    prev = (
        jobs_store.recent_token_usage_by_lane(within_hours=24, offset_hours=24)[
            "total"
        ]
        or {}
    )

    in_cur = int(cur.get("turns") or 0) - int(before_cur.get("turns") or 0)
    in_prev = int(prev.get("turns") or 0) - int(before_prev.get("turns") or 0)
    assert in_cur >= 0 and in_prev >= 0
    # Half-open windows: the boundary row lands in exactly one window —
    # never both (double count), never neither (dropped).
    assert in_cur + in_prev == 1


# --------------------------------------------------------------------------- #
# (d)-(f) page_html cache + parallel fan-out (no DB; builders monkeypatched)
# --------------------------------------------------------------------------- #


def _stub_builders(monkeypatch, counters, **overrides):
    def make(name, payload_fn):
        def fn(**_kwargs):
            counters[name] = counters.get(name, 0) + 1
            return payload_fn()
        return fn

    defaults = {
        "imports": (admin_core.db, "recent_genesis_import_health", _imports),
        "chat": (admin_core.jobs_store, "recent_chat_reliability", _chat),
        "runtime": (admin_core.jobs_store, "recent_runtime_health", _runtime),
        "product": (admin_core.db, "recent_admin_product_kpis", _product),
        "usage": (admin_core.jobs_store, "recent_token_usage_by_lane", _usage),
    }
    for name, (module, attr, payload_fn) in defaults.items():
        monkeypatch.setattr(
            module, attr, overrides.get(name) or make(name, payload_fn)
        )


@pytest.fixture(autouse=True)
def _reset_page_cache_failure_state():
    # conftest's _reset_admin_page_cache predates the failure-cooldown dict;
    # clear it here so a cooldown recorded by one test cannot leak into the
    # next within the 5s window.
    yield
    with admin_core._page_cache_lock:
        admin_core._page_cache_last_failure.clear()


def test_page_html_cache_hits_within_ttl_and_skips_builders(monkeypatch):
    counters: dict[str, int] = {}
    _stub_builders(monkeypatch, counters)

    first = admin_core.page_html("view=overview&hours=24")
    assert "页面缓存" not in first
    # product/usage are each built twice: current window + previous window.
    assert counters["product"] == 2
    assert counters["usage"] == 2
    built = dict(counters)

    second = admin_core.page_html("view=overview&hours=24")
    assert "页面缓存" in second
    assert counters == built  # no builder re-ran

    # admin_key is PART of the cache key: the two auth channels (query key vs
    # session cookie) must never share an entry, because the rendered hrefs
    # embed (or omit) the key. Same params ± admin_key = rebuild.
    keyed = admin_core.page_html("view=overview&hours=24&admin_key=sekrit")
    assert "页面缓存" not in keyed
    assert counters["product"] == built["product"] + 2
    assert counters["usage"] == built["usage"] + 2
    # The channel split matters precisely because the pages differ: the
    # key-authed page embeds admin_key in its nav hrefs, the cookie page must not.
    assert "admin_key=sekrit" in keyed
    assert "admin_key" not in second

    # A different admin_key VALUE is also a different entry (no cross-admin
    # sharing), and the digest key never stores the secret in plaintext.
    keyed2 = admin_core.page_html("view=overview&hours=24&admin_key=other")
    assert "页面缓存" not in keyed2
    for key in admin_core._page_cache:
        assert re.fullmatch(r"[0-9a-f]{64}", key)
        assert "sekrit" not in key and "other" not in key

    # A different window is a different cache entry and rebuilds.
    other = admin_core.page_html("view=overview&hours=168")
    assert "页面缓存" not in other
    assert counters["product"] == built["product"] + 6
    assert counters["usage"] == built["usage"] + 6


def test_page_cache_key_matches_renderer_first_value_wins(monkeypatch):
    # Probe the reqctx contract the renderer actually reads: parse_qsl order
    # + setdefault, i.e. the FIRST occurrence of a duplicated param wins.
    from core.reqctx import bind, request

    with bind("hours=24&hours=168&view=overview"):
        assert request.args.get("hours") == "24"
    with bind("hours=168&hours=24&view=overview"):
        assert request.args.get("hours") == "168"

    # Same canonical dict (same first values, params merely reordered /
    # duplicated) -> ONE cache entry.
    k1 = admin_core._page_cache_key("view=overview&hours=24&hours=168")
    k2 = admin_core._page_cache_key("hours=24&view=overview&hours=168")
    # Different first value for the duplicate -> DIFFERENT entry, because the
    # renderer produces a different page (老 key 把两种顺序排序成同一个条目，
    # 第二种顺序就会拿到错的页).
    k3 = admin_core._page_cache_key("hours=168&hours=24&view=overview")
    assert k1 == k2
    assert k3 != k1

    counters: dict[str, int] = {}
    _stub_builders(monkeypatch, counters)

    first = admin_core.page_html("view=overview&hours=24&hours=168")
    assert "最近 24 小时" in first  # first-value-wins page
    built = dict(counters)

    # Reordered-but-equivalent query shares the entry: served from cache,
    # still the first-value-wins (24h) page.
    shared = admin_core.page_html("hours=24&view=overview&hours=168")
    assert "页面缓存" in shared
    assert "最近 24 小时" in shared
    assert counters == built

    # Swapped duplicate order renders 168h and must NOT be served the 24h page.
    other = admin_core.page_html("hours=168&hours=24&view=overview")
    assert "页面缓存" not in other
    assert "最近 168 小时" in other


def test_debug_view_bypasses_page_cache(monkeypatch):
    calls: list[str] = []

    def fake_build(query_string: str) -> str:
        calls.append(query_string)
        return "<main>debug reveal=plaintext</main>"

    monkeypatch.setattr(admin_core, "_build_page_html", fake_build)

    for _ in range(2):
        page = admin_core.page_html("view=debug&reveal=usr_123&admin_key=sekrit")
        assert "页面缓存" not in page
    assert len(calls) == 2  # rebuilt every time, never served from cache
    assert admin_core._page_cache == {}  # and never stored
    # Duplicate-param smuggling doesn't sneak debug into the cache either:
    # the canonical (first-value-wins) view is what's checked.
    admin_core.page_html("view=debug&view=dau")
    assert admin_core._page_cache == {}


def test_hard_retention_purges_old_entries_but_keeps_stale_window():
    now = time.monotonic()
    with admin_core._page_cache_lock:
        admin_core._page_cache["old"] = (
            now - admin_core._PAGE_CACHE_HARD_RETENTION_SEC - 100,
            "<main>ancient</main>",
        )
        admin_core._page_cache_builds["old"] = threading.Lock()
        # Between TTL and hard retention: must survive for stale-on-error.
        admin_core._page_cache["stale"] = (now - 120, "<main>stale</main>")

    # Any get sweeps hard-expired entries (and their locks) out.
    assert admin_core._page_cache_get("stale") is not None
    with admin_core._page_cache_lock:
        assert "old" not in admin_core._page_cache
        assert "old" not in admin_core._page_cache_builds
        assert "stale" in admin_core._page_cache

    # Any put sweeps too.
    with admin_core._page_cache_lock:
        admin_core._page_cache["old2"] = (
            now - admin_core._PAGE_CACHE_HARD_RETENTION_SEC - 1,
            "<main>ancient</main>",
        )
    admin_core._page_cache_put("fresh", time.monotonic(), "<main>fresh</main>")
    with admin_core._page_cache_lock:
        assert "old2" not in admin_core._page_cache
        assert {"stale", "fresh"} <= set(admin_core._page_cache)


def test_rebuild_failure_cooldown_and_lock_pruning(monkeypatch):
    calls: list[str] = []

    def failing_build(query_string: str) -> str:
        calls.append(query_string)
        raise RuntimeError("db down")

    monkeypatch.setattr(admin_core, "_build_page_html", failing_build)
    key = admin_core._page_cache_key("view=overview&hours=24")

    # First request attempts the rebuild and surfaces the real failure...
    with pytest.raises(RuntimeError, match="db down"):
        admin_core.page_html("view=overview&hours=24")
    assert len(calls) == 1
    with admin_core._page_cache_lock:
        # ...and with no cache entry to serve, the per-key lock entry is
        # pruned right there (no orphan locks piling up during an outage).
        assert key not in admin_core._page_cache_builds
        assert key in admin_core._page_cache_last_failure

    # Within the cooldown: no rebuild attempt at all, immediate fast failure
    # (no serial convoy of N x DB-timeout behind the build lock).
    with pytest.raises(RuntimeError, match="rebuild failed recently"):
        admin_core.page_html("view=overview&hours=24")
    assert len(calls) == 1

    # A stale entry within the cooldown is served without a rebuild attempt.
    with admin_core._page_cache_lock:
        admin_core._page_cache[key] = (
            time.monotonic() - admin_core._PAGE_CACHE_TTL_SEC - 5,
            "<main>stale page</main>",
        )
    stale = admin_core.page_html("view=overview&hours=24")
    assert "stale page" in stale
    assert "页面缓存" in stale
    assert len(calls) == 1

    # After the cooldown expires, a rebuild is attempted again.
    with admin_core._page_cache_lock:
        admin_core._page_cache_last_failure[key] = (
            time.monotonic() - admin_core._PAGE_CACHE_FAILURE_COOLDOWN_SEC - 1
        )
    served = admin_core.page_html("view=overview&hours=24")  # stale-on-error
    assert "stale page" in served
    assert len(calls) == 2


def test_overview_fanout_uses_shared_bounded_executor(monkeypatch):
    counters: dict[str, int] = {}
    _stub_builders(monkeypatch, counters)
    seen_threads: set[str] = set()
    real_chat = admin_core.jobs_store.recent_chat_reliability

    def chat_recording_thread(**kwargs):
        seen_threads.add(threading.current_thread().name)
        return real_chat(**kwargs)

    monkeypatch.setattr(
        admin_core.jobs_store, "recent_chat_reliability", chat_recording_thread
    )

    admin_core.page_html("view=overview&hours=24")
    first_executor = admin_core._ops_executor
    assert first_executor is not None
    assert first_executor._max_workers == 4
    assert all(name.startswith("admin-ops") for name in seen_threads)

    # A second build reuses the same module-level executor: it is never shut
    # down per request, so total builder concurrency stays process-wide.
    admin_core.page_html("view=overview&hours=168")
    assert admin_core._ops_executor is first_executor
    assert not first_executor._shutdown


def test_cache_note_is_styled_humanized_and_at_top_of_main(monkeypatch):
    counters: dict[str, int] = {}
    _stub_builders(monkeypatch, counters)

    admin_core.page_html("view=overview&hours=24")
    cached = admin_core.page_html("view=overview&hours=24")

    # Exactly one note, self-styled (the page templates ship no cache-note
    # CSS), sitting right after the opening <main> tag — top of the page.
    assert cached.count("cache-note") == 1
    main_idx = cached.find("<main")
    assert main_idx >= 0
    main_open_end = cached.find(">", main_idx)
    after_main = cached[main_open_end + 1 :]
    assert after_main.lstrip().startswith("<div class='cache-note'")
    note_idx = cached.find("cache-note")
    assert "style=" in cached[note_idx : note_idx + 300]
    assert "#f6f5f0" in cached and "#68706a" in cached

    # Humanized ages: seconds under 2 minutes, then minutes, then hours.
    assert re.search(r"数据生成于 \d+ 秒前", cached)
    assert "数据生成于 90 秒前" in admin_core._with_cache_note("<main></main>", 90)
    assert "数据生成于 5 分钟前" in admin_core._with_cache_note("<main></main>", 300)
    assert "数据生成于 3 小时前" in admin_core._with_cache_note(
        "<main></main>", 3600 * 3.5
    )
    # No <main>? The note is prepended, never dropped.
    assert admin_core._with_cache_note("<p>x</p>", 10).startswith("<div class='cache-note'")


def test_overview_html_unknown_pill_details_href_and_clean_logs(
    monkeypatch, caplog
):
    counters: dict[str, int] = {}
    # imports builder yields no evidence -> grey "unknown" pill, not yellow.
    _stub_builders(
        monkeypatch, counters, imports=lambda **_kwargs: None
    )

    secret = "sekrit-admin-key-123"
    with caplog.at_level(logging.INFO):
        page = admin_core.page_html(f"view=overview&hours=24&admin_key={secret}")
        cached = admin_core.page_html(
            f"view=overview&hours=24&admin_key={secret}"
        )

    assert "pill unknown" in page
    assert "证据不足" in page
    assert "<details" in page and "口径说明" in page
    # The imports question card is a link into the imports detail view.
    assert re.search(r"question-link' href='[^']*view=imports", page)
    assert "页面缓存" in cached
    # Timing/cache paths logged, and never with the admin_key in them.
    assert "[admin:perf]" in caplog.text
    assert secret not in caplog.text


def test_overview_fanout_isolates_single_builder_failure(monkeypatch):
    counters: dict[str, int] = {}

    def boom(**_kwargs):
        raise RuntimeError("chat query exploded")

    _stub_builders(monkeypatch, counters, chat=boom)

    page = admin_core.page_html("view=overview&hours=24")

    # The failed chat builder degrades its own card to "no evidence"...
    assert "聊天统计暂不可用" in page
    assert "延迟统计暂不可用" in page
    # ...while every other section still renders from its own builder.
    assert "用户导入成功了吗？" in page
    assert "窗口内 App 活跃账号" in page
    assert "V2 turns" in page
    assert "存活 worker" in page
    # And a failed build is never cached as a page.
    rebuilt = admin_core.page_html("view=overview&hours=24")
    assert "页面缓存" in rebuilt  # successful page WAS cached (degraded ≠ failed)
