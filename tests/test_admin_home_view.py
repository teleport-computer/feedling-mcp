"""Home view (new default) dispatch, verdict composition, verdicts JSON route.

Task C of the dashboard IA rework: admin_core's view dispatch ('' / 'home' /
unknown -> home, 'diag' hub, users-branch funnel injection), the system-verdict
worst-of composition (composed in admin_core from data_track's _ops_*_level
rulers), and GET /v1/admin/data-track/verdicts.

Two tiers:
  - Dispatch/composition tests always run: db builders AND data_track home
    renderers are monkeypatched (raising=False), so they hold before and after
    the parallel db.py / data_track.py tasks merge.
  - Rendered-HTML tests (pills / queue rows / funnel bars / sparkline / nav)
    exercise the REAL renderers and are skipped until data_track ships
    _render_home_page/_spark/_render_funnel — never stubbed, so they verify
    the merged renderer, not a test double.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db  # noqa: E402
from admin import admin_core  # noqa: E402
from admin import data_track as dt  # noqa: E402
from admin import routes_asgi as admin_asgi  # noqa: E402
from asgi import middleware  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from model_api_runtime.v2 import jobs_store  # noqa: E402

# Ops-view payload fixtures (import/chat/etc. report shapes) shared with the
# overview tests — the home system verdict is composed from these reports.
from test_data_track_ops_views import (  # noqa: E402
    _chat,
    _imports,
    _product,
    _runtime,
    _usage,
)

requires_pg = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="admin dashboard tests require the test PostgreSQL",
)

_HOME_RENDERERS = ("_render_home_page", "_spark", "_render_funnel")
_HOME_RENDERER_READY = all(hasattr(dt, name) for name in _HOME_RENDERERS)
requires_home_renderer = pytest.mark.skipif(
    not _HOME_RENDERER_READY,
    reason=(
        "data_track home renderers (_render_home_page/_spark/_render_funnel) "
        "not merged yet — dispatch-level coverage below still runs"
    ),
)

_ADMIN_TOKEN = "home-admin-test-token"


# --------------------------------------------------------------------------- #
# Contract-shaped stub payloads (THE FROZEN CONTRACT shapes, deterministic).
# --------------------------------------------------------------------------- #


def _queue() -> dict:
    return {
        "rows": [
            {
                "user_id": "usr_stuck1",
                "reason_code": "stalled_no_reply",
                "reason_text": "最后一条用户消息超过 30 分钟没有回复",
                "since_epoch": 1_754_000_000.0,
                "detail": "最后用户消息后没有 agent 非兜底回复",
            },
            {
                "user_id": "usr_stuck2",
                "reason_code": "onboarding_stuck",
                "reason_text": "注册超过 24 小时仍未拿到首次真回复",
                "since_epoch": 1_753_900_000.0,
                "detail": "t0 已设置、t3 缺失",
            },
        ],
        "truncated": False,
    }


def _pulse() -> dict:
    return {
        "daily_actives": [
            {"day": f"2026-07-2{i}", "dau": 3 + i} for i in range(3, 10)
        ][:7],
        "wau": 12,
        "prev_wau": 10,
        "latest_mature_w4": {"cohort_week": "2026-06-29", "pct": 0.25, "n": 8},
        "activation_recent": [
            {"cohort_week": "2026-07-27", "t3_rate": 0.5, "n": 4},
            {"cohort_week": "2026-07-20", "t3_rate": 0.4, "n": 5},
            {"cohort_week": "2026-07-13", "t3_rate": 0.6, "n": 5},
            {"cohort_week": "2026-07-06", "t3_rate": 0.5, "n": 6},
        ],
    }


def _feed() -> dict:
    return {
        "events": [
            {
                "epoch": 1_754_300_000.0,
                "kind": "registration",
                "user_id": "usr_new1",
                "text": "新用户注册",
            },
            {
                "epoch": 1_754_290_000.0,
                "kind": "first_reply",
                "user_id": "usr_new2",
                "text": "拿到首次真回复",
            },
            {
                "epoch": 1_754_280_000.0,
                "kind": "import_failed",
                "user_id": "usr_new3",
                "text": "记忆导入失败 2 次",
            },
        ]
    }


def _cost() -> dict:
    return {
        "daily_tokens": [
            {"day": f"2026-07-2{i}", "tokens": 10_000 * (i + 1)} for i in range(3, 10)
        ][:7],
        "today_so_far": 12_345,
        "per_active_user_day": 3_500.5,
        "runaway": False,
        "coverage": 0.97,
    }


_EVIDENCE_GAPS = ["客户端已读 ACK 未埋点", "session 来源标记未埋点"]


def _soft_verdicts() -> dict:
    return {
        "growth": {"level": "ok", "reasons": []},
        "cost": {"level": "ok", "reasons": []},
        # evidence 永远灰：结构性证据缺口未闭合前绝不许绿。
        "evidence": {"level": "unknown", "reasons": list(_EVIDENCE_GAPS)},
    }


def _funnel() -> dict:
    return {
        "stages": [
            {"id": "registered", "label": "注册", "count": 20},
            {"id": "connected", "label": "已连接(t1)", "count": 15},
            {"id": "content_ready", "label": "内容就绪(t2)", "count": 12},
            {"id": "first_reply", "label": "首次真回复(t3)", "count": 9},
            {"id": "w1_retained", "label": "W1 仍活跃", "count": 4},
        ],
        "window_days": 28,
        "prev": None,
    }


_HOME_BUILDER_STUBS = {
    "queue": (db, "admin_home_queue", _queue),
    "pulse": (db, "admin_home_pulse", _pulse),
    "feed": (db, "admin_home_feed", _feed),
    "cost": (db, "admin_home_cost", _cost),
    "soft_verdicts": (db, "admin_home_soft_verdicts", _soft_verdicts),
    "funnel": (db, "admin_funnel_snapshot", _funnel),
    "imports": (db, "recent_genesis_import_health", _imports),
    "chat": (jobs_store, "recent_chat_reliability", _chat),
}


def _stub_home_builders(monkeypatch, counters, **overrides):
    # raising=False：db 侧的 admin_home_* builders 属于并行任务，合并前这些
    # 属性还不存在；合并后 monkeypatch 照常替换真实现（stub 数据是口径固定
    # 的契约形状，两种情况下测的都是 admin_core 的分发与合成逻辑）。
    def make(name, payload_fn):
        def fn(**_kwargs):
            counters[name] = counters.get(name, 0) + 1
            return payload_fn()

        return fn

    for name, (module, attr, payload_fn) in _HOME_BUILDER_STUBS.items():
        monkeypatch.setattr(
            module, attr, overrides.get(name) or make(name, payload_fn), raising=False
        )


class _HomeRenderCapture:
    """Stand-in for data_track._render_home_page recording the contract args."""

    MARKER = "<main>HOME_STUB_PAGE</main>"

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, system_verdict, soft_verdicts, queue, pulse, feed, cost, funnel):
        self.calls.append(
            {
                "system_verdict": system_verdict,
                "soft_verdicts": soft_verdicts,
                "queue": queue,
                "pulse": pulse,
                "feed": feed,
                "cost": cost,
                "funnel": funnel,
            }
        )
        return self.MARKER


def _stub_home_renderer(monkeypatch) -> _HomeRenderCapture:
    capture = _HomeRenderCapture()
    monkeypatch.setattr(dt, "_render_home_page", capture, raising=False)
    return capture


# --------------------------------------------------------------------------- #
# Dispatch: '' / 'home' / unknown -> home (the NEW default); diag hub; users.
# --------------------------------------------------------------------------- #


def test_default_home_alias_and_unknown_views_dispatch_home(monkeypatch):
    counters: dict[str, int] = {}
    _stub_home_builders(monkeypatch, counters)
    capture = _stub_home_renderer(monkeypatch)

    bare = admin_core.page_html("")
    assert _HomeRenderCapture.MARKER in bare
    # All 8 home builders ran exactly once for the bare default page.
    assert {name: counters.get(name) for name in _HOME_BUILDER_STUBS} == {
        name: 1 for name in _HOME_BUILDER_STUBS
    }

    named = admin_core.page_html("view=home")
    unknown = admin_core.page_html("view=definitely_not_a_view")
    assert _HomeRenderCapture.MARKER in named
    assert _HomeRenderCapture.MARKER in unknown
    assert len(capture.calls) == 3

    # Renderer received the contract payloads verbatim, in positional order.
    call = capture.calls[0]
    assert call["queue"] == _queue()
    assert call["pulse"] == _pulse()
    assert call["feed"] == _feed()
    assert call["cost"] == _cost()
    assert call["soft_verdicts"] == _soft_verdicts()
    assert call["funnel"] == _funnel()
    assert set(call["system_verdict"]) == {"level", "reasons"}

    # Documented cache aliasing: '' and view=home are DIFFERENT cache keys
    # serving identical content (accepted: 60s TTL, capped entries, one page
    # string of overhead — simpler than view-alias normalisation in the key).
    assert admin_core._page_cache_key("") != admin_core._page_cache_key("view=home")
    with admin_core._page_cache_lock:
        assert len(admin_core._page_cache) == 3  # '', home, unknown all cached

    # Second hit within TTL: served from cache with the honesty note, no
    # builder re-runs.
    built = dict(counters)
    again = admin_core.page_html("")
    assert "页面缓存" in again
    assert counters == built


def test_home_fanout_runs_on_shared_bounded_executor(monkeypatch):
    counters: dict[str, int] = {}
    seen_threads: set[str] = set()

    def queue_recording_thread(**_kwargs):
        seen_threads.add(threading.current_thread().name)
        counters["queue"] = counters.get("queue", 0) + 1
        return _queue()

    _stub_home_builders(monkeypatch, counters, queue=queue_recording_thread)
    _stub_home_renderer(monkeypatch)

    admin_core.page_html("view=home")
    executor = admin_core._ops_executor
    assert executor is not None
    assert executor._max_workers == 4
    assert seen_threads and all(n.startswith("admin-ops") for n in seen_threads)

    # Same process-wide executor on a second (different-key) build.
    admin_core.page_html("view=home&again=1")
    assert admin_core._ops_executor is executor
    assert not executor._shutdown


def test_home_single_builder_failure_isolates_and_page_still_cached(monkeypatch):
    counters: dict[str, int] = {}

    def boom(**_kwargs):
        raise RuntimeError("queue query exploded")

    _stub_home_builders(monkeypatch, counters, queue=boom)
    capture = _stub_home_renderer(monkeypatch)

    page = admin_core.page_html("view=home")
    assert _HomeRenderCapture.MARKER in page
    call = capture.calls[-1]
    # Failed builder -> None (renderer shows 暂不可用, never a fabricated 0)…
    assert call["queue"] is None
    # …while every other section's payload arrived intact.
    for name in ("pulse", "feed", "cost", "soft_verdicts", "funnel"):
        assert call[name] is not None
    assert call["system_verdict"]["level"] in {"ok", "warn", "bad", "unknown"}

    # Degraded ≠ failed: the page WAS cached.
    built = dict(counters)
    again = admin_core.page_html("view=home")
    assert "页面缓存" in again
    assert counters == built


def test_all_builders_none_still_renders_with_unknown_system(monkeypatch):
    counters: dict[str, int] = {}
    nones = {name: (lambda **_kwargs: None) for name in _HOME_BUILDER_STUBS}
    _stub_home_builders(monkeypatch, counters, **nones)
    capture = _stub_home_renderer(monkeypatch)

    page = admin_core.page_html("view=home&case=all-none")
    assert _HomeRenderCapture.MARKER in page
    call = capture.calls[-1]
    for name in ("queue", "pulse", "feed", "cost", "soft_verdicts", "funnel"):
        assert call[name] is None
    # No evidence at all -> the composed system verdict is unknown (grey),
    # never ok: _ops_import_level(None)/_ops_chat_level(None) both say so.
    assert call["system_verdict"]["level"] == "unknown"
    assert call["system_verdict"]["reasons"]


def test_users_view_injects_funnel_into_renderer(monkeypatch):
    counters: dict[str, int] = {}
    _stub_home_builders(monkeypatch, counters)

    sentinel_payload = {"summary": {"sentinel": True}, "users": []}
    monkeypatch.setattr(
        dt, "_data_track_payload", lambda include_users=True: sentinel_payload
    )

    captured: dict = {}

    def fake_users_page(payload, *, funnel=None):
        captured["payload"] = payload
        captured["funnel"] = funnel
        return "<main>USERS_STUB_PAGE</main>"

    monkeypatch.setattr(dt, "_render_data_track_page", fake_users_page)

    page = admin_core.page_html("view=users")
    assert "USERS_STUB_PAGE" in page
    assert captured["payload"] is sentinel_payload
    assert captured["funnel"] == _funnel()
    assert counters.get("funnel") == 1
    # users 不再是默认页：只有显式 view=users 才走这条分支（默认页在上面的
    # 测试里已被证明是 home）。


def test_users_view_funnel_failure_degrades_to_none(monkeypatch):
    counters: dict[str, int] = {}

    def boom(**_kwargs):
        raise RuntimeError("funnel query exploded")

    _stub_home_builders(monkeypatch, counters, funnel=boom)
    monkeypatch.setattr(
        dt, "_data_track_payload", lambda include_users=True: {"summary": {}}
    )
    captured: dict = {}

    def fake_users_page(payload, *, funnel=None):
        captured["funnel"] = funnel
        return "<main>USERS_STUB_PAGE</main>"

    monkeypatch.setattr(dt, "_render_data_track_page", fake_users_page)

    admin_core.page_html("view=users&case=funnel-boom")
    assert captured["funnel"] is None  # raise -> None -> 暂不可用, not fake bars


def test_diag_view_dispatches_hub(monkeypatch):
    monkeypatch.setattr(
        dt, "_render_diag_hub_page", lambda: "<main>DIAG_HUB_STUB</main>", raising=False
    )
    page = admin_core.page_html("view=diag")
    assert "DIAG_HUB_STUB" in page


# --------------------------------------------------------------------------- #
# System verdict: worst-of composition over the existing _ops_*_level rulers.
# --------------------------------------------------------------------------- #


def _force_levels(monkeypatch, imports_lvl, chat_lvl, latency_lvl):
    monkeypatch.setattr(dt, "_ops_import_level", lambda _r: imports_lvl)
    monkeypatch.setattr(dt, "_ops_chat_level", lambda _r: chat_lvl)
    monkeypatch.setattr(dt, "_ops_latency_level", lambda _r: latency_lvl)


def test_system_verdict_is_worst_of_with_merged_reasons(monkeypatch):
    # The spec case: import=bad chat=ok latency=warn -> bad, reasons merged.
    _force_levels(
        monkeypatch,
        ("bad", ["导入终态失败率 25.0%"]),
        ("ok", []),
        ("warn", ["服务端交付 p95 90s"]),
    )
    verdict = admin_core.compose_system_verdict({}, {})
    assert verdict == {
        "level": "bad",
        "reasons": ["导入终态失败率 25.0%", "服务端交付 p95 90s"],
    }


def test_system_verdict_level_ordering_and_dedup(monkeypatch):
    # bad > warn > unknown > ok — unknown outranks ok (no evidence is not
    # good news) but never outranks a measured warn.
    _force_levels(monkeypatch, ("unknown", ["没样本"]), ("ok", []), ("ok", []))
    assert admin_core.compose_system_verdict({}, {})["level"] == "unknown"

    _force_levels(monkeypatch, ("unknown", ["没样本"]), ("warn", ["测出问题"]), ("ok", []))
    assert admin_core.compose_system_verdict({}, {})["level"] == "warn"

    _force_levels(monkeypatch, ("ok", []), ("ok", []), ("ok", []))
    assert admin_core.compose_system_verdict({}, {}) == {"level": "ok", "reasons": []}

    # Unrecognized level defends to warn — a typo'd ruler never reads as ok.
    _force_levels(monkeypatch, ("purple", ["咦"]), ("ok", []), ("ok", []))
    assert admin_core.compose_system_verdict({}, {})["level"] == "warn"

    # chat and latency read the SAME report; identical reasons dedup'd in order.
    _force_levels(
        monkeypatch, ("warn", ["同一句话"]), ("warn", ["同一句话"]), ("ok", [])
    )
    assert admin_core.compose_system_verdict({}, {})["reasons"] == ["同一句话"]


def test_system_verdict_from_real_rulers_over_fixture_reports():
    # No monkeypatching: run the real _ops_*_level over the shared fixtures.
    # _imports() carries a 25% terminal failure rate -> bad; merged reasons
    # keep chat's structural-ACK caveat.
    verdict = admin_core.compose_system_verdict(_imports(), _chat())
    assert verdict["level"] == "bad"
    assert any("失败率" in reason for reason in verdict["reasons"])


# --------------------------------------------------------------------------- #
# verdicts_payload + GET /v1/admin/data-track/verdicts (JSON, not page-cached)
# --------------------------------------------------------------------------- #


def test_verdicts_payload_contract_shape(monkeypatch):
    counters: dict[str, int] = {}
    _stub_home_builders(monkeypatch, counters)

    payload = admin_core.verdicts_payload("")

    datetime.fromisoformat(payload["generated_at"])  # ISO or raise
    assert set(payload["verdicts"]) == {"system", "growth", "cost", "evidence"}
    for verdict in payload["verdicts"].values():
        assert verdict["level"] in {"ok", "warn", "bad", "unknown"}
        assert isinstance(verdict["reasons"], list)
    # evidence is ALWAYS grey while the standing gaps are open — passed
    # through from the soft-verdict builder untouched, never upgraded.
    assert payload["verdicts"]["evidence"]["level"] == "unknown"
    assert payload["verdicts"]["evidence"]["reasons"] == _EVIDENCE_GAPS
    # system composed from the same rulers as the home page (fixture -> bad).
    assert payload["verdicts"]["system"]["level"] == "bad"
    assert payload["queue"] == _queue()
    assert payload["pulse"] == _pulse()
    # feed/cost/funnel are page-only; the JSON endpoint runs 5 builders.
    assert counters.get("feed") is None
    assert counters.get("cost") is None
    assert counters.get("funnel") is None

    # NOT page-cached (JSON has no cache-note channel to declare data age):
    # a second call re-runs every builder.
    admin_core.verdicts_payload("")
    assert counters["queue"] == 2
    assert counters["imports"] == 2


def test_verdicts_payload_failure_honesty(monkeypatch):
    counters: dict[str, int] = {}

    def boom(**_kwargs):
        raise RuntimeError("builder exploded")

    _stub_home_builders(monkeypatch, counters, queue=boom, soft_verdicts=boom)
    payload = admin_core.verdicts_payload("")

    # A failed queue builder yields null — an empty rows list would assert
    # 「没有人卡住」, which a failed query cannot honestly claim.
    assert payload["queue"] is None
    # Missing soft verdicts degrade to grey with a reason, never to green.
    for name in ("growth", "cost", "evidence"):
        assert payload["verdicts"][name] == {
            "level": "unknown",
            "reasons": ["软性判定暂不可用"],
        }
    # system still composes from its own (healthy) builders.
    assert payload["verdicts"]["system"]["level"] == "bad"


def _build_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    middleware.register_exception_handlers(app)
    admin_asgi.register_asgi(app)
    return app


_APP = _build_app()


def _asgi_get(path: str, headers: dict | None = None) -> httpx.Response:
    async def go():
        transport = httpx.ASGITransport(app=_APP)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            return await client.get(path, headers=headers or {})

    return asyncio.run(go())


def test_verdicts_route_admin_gated_shape_and_clean_logs(monkeypatch, caplog):
    monkeypatch.setenv("FEEDLING_ADMIN_TOKEN", _ADMIN_TOKEN)
    counters: dict[str, int] = {}
    _stub_home_builders(monkeypatch, counters)

    # Gated exactly like its siblings: no token -> 401, bad token -> 401.
    assert _asgi_get("/v1/admin/data-track/verdicts").status_code == 401
    assert (
        _asgi_get(
            "/v1/admin/data-track/verdicts", headers={"X-Admin-Token": "wrong"}
        ).status_code
        == 401
    )

    with caplog.at_level(logging.INFO):
        via_header = _asgi_get(
            "/v1/admin/data-track/verdicts", headers={"X-Admin-Token": _ADMIN_TOKEN}
        )
        via_query = _asgi_get(
            f"/v1/admin/data-track/verdicts?admin_key={_ADMIN_TOKEN}"
        )
    assert via_header.status_code == 200
    assert via_query.status_code == 200
    body = via_header.json()
    assert set(body) == {"generated_at", "verdicts", "queue", "pulse"}
    assert set(body["verdicts"]) == {"system", "growth", "cost", "evidence"}
    assert body["verdicts"]["evidence"]["level"] == "unknown"
    assert body["queue"]["rows"][0]["user_id"] == "usr_stuck1"
    assert body["queue"]["truncated"] is False
    assert body["pulse"]["wau"] == 12

    # Builder timings are logged; the admin key never is. Filter out httpx's
    # own CLIENT-side request-URL log line — that's this test's plumbing, not
    # server logging (the rule under test: the server never logs the key).
    server_log = "\n".join(
        record.getMessage()
        for record in caplog.records
        if not record.name.startswith("httpx")
    )
    assert "[admin:perf]" in server_log
    assert _ADMIN_TOKEN not in server_log


def test_unconfigured_admin_token_is_503(monkeypatch):
    monkeypatch.delenv("FEEDLING_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("FEEDLING_RUNTIME_TOKEN_SECRET", raising=False)
    assert _asgi_get("/v1/admin/data-track/verdicts").status_code == 503


def test_home_page_logs_never_contain_admin_key(monkeypatch, caplog):
    counters: dict[str, int] = {}
    _stub_home_builders(monkeypatch, counters)
    _stub_home_renderer(monkeypatch)

    secret = "home-sekrit-key-42"
    with caplog.at_level(logging.INFO):
        admin_core.page_html(f"view=home&admin_key={secret}")
    assert "[admin:perf]" in caplog.text
    assert secret not in caplog.text
    # And the digest cache never stores the plaintext secret either.
    for key in admin_core._page_cache:
        assert re.fullmatch(r"[0-9a-f]{64}", key)
        assert secret not in key


# --------------------------------------------------------------------------- #
# Legacy views: every named view keeps working unchanged.
# --------------------------------------------------------------------------- #


def _stub_overview_builders(monkeypatch, counters):
    def make(name, payload_fn):
        def fn(**_kwargs):
            counters[name] = counters.get(name, 0) + 1
            return payload_fn()

        return fn

    for name, (module, attr, payload_fn) in {
        "imports": (db, "recent_genesis_import_health", _imports),
        "chat": (jobs_store, "recent_chat_reliability", _chat),
        "runtime": (jobs_store, "recent_runtime_health", _runtime),
        "product": (db, "recent_admin_product_kpis", _product),
        "usage": (jobs_store, "recent_token_usage_by_lane", _usage),
    }.items():
        monkeypatch.setattr(module, attr, make(name, payload_fn))


def test_legacy_overview_and_chat_views_still_render(monkeypatch):
    counters: dict[str, int] = {}
    _stub_overview_builders(monkeypatch, counters)

    overview = admin_core.page_html("view=overview&hours=24")
    assert "窗口内 App 活跃账号" in overview
    assert re.search(r"question-link' href='[^']*view=imports", overview)

    chat_page = admin_core.page_html("view=chat&hours=24")
    assert "chat" in chat_page.lower()
    assert "<main" in chat_page


_HEALTH_BUILDERS = (
    "admin_product_health_weekly_cohort_retention",
    "admin_product_health_activation_weekly",
    "admin_product_health_w4_split",
    "admin_product_health_stickiness",
    "admin_product_health_concentration",
    "admin_product_health_growth_accounting_weekly",
    "admin_product_health_power_users",
    "admin_product_health_proactive_reply_rate",
)


def test_legacy_health_view_still_renders_with_honest_degradation(monkeypatch):
    # All health builders raising -> every section 暂不可用, page still 200.
    def boom(**_kwargs):
        raise RuntimeError("health query exploded")

    for name in _HEALTH_BUILDERS:
        monkeypatch.setattr(db, name, boom)
    page = admin_core.page_html("view=health")
    assert "暂不可用" in page
    # RAISE -> None -> 暂不可用/—, never a fabricated healthy zero like "0%".
    assert "<main" in page


def test_legacy_debug_view_dispatch_unchanged(monkeypatch):
    sentinel = {"debug": True}
    monkeypatch.setattr(dt, "_data_track_debug_payload", lambda: sentinel)
    captured: dict = {}

    def fake_debug_page(payload):
        captured["payload"] = payload
        return "<main>DEBUG_STUB_PAGE</main>"

    monkeypatch.setattr(dt, "_render_data_track_debug_page", fake_debug_page)
    page = admin_core.page_html("view=debug")
    assert "DEBUG_STUB_PAGE" in page
    assert captured["payload"] is sentinel
    # debug 仍然整体绕过页缓存（可能带 reveal 明文）。
    assert admin_core._page_cache_key("view=debug") not in admin_core._page_cache


# --------------------------------------------------------------------------- #
# Rendered HTML (REAL data_track home renderers — skipped until Task B merges;
# these are deliberately never stubbed so they verify the merged renderer).
# --------------------------------------------------------------------------- #


@requires_home_renderer
def test_home_page_four_pills_with_levels_and_evidence_always_grey(monkeypatch):
    counters: dict[str, int] = {}
    _stub_home_builders(monkeypatch, counters)
    _force_levels(monkeypatch, ("ok", []), ("ok", []), ("ok", []))

    page = admin_core.page_html("view=home&case=pills-ok")
    for label in ("系统", "增长", "成本", "数据完整性"):
        assert label in page
    assert "pill ok" in page
    # evidence is unknown in the stub (and by contract can NEVER be ok while
    # the standing gaps are open): grey pill, with the gaps as reasons.
    assert "pill unknown" in page
    for gap in _EVIDENCE_GAPS:
        assert gap in page

    _force_levels(monkeypatch, ("bad", ["导入炸了"]), ("ok", []), ("ok", []))
    page_bad = admin_core.page_html("view=home&case=pills-bad")
    assert "pill bad" in page_bad
    assert "导入炸了" in page_bad


@requires_home_renderer
def test_home_queue_rows_empty_state_and_failed_queue_honesty(monkeypatch):
    counters: dict[str, int] = {}
    _stub_home_builders(monkeypatch, counters)
    page = admin_core.page_html("view=home&case=queue-rows")
    # Queue rows deep-link into the per-user detail page.
    assert "/admin/data-track/users/usr_stuck1" in page
    assert "usr_stuck2" in page

    _stub_home_builders(
        monkeypatch, counters,
        queue=lambda **_kwargs: {"rows": [], "truncated": False},
    )
    empty = admin_core.page_html("view=home&case=queue-empty")
    # The element (not the stylesheet's .queue-empty selector, which is
    # always shipped) — class attribute means the empty state rendered.
    assert "class='queue-empty'" in empty
    assert "没有人卡住" in empty

    def boom(**_kwargs):
        raise RuntimeError("queue query exploded")

    _stub_home_builders(monkeypatch, counters, queue=boom)
    failed = admin_core.page_html("view=home&case=queue-failed")
    # A failed queue query may NOT claim the empty state — that would be a
    # fabricated 「没有人卡住」. (The renderer's honesty note QUOTES the phrase
    # while explaining it won't claim it, so assert on the element, not the
    # raw string, and on the element, not the always-shipped CSS selector.)
    assert "class='queue-empty'" not in failed
    assert "队列暂不可用" in failed
    # …but every other section still renders (single-failure isolation).
    assert "usr_new1" in failed  # feed
    assert "注册" in failed  # funnel stage label


@requires_home_renderer
def test_home_funnel_sparkline_feed_and_kouj_note(monkeypatch):
    counters: dict[str, int] = {}
    _stub_home_builders(monkeypatch, counters)
    page = admin_core.page_html("view=home&case=body")

    # Funnel: monotonic stages render in order with their counts.
    labels = ["注册", "已连接(t1)", "内容就绪(t2)", "首次真回复(t3)", "W1 仍活跃"]
    positions = [page.find(label) for label in labels]
    assert all(pos >= 0 for pos in positions)
    assert positions == sorted(positions)

    # Sparkline: inline SVG polyline, no JS.
    assert "<svg" in page
    assert "polyline" in page
    assert "<script" not in page.lower()  # sparkline 是纯 SVG，不许上 JS

    # Feed renders the 48h events.
    assert "usr_new1" in page

    # One collapsed 口径说明 at the foot of the page.
    assert "<details" in page
    assert "口径说明" in page


@requires_home_renderer
def test_spark_and_funnel_helpers_are_none_safe():
    spark = dt._spark([1, None, 3, 2])
    assert "<svg" in spark and "polyline" in spark
    dt._spark([])  # must not raise on empty history
    dt._spark([None, None])  # nor on all-gaps

    missing = dt._render_funnel(None, compact=True)
    assert "暂不可用" in missing


@requires_home_renderer
def test_nav_four_primary_on_home_and_diag_row_on_diagnostic_view(monkeypatch):
    counters: dict[str, int] = {}
    _stub_home_builders(monkeypatch, counters)
    home = admin_core.page_html("view=home&case=nav")
    for label in ("首页", "产品健康", "用户", "诊断"):
        assert label in home

    # A diagnostic view shows the second row listing the 11 legacy views.
    _stub_overview_builders(monkeypatch, counters)
    chat_page = admin_core.page_html("view=chat&hours=24&case=nav")
    for label in ("首页", "记忆导入", "调试", "日活与时长"):
        assert label in chat_page


_USERS_FUNNEL_READY = _HOME_RENDERER_READY and (
    "funnel" in inspect.signature(dt._render_data_track_page).parameters
)


@pytest.mark.skipif(
    not _USERS_FUNNEL_READY,
    reason="_render_data_track_page funnel kwarg not merged yet",
)
@requires_pg
def test_users_page_renders_funnel_over_real_payload(monkeypatch):
    counters: dict[str, int] = {}
    _stub_home_builders(monkeypatch, counters)  # funnel snapshot stubbed
    page = admin_core.page_html("view=users")
    assert "首次真回复(t3)" in page  # funnel stage rendered on the users page
    assert counters.get("funnel") == 1


# --------------------------------------------------------------------------- #
# 2026-08-05 adversarial-review 修复回归：真 builder / 真渲染层的口径测试。
# 前缀 u_hmfx_ 的种子行进出都清，不污染同 session 的其他用例。
# --------------------------------------------------------------------------- #

import json as _json  # noqa: E402
import uuid as _uuid  # noqa: E402
from datetime import timedelta, timezone as _tz  # noqa: E402
from zoneinfo import ZoneInfo as _ZoneInfo  # noqa: E402


@pytest.fixture()
def clean_hmfx_rows():
    if not os.environ.get("DATABASE_URL"):
        yield
        return

    def clean() -> None:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM user_logs WHERE user_id LIKE 'u_hmfx_%'")
            conn.execute("DELETE FROM chat_messages WHERE user_id LIKE 'u_hmfx_%'")
            conn.execute("DELETE FROM v2_turn_metrics WHERE user_id LIKE 'u_hmfx_%'")
            conn.execute("DELETE FROM users WHERE user_id LIKE 'u_hmfx_%'")

    clean()
    try:
        yield
    finally:
        clean()


def _hmfx_user(conn, uid: str, created_epoch: float) -> None:
    conn.execute(
        "INSERT INTO users (user_id, created_at, doc) VALUES (%s, %s, '{}')",
        (uid,
         datetime.fromtimestamp(created_epoch, _tz.utc).strftime("%Y-%m-%dT%H:%M:%S")),
    )


def _hmfx_msg(conn, uid: str, ts: float, role: str, source: str | None = None) -> None:
    doc = {"role": role}
    if source is not None:
        doc["source"] = source
    conn.execute(
        "INSERT INTO chat_messages (user_id, msg_id, ts, doc) VALUES (%s, %s, %s, %s)",
        (uid, _uuid.uuid4().hex, ts, _json.dumps(doc)),
    )


@requires_pg
def test_queue_stalled_not_cleared_by_agent_initiated_proactive(clean_hmfx_rows):
    """定时主动消息不是「对用户的回复」（与 admin_ops_dashboard real_replies
    同一把尺）：它落下来不许把还在等真回复的用户从队列里洗掉。"""
    now = time.time()
    with db.get_pool().connection() as conn:
        # 用户 2h 前发问，1h 前只来了一条 agent_initiated_proactive —— 仍卡住。
        _hmfx_user(conn, "u_hmfx_masked", now - 5 * 86400)
        _hmfx_msg(conn, "u_hmfx_masked", now - 2 * 3600, "user")
        _hmfx_msg(conn, "u_hmfx_masked", now - 1 * 3600, "agent",
                  "agent_initiated_proactive")
        # 对照：同样 2h 前发问，1h 前收到真回复 —— 不卡。
        _hmfx_user(conn, "u_hmfx_fine", now - 5 * 86400)
        _hmfx_msg(conn, "u_hmfx_fine", now - 2 * 3600, "user")
        _hmfx_msg(conn, "u_hmfx_fine", now - 1 * 3600, "agent")
    codes = {r["user_id"]: r["reason_code"]
             for r in db.admin_home_queue()["rows"]}
    assert codes.get("u_hmfx_masked") == "stalled_no_reply"
    assert "u_hmfx_fine" not in codes


@requires_pg
def test_cost_verdict_grey_not_green_when_runaway_unjudgeable(clean_hmfx_rows):
    """可判定完整日 <4 → runaway=None → 灰 unknown。绿灯配「判不了」的理由
    是自相矛盾（每次新部署头几天都会走到这）——诚实分层：没证据是灰。"""
    now_dt = datetime.now(_tz.utc)
    with db.get_pool().connection() as conn:
        _hmfx_user(conn, "u_hmfx_cost", time.time() - 90 * 86400)
        for days_ago in (1, 2):  # 只有 2 个有上报的完整日
            for _ in range(3):
                conn.execute(
                    "INSERT INTO v2_turn_metrics (user_id, lane, prompt_tokens,"
                    " completion_tokens, latency_ms, created_at)"
                    " VALUES ('u_hmfx_cost','chat',100,50,900,%s)",
                    (now_dt - timedelta(days=days_ago),),
                )
    cost = db.admin_home_cost()
    assert cost["runaway"] is None
    assert cost["coverage"] == 1.0
    verdict = db.admin_home_soft_verdicts()["cost"]
    assert verdict["level"] == "unknown"
    assert any("可判定" in r for r in verdict["reasons"])


@requires_pg
def test_funnel_w1_settles_to_zero_for_mature_dead_cohort(clean_hmfx_rows):
    """cohort 已成熟（t0+14d 已过）但没人到 first_reply：w1_retained 是测得
    的 0，不是「窗口没走完」的 None——灰掉测得的全灭等于把激活失败说成
    不成熟。"""
    now = time.time()
    with db.get_pool().connection() as conn:
        _hmfx_user(conn, "u_hmfx_dead", now - 20 * 86400)  # W1 窗早已走完
        conn.execute(
            "INSERT INTO user_logs (user_id, stream, ts, doc)"
            " VALUES (%s,%s,%s,%s)",
            ("u_hmfx_dead", "tracking_events", now - 10 * 86400,
             _json.dumps({"type": "app_session_end",
                          "payload": {"duration_sec": 60}})),
        )
    stages = {s["id"]: s["count"] for s in db.admin_funnel_snapshot()["stages"]}
    assert stages["registered"] >= 1
    assert stages["first_reply"] == 0
    assert stages["w1_retained"] == 0  # 定局的 0，不是 None/—


@requires_pg
def test_growth_verdict_small_baseline_is_unknown(clean_hmfx_rows):
    """小基线护栏：prev_wau=5→wau=3（掉 40%）在 intentionally tiny 舰队上是
    单人级噪声，不许点红黄——unknown + 原始人数。"""
    now = time.time()
    with db.get_pool().connection() as conn:
        for i in range(5):
            _hmfx_user(conn, f"u_hmfx_w{i}", now - 90 * 86400)

        def _session(uid: str, ts: float) -> None:
            conn.execute(
                "INSERT INTO user_logs (user_id, stream, ts, doc)"
                " VALUES (%s,%s,%s,%s)",
                (uid, "tracking_events", ts,
                 _json.dumps({"type": "app_session_end",
                              "payload": {"duration_sec": 60}})),
            )

        _session("u_hmfx_w0", now - 20 * 86400)  # 保证埋点史覆盖对比窗
        for i in range(5):                       # 上一窗 5 人活跃
            _session(f"u_hmfx_w{i}", now - 10 * 86400)
        for i in range(3):                       # 本窗 3 人活跃
            _session(f"u_hmfx_w{i}", now - 2 * 86400)
    pulse = db.admin_home_pulse()
    assert pulse["prev_wau"] == 5 and pulse["wau"] == 3
    verdict = db.admin_home_soft_verdicts()["growth"]
    assert verdict["level"] == "unknown"
    assert any("基线过小" in r for r in verdict["reasons"])


@requires_home_renderer
def test_home_activation_headline_skips_in_progress_week(monkeypatch):
    """进行中的北京周右删失塌向 0%——头条只取已完整的周，进行中的周在折线
    上留缺口（与 db 侧 growth 判定剔除 this_monday 同一把尺）。"""
    this_monday = dt._health_bj_this_monday()

    def pulse_with_inprogress(**_kwargs):
        payload = _pulse()
        payload["activation_recent"] = [
            {"cohort_week": this_monday, "t3_rate": 0.0, "n": 2},
            {"cohort_week": "2026-07-27", "t3_rate": 0.5, "n": 4},
            {"cohort_week": "2026-07-20", "t3_rate": 0.4, "n": 5},
            {"cohort_week": "2026-07-13", "t3_rate": 0.6, "n": 5},
        ]
        return payload

    counters: dict[str, int] = {}
    _stub_home_builders(monkeypatch, counters, pulse=pulse_with_inprogress)
    page = admin_core.page_html("view=home&case=act-headline")
    assert "注册周 2026-07-27" in page          # 头条来自最新完整周
    assert f"注册周 {this_monday}" not in page  # 进行中的周不当定局


@requires_home_renderer
def test_home_cost_section_survives_non_finite_per_active(monkeypatch):
    """契约健壮性：per_active_user_day 为 inf/NaN 时该格降级成 —，绝不带崩
    整页（首页是裸 URL 默认页）。"""

    def cost_inf(**_kwargs):
        payload = _cost()
        payload["per_active_user_day"] = float("inf")
        return payload

    counters: dict[str, int] = {}
    _stub_home_builders(monkeypatch, counters, cost=cost_inf)
    page = admin_core.page_html("view=home&case=cost-inf")
    assert "每活跃用户日<b>—</b>" in page
    assert "今日已用" in page  # 其余成本格照常渲染


@requires_home_renderer
def test_invalid_uid_back_link_returns_to_users_view():
    # UID 直查表单长在用户页；默认视图切成首页后，返回链接必须显式带
    # view=users，不许把人丢回首页。
    page = dt._render_invalid_data_track_user_page("lol/../etc")
    assert "view=users" in page
