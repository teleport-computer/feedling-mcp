"""Framework-neutral entry points for the native ASGI admin data-track routes.

The ``admin.data_track`` helpers read their query parameters from a request
proxy (``request.args``) deep inside ``_data_track_payload`` /
``_data_track_request_filters`` / ``_data_track_qs`` (the HTML pages embed
``admin_key``/``since``/``sort``/… in their hrefs). To run that logic without
forking it, each entry point binds a neutral, flask-free request context
(``core.reqctx.bind``) built from the ASGI request's raw query string, so the
identical ``data_track`` code path executes off the event loop.

Every entry point is blocking (sync ``db.py`` under the hood) and must be invoked
via ``asgi.threadpool.run_db`` from the async routes.

``page_html`` is the single HTML dispatcher for every ``view=`` (首页 home/
诊断枢纽 diag/运营总览/产品健康/imports/chat/latency/runtime/usage/dau/
growth/proactive/events/debug/users)，并在此层做 60s digest-key 缓存与
builder 扇出。默认视图（``view`` 缺省或未知）是首页 home——users 不再兜底。
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import logging
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode

import db
from accounts import registry
from admin import data_track
from content import content_core
from core import store as core_store
from core import wake_bus
from core.reqctx import bind, request
from hosted import config_store
from model_api_runtime.v2 import jobs_store

log = logging.getLogger("feedling.admin")
InvalidDauDay = data_track.InvalidDauDay
InvalidDataTrackUserId = data_track.InvalidDataTrackUserId


def _timed(name: str, fn, *args, **kwargs):
    """计时包装：成功失败都记耗时，异常原样上抛（失败域仍归调用方）。

    只记 builder 名——查询串/入参可能带 admin_key，绝不能进日志。
    """
    start = time.monotonic()
    try:
        return fn(*args, **kwargs)
    finally:
        log.info(
            "[admin:perf] builder=%s elapsed_ms=%d",
            name,
            int((time.monotonic() - start) * 1000),
        )


def normalize_data_track_user_id(raw_user_id: str) -> str:
    return data_track._normalized_data_track_user_id(raw_user_id)


def invalid_user_id_page(query_string: str, raw_user_id: str) -> str:
    with bind(query_string):
        return data_track._render_invalid_data_track_user_page(raw_user_id)


def summary_payload(query_string: str) -> dict:
    with bind(query_string):
        return data_track._data_track_payload(include_users=False)


def users_payload(query_string: str) -> dict:
    with bind(query_string):
        return data_track._data_track_payload(include_users=True)


def dau_payload(query_string: str) -> dict:
    with bind(query_string):
        return data_track._data_track_dau_payload()


def growth_payload(query_string: str) -> dict:
    with bind(query_string):
        return data_track._data_track_growth_payload()


def debug_payload(query_string: str) -> dict:
    with bind(query_string):
        return data_track._data_track_debug_payload()


def user_payload(query_string: str, user_id: str) -> tuple[dict, int]:
    # Mirror admin_data_track_user: 404 -> {"error": "user_not_found"}.
    try:
        user_id = normalize_data_track_user_id(user_id)
    except InvalidDataTrackUserId:
        return {"error": "invalid_user_id"}, 400
    with registry._users_lock:
        entry = next((dict(u) for u in registry._users if u.get("user_id") == user_id), None)
    if not entry:
        return {"error": "user_not_found"}, 404
    with bind(query_string):
        return {"user": data_track._build_data_track_user(entry, include_detail=True)}, 200


# overview 扇出共用一个进程级线程池。每请求各开 7 个 worker 的旧写法，会让
# 两个管理员在事故期间各看一个小时窗就向共享 16 连接 DB 池（db.get_pool() 与
# jobs_store 同池）索要 14+ 条连接，把用户路由饿死。进程级上限 4 意味着无论
# 多少并发管理员/窗口，dashboard builder 的总并发就是 4。绝不能 with 包裹
# 每请求 shutdown——池要活过单个请求。
_ops_executor_lock = threading.Lock()
_ops_executor: concurrent.futures.ThreadPoolExecutor | None = None


def _get_ops_executor() -> concurrent.futures.ThreadPoolExecutor:
    global _ops_executor
    with _ops_executor_lock:
        if _ops_executor is None:
            _ops_executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=4, thread_name_prefix="admin-ops"
            )
        return _ops_executor


def _overview_builder(name: str, failure_msg: str, fn, **kwargs) -> dict | None:
    # 每个 builder 是独立失败域：自己吞异常、自己记日志、缺数据用 None 表达
    # （渲染层显示「取不到」，不伪装成健康）。跑在 worker 线程上，因此只准
    # 用显式入参——contextvar 的 request 代理在 worker 线程里是空绑定。
    try:
        return _timed(name, fn, **kwargs)
    except Exception:
        log.exception(failure_msg)
        return None


# 单个 builder 卡死（连接池 checkout 悬挂、语句无超时）不能把请求线程连同
# per-key build 锁一起挂死——挂起不抛异常，失败冷却也永远不会触发。整页共用
# 一个 deadline 而不是每个 future 各等一轮，最坏等待就是一个超时窗。
_BUILDER_RESULT_TIMEOUT_SEC = 30.0


def _collect_builder_results(
    futures: dict[str, concurrent.futures.Future],
) -> dict[str, dict | None]:
    deadline = time.monotonic() + _BUILDER_RESULT_TIMEOUT_SEC
    results: dict[str, dict | None] = {}
    for name, future in futures.items():
        try:
            results[name] = future.result(timeout=max(0.0, deadline - time.monotonic()))
        except concurrent.futures.TimeoutError:
            # 已在跑的线程无法被取消（继续占一个 admin-ops 槽直到 DB 放行），
            # 但排队未启动的可以拦下来，别让它们白白再占池子。
            future.cancel()
            log.error(
                "[admin:perf] builder=%s timed out after %.0fs; section degrades",
                name,
                _BUILDER_RESULT_TIMEOUT_SEC,
            )
            results[name] = None
    return results


# --------------------------------------------------------------------------- #
# 首页（``view`` 缺省 / home / 未知 —— 新默认页）与 verdicts JSON 共用的
# builder 扇出。首页没有 hours 窗口条：系统体检固定看最近 24 小时（与
# /v1/admin/data-track/verdicts 同口径），其余口径（周 cohort、28 天漏斗、
# 8 个北京日）全部固定在 db builder 内部。
# --------------------------------------------------------------------------- #

_HOME_WITHIN_HOURS = 24

# 系统体检的合成序：坏消息压过没消息，没消息压过好消息（bad>warn>unknown>ok
# 是「该不该点进去看」的排序，不是健康度排序——unknown 排在 warn 之下是因为
# warn 是测出来的问题、unknown 只是没证据）。
_SYSTEM_LEVEL_ORDER = {"ok": 0, "unknown": 1, "warn": 2, "bad": 3}


def compose_system_verdict(imports: dict | None, chat: dict | None) -> dict:
    """用三把既有的 ``data_track._ops_*_level`` 尺子合成「系统」判定。

    取最坏级别、合并理由（去重保序——chat 与 latency 同源，理由可能撞车）。
    合成必须发生在 admin_core：db.py 不许 import data_track（依赖方向），
    而这三把尺子属于 data_track——admin_core 是唯一同时够得到两边的层。
    未识别的级别按 warn 计：宁可让人多看一眼，不许悄悄降级成 ok。
    """
    parts = (
        data_track._ops_import_level(imports),
        data_track._ops_chat_level(chat),
        data_track._ops_latency_level(chat),
    )
    level = "ok"
    reasons: list[str] = []
    for part_level, part_reasons in parts:
        normalized = part_level if part_level in _SYSTEM_LEVEL_ORDER else "warn"
        if _SYSTEM_LEVEL_ORDER[normalized] > _SYSTEM_LEVEL_ORDER[level]:
            level = normalized
        for reason in part_reasons:
            if reason not in reasons:
                reasons.append(reason)
    return {"level": level, "reasons": reasons}


_HOME_PAGE_BUILDERS = (
    "queue", "pulse", "feed", "cost", "soft_verdicts", "funnel", "imports", "chat",
)
_VERDICTS_BUILDERS = ("queue", "pulse", "soft_verdicts", "imports", "chat")


def _run_home_builders(names: tuple[str, ...]) -> dict[str, dict | None]:
    # 与 overview/health 同一套约定：共享 4-worker 进程级执行器、每个 builder
    # 独立失败域（失败 -> None -> 渲染层「暂不可用」，绝不伪造 0）、整页共用
    # 一个 30s deadline。所有查询都是有界的（72h/48h/14d/28d/8 天），home 的
    # 8 个 builder 在 4 个 worker 上排队即可，不需要更大的池。
    specs: dict[str, tuple[str, object, dict]] = {
        "queue": ("admin home queue query failed", db.admin_home_queue, {}),
        "pulse": ("admin home pulse query failed", db.admin_home_pulse, {}),
        "feed": ("admin home feed query failed", db.admin_home_feed, {}),
        "cost": ("admin home cost query failed", db.admin_home_cost, {}),
        "soft_verdicts": (
            "admin home soft verdicts query failed",
            db.admin_home_soft_verdicts,
            {},
        ),
        "funnel": (
            "admin home funnel snapshot query failed",
            db.admin_funnel_snapshot,
            {},
        ),
        "imports": (
            "admin home import health query failed",
            db.recent_genesis_import_health,
            {"within_hours": _HOME_WITHIN_HOURS},
        ),
        "chat": (
            "admin home chat reliability query failed",
            jobs_store.recent_chat_reliability,
            {"within_hours": _HOME_WITHIN_HOURS},
        ),
    }
    executor = _get_ops_executor()
    futures = {
        name: executor.submit(_overview_builder, name, failure_msg, fn, **kwargs)
        for name, (failure_msg, fn, kwargs) in specs.items()
        if name in names
    }
    return _collect_builder_results(futures)


def _build_home_page() -> str:
    results = _run_home_builders(_HOME_PAGE_BUILDERS)
    system_verdict = compose_system_verdict(results["imports"], results["chat"])
    return data_track._render_home_page(
        system_verdict,
        results["soft_verdicts"],
        results["queue"],
        results["pulse"],
        results["feed"],
        results["cost"],
        results["funnel"],
    )


def _build_page_html(query_string: str) -> str:
    # Mirror admin_data_track_page's view dispatch.
    with bind(query_string):
        view = (request.args.get("view") or "").strip().lower()
        if view == "overview":
            # hours 必须在提交线程池之前算好：request 是 contextvar 代理，
            # worker 线程读不到本请求的绑定。
            hours = data_track._ops_window_hours()
            specs: dict[str, tuple[str, object, dict]] = {
                "imports": (
                    "admin operations overview import query failed",
                    db.recent_genesis_import_health,
                    {"within_hours": hours},
                ),
                "chat": (
                    "admin operations overview chat query failed",
                    jobs_store.recent_chat_reliability,
                    {"within_hours": hours},
                ),
                "runtime": (
                    "admin operations overview runtime query failed",
                    jobs_store.recent_runtime_health,
                    {"within_hours": hours},
                ),
                "product": (
                    "admin operations overview product KPI query failed",
                    db.recent_admin_product_kpis,
                    {"within_hours": hours},
                ),
                "usage": (
                    "admin operations overview usage query failed",
                    jobs_store.recent_token_usage_by_lane,
                    {"within_hours": hours},
                ),
                # 上一个同长窗口（环比基线）：offset_hours 把窗口整体后移。
                "prev_product": (
                    "admin operations overview previous-window product KPI query failed",
                    db.recent_admin_product_kpis,
                    {"within_hours": hours, "offset_hours": hours},
                ),
                "prev_usage": (
                    "admin operations overview previous-window usage query failed",
                    jobs_store.recent_token_usage_by_lane,
                    {"within_hours": hours, "offset_hours": hours},
                ),
            }
            executor = _get_ops_executor()
            futures = {
                name: executor.submit(_overview_builder, name, failure_msg, fn, **kwargs)
                for name, (failure_msg, fn, kwargs) in specs.items()
            }
            results = _collect_builder_results(futures)
            # 渲染留在调用线程（bind 之内）：渲染层还要读 request.args。
            return data_track._render_ops_overview_page(
                results["imports"],
                results["chat"],
                results["runtime"],
                results["product"],
                results["usage"],
                prev_product=results["prev_product"],
                prev_usage=results["prev_usage"],
                within_hours=hours,
            )
        if view == "health":
            # 产品健康页不吃 hours/day 窗口参数：留存/激活/粘性口径全部固定在
            # db 层（周 cohort、28 天滚动窗），带窗参会让 URL 看起来可调实际
            # 不可调。扇出走 overview 同一个进程级线程池与失败域约定。
            specs: dict[str, tuple[str, object, dict]] = {
                "retention": (
                    "admin product-health retention query failed",
                    db.admin_product_health_weekly_cohort_retention,
                    {},
                ),
                "activation": (
                    "admin product-health activation query failed",
                    db.admin_product_health_activation_weekly,
                    {},
                ),
                "w4_split": (
                    "admin product-health w4_split query failed",
                    db.admin_product_health_w4_split,
                    {},
                ),
                "stickiness": (
                    "admin product-health stickiness query failed",
                    db.admin_product_health_stickiness,
                    {},
                ),
                "concentration": (
                    "admin product-health concentration query failed",
                    db.admin_product_health_concentration,
                    {},
                ),
                "growth": (
                    "admin product-health growth query failed",
                    db.admin_product_health_growth_accounting_weekly,
                    {},
                ),
                "power": (
                    "admin product-health power query failed",
                    db.admin_product_health_power_users,
                    {},
                ),
                "reply_rate": (
                    "admin product-health reply_rate query failed",
                    db.admin_product_health_proactive_reply_rate,
                    {},
                ),
            }
            executor = _get_ops_executor()
            futures = {
                name: executor.submit(_overview_builder, name, failure_msg, fn, **kwargs)
                for name, (failure_msg, fn, kwargs) in specs.items()
            }
            results = _collect_builder_results(futures)
            return data_track._render_product_health_page(
                results["retention"],
                results["activation"],
                results["w4_split"],
                results["stickiness"],
                results["concentration"],
                results["growth"],
                results["power"],
                results["reply_rate"],
            )
        if view == "imports":
            hours = data_track._ops_window_hours()
            try:
                report = _timed(
                    "imports", db.recent_genesis_import_health, within_hours=hours
                )
            except Exception:
                log.exception("admin import health query failed")
                report = None
            return data_track._render_imports_page(report, within_hours=hours)
        if view in {"chat", "latency"}:
            hours = data_track._ops_window_hours()
            try:
                report = _timed(
                    "chat", jobs_store.recent_chat_reliability, within_hours=hours
                )
            except Exception:
                log.exception("admin chat reliability query failed")
                report = None
            if view == "latency":
                return data_track._render_latency_page(
                    report,
                    within_hours=hours,
                )
            return data_track._render_chat_reliability_page(
                report,
                within_hours=hours,
            )
        if view == "dau":
            return data_track._render_data_track_dau_page(data_track._data_track_dau_payload())
        if view == "growth":
            return data_track._render_data_track_growth_page(data_track._data_track_growth_payload())
        if view == "proactive":
            return data_track._render_proactive_daily_page(data_track._data_track_proactive_daily_payload())
        if view == "debug":
            return data_track._render_data_track_debug_page(data_track._data_track_debug_payload())
        if view == "usage":
            query = data_track.admin_usage.parse_usage_query(request.args)
            try:
                report = data_track._usage_report(query)
            except Exception:
                logging.exception("admin usage report failed (other views still served)")
                return data_track._render_usage_error_page(query)
            return data_track._render_usage_page(report, query)
        if view == "runtime":
            # 窗口算一次、传给四个数据函数——各处自行读 request.args 会让窗口
            # 有机会不一致（同页一个 24 小时、一个 720 小时）。
            hours = data_track._runtime_health_window_hours()
            # 四次调用是**四个独立的失败域**，不共用一个 try。健康数据是这页的
            # 核心，它没了才该降级；token 与交付是附加信息。token 的查询无 LIMIT、
            # 扫描量随表增长单调变大，是最先超时的那个——让它把明明可用的健康数据
            # 一起拖进降级页，是这页最坏的失败模式（它恰恰是出事时才被打开的那一
            # 页）。附加信息挂掉只让对应区块显「取不到」。
            try:
                payload = _timed(
                    "runtime_health_summary",
                    data_track._runtime_health_summary,
                    within_hours=hours,
                )
            except Exception:
                logging.exception("runtime health summary failed")
                return data_track._render_runtime_health_error_page()
            try:
                tokens = _timed(
                    "runtime_token_by_lane",
                    data_track._runtime_token_by_lane,
                    within_hours=hours,
                )
            except Exception:
                logging.exception("runtime token usage failed (health still served)")
                tokens = None
            try:
                delivery = _timed(
                    "runtime_delivery_health",
                    data_track._runtime_delivery_health,
                    within_hours=hours,
                )
            except Exception:
                logging.exception("runtime delivery health failed (health still served)")
                delivery = None
            try:
                user_report = _timed(
                    "runtime_user_report",
                    data_track._runtime_user_report,
                    within_hours=hours,
                )
            except Exception:
                logging.exception("runtime user report failed (health still served)")
                user_report = None
            return data_track._render_runtime_health_page(
                payload, tokens, delivery, user_report
            )
        if view == "events":
            event = (request.args.get("event") or "").strip()
            if event == "onboarding":
                return data_track._render_onboarding_funnel_page(data_track._data_track_onboarding_funnel_payload())
            if event:
                return data_track._render_event_users_page(data_track._data_track_event_users_payload(event))
            return data_track._render_events_page(data_track._data_track_events_payload())
        if view == "users":
            # 用户页（不再是默认页）：漏斗快照走共享执行器、沿用首页同一
            # 失败域约定（失败 -> None -> 渲染层「暂不可用」）。用户列表
            # payload 要读 request.args（过滤/分页/排序），必须留在调用线程
            # （contextvar 绑定不跨 worker 线程）——正好与漏斗查询并行。
            executor = _get_ops_executor()
            futures = {
                "funnel": executor.submit(
                    _overview_builder,
                    "funnel",
                    "admin users funnel snapshot query failed",
                    db.admin_funnel_snapshot,
                )
            }
            payload = data_track._data_track_payload(include_users=True)
            results = _collect_builder_results(futures)
            return data_track._render_data_track_page(
                payload, funnel=results["funnel"]
            )
        if view == "diag":
            # 诊断枢纽：11 个旧视图的卡片索引（何时来看），纯静态、无 builder。
            return data_track._render_diag_hub_page()
        # ``view`` 缺省 / home / 未知 -> 首页（新默认）。缓存 key 已含 view，
        # 因此裸 /admin/data-track 与 ?view=home 是两个 key、各缓存一份相同
        # 内容——已核实并接受：60s TTL、64 条上限下代价是一份页面字符串，比
        # 在缓存 key 层做视图别名归一更简单，也不会引入「别名归一与渲染层
        # 不一致」这类新错法。
        return _build_home_page()


# --------------------------------------------------------------------------- #
# page_html 的 60s TTL 缓存（single-flight + stale-on-error）。
# 只缓存列表/报表页；user_page（用户详情，可能带 reveal 参数）绝不进缓存；
# view=debug（同样可能带 reveal 明文）整体绕过缓存。
# key 是「规范化参数字典（first-value-wins，与 core.reqctx._Args 同口径）
# **含 admin_key**」的 sha256 摘要：缓存结构里只存摘要、不存明文 secret，
# 同时两条鉴权通道（查询串 admin_key vs 会话 cookie，见 routes_asgi）永不
# 共享条目——否则 cookie 端生成的页会把 admin_key 从 key 端管理员的 19 个
# 导航 href 里剥掉（点一下就 401），key 端生成的页会把明文 token 泄给
# cookie 端浏览器。命中缓存必须带 cache-note 声明数据年龄：不许把旧数据
# 伪装成刚生成的。TTL 过后条目仍可为 stale-on-error 服务，但超过硬保留期
# （10 分钟）的条目在每次 get/put 时一律清除——什么都不许无限期活着。
# --------------------------------------------------------------------------- #

_PAGE_CACHE_TTL_SEC = 60.0
_PAGE_CACHE_HARD_RETENTION_SEC = 600.0
_PAGE_CACHE_FAILURE_COOLDOWN_SEC = 5.0
_PAGE_CACHE_MAX_ENTRIES = 64
# 全局锁只保护下面几个 dict 的结构，绝不在持有期间重建页面。
_page_cache_lock = threading.Lock()
_page_cache: dict[str, tuple[float, str]] = {}  # key -> (built_at monotonic, html)
_page_cache_builds: dict[str, threading.Lock] = {}  # per-key single-flight 锁
_page_cache_last_failure: dict[str, float] = {}  # key -> 最近一次重建失败时刻


def _canonical_page_params(query_string: str) -> dict[str, str]:
    # 与 core.reqctx._Args 完全同口径：parse_qsl 原始顺序 + setdefault，
    # 重复参数取第一个值。缓存 key 必须等于渲染层实际看到的参数，否则
    # ?view=dau&view=growth 与 ?view=growth&view=dau 会共享一个条目却
    # 渲染出不同的页。
    first: dict[str, str] = {}
    for key, value in parse_qsl(query_string or "", keep_blank_values=True):
        first.setdefault(key, value)
    return first


def _page_cache_key(query_string: str) -> str:
    params = _canonical_page_params(query_string)
    return hashlib.sha256(
        urlencode(sorted(params.items())).encode("utf-8")
    ).hexdigest()


def _purge_hard_expired_locked(now: float) -> None:
    # 调用方必须已持有 _page_cache_lock。
    for key in [
        key
        for key, (built_at, _) in _page_cache.items()
        if now - built_at >= _PAGE_CACHE_HARD_RETENTION_SEC
    ]:
        _page_cache.pop(key, None)
        _page_cache_builds.pop(key, None)
    for key in [
        key
        for key, failed_at in _page_cache_last_failure.items()
        if now - failed_at >= _PAGE_CACHE_FAILURE_COOLDOWN_SEC
    ]:
        _page_cache_last_failure.pop(key, None)


def _page_cache_get(key: str) -> tuple[float, str] | None:
    with _page_cache_lock:
        _purge_hard_expired_locked(time.monotonic())
        return _page_cache.get(key)


def _page_cache_put(key: str, built_at: float, page: str) -> None:
    with _page_cache_lock:
        _purge_hard_expired_locked(time.monotonic())
        _page_cache[key] = (built_at, page)
        _page_cache_last_failure.pop(key, None)
        overflow = len(_page_cache) - _PAGE_CACHE_MAX_ENTRIES
        if overflow > 0:
            # 恶意查询串变体不能把缓存撑爆：按生成时间淘汰最旧的。
            stale_keys = sorted(_page_cache, key=lambda k: _page_cache[k][0])[:overflow]
            for stale_key in stale_keys:
                _page_cache.pop(stale_key, None)
                _page_cache_builds.pop(stale_key, None)
        if len(_page_cache_builds) > _PAGE_CACHE_MAX_ENTRIES * 2:
            # 重建异常路径已就地回收自己的锁条目，这里是兜底：清掉一切
            # 没有对应缓存条目的孤儿锁。被回收的锁若仍有持有者/等待者，
            # 最坏结果是同 key 多重建一次。
            for stale_key in [k for k in _page_cache_builds if k not in _page_cache]:
                _page_cache_builds.pop(stale_key, None)


def _humanize_age_zh(age_sec: float) -> str:
    seconds = max(0, int(age_sec))
    if seconds < 120:
        return f"{seconds} 秒前"
    if seconds < 7200:
        return f"{seconds // 60} 分钟前"
    return f"{seconds // 3600} 小时前"


def _with_cache_note(page: str, age_sec: float) -> str:
    # 样式必须自包含（页面模板各自为政、没有 cache-note 的 CSS），位置必须
    # 在 <main> 顶部——一条“这是缓存”的声明沉在一万像素高的页底等于没说。
    note = (
        "<div class='cache-note' style=\"display:inline-block;margin:10px 0;"
        "padding:4px 10px;background:#f6f5f0;border:1px solid #dddcd4;"
        "border-radius:6px;color:#68706a;font-size:12px;\">"
        f"页面缓存 · 数据生成于 {_humanize_age_zh(age_sec)}</div>"
    )
    idx = page.find("<main")
    if idx >= 0:
        tag_end = page.find(">", idx)
        if tag_end >= 0:
            return page[: tag_end + 1] + note + page[tag_end + 1 :]
    return note + page


def page_html(query_string: str) -> str:
    params = _canonical_page_params(query_string)
    if (params.get("view") or "").strip().lower() == "debug":
        # debug 视图可能带 reveal=<明文>，任何形式的驻留都不行——直接重建。
        return _build_page_html(query_string)
    key = _page_cache_key(query_string)
    entry = _page_cache_get(key)
    now = time.monotonic()
    if entry is not None and now - entry[0] < _PAGE_CACHE_TTL_SEC:
        return _with_cache_note(entry[1], now - entry[0])
    with _page_cache_lock:
        build_lock = _page_cache_builds.get(key)
        if build_lock is None:
            build_lock = threading.Lock()
            _page_cache_builds[key] = build_lock
    with build_lock:
        # 等锁期间可能已有并发请求完成重建（single-flight：同 key 只重建一次）。
        entry = _page_cache_get(key)
        now = time.monotonic()
        if entry is not None and now - entry[0] < _PAGE_CACHE_TTL_SEC:
            return _with_cache_note(entry[1], now - entry[0])
        with _page_cache_lock:
            failed_at = _page_cache_last_failure.get(key)
        if (
            failed_at is not None
            and now - failed_at < _PAGE_CACHE_FAILURE_COOLDOWN_SEC
        ):
            # 冷却期内不再逐个排队撞 DB 超时（N × timeout 的车队效应）：
            # 有旧页就诚实地端旧页，没有就立刻失败，冷却期过了才许再试。
            if entry is not None:
                return _with_cache_note(entry[1], now - entry[0])
            raise RuntimeError("admin page rebuild failed recently; retry shortly")
        try:
            page = _build_page_html(query_string)
        except Exception:
            with _page_cache_lock:
                _page_cache_last_failure[key] = time.monotonic()
                if key not in _page_cache:
                    # 失败从不写 _page_cache；不就地回收锁条目的话，事故期间
                    # 300 个各异的失败 key 就是 300 把孤儿锁。
                    _page_cache_builds.pop(key, None)
            if entry is not None:
                # stale-on-error：旧页比 5xx 有用；诚实性由 cache-note 的年龄保证。
                log.exception(
                    "admin data-track page rebuild failed; serving stale cache"
                )
                return _with_cache_note(entry[1], time.monotonic() - entry[0])
            raise
        _page_cache_put(key, time.monotonic(), page)
        return page


def verdicts_payload(query_string: str) -> dict:
    """GET /v1/admin/data-track/verdicts 的机读体检 payload。

    有意 **不** 走 page_html 的 60s 页缓存：那层只服务 HTML（cache-note 是
    往 ``<main>`` 里插 HTML 声明数据年龄的），JSON 没有对应的「这是旧数据」
    通道——把缓存过的判定不加声明地喂给机器消费方，比喂给人更危险。底层
    查询全部有界、又都在共享 4-worker 执行器上限流，事故期间的总并发依旧
    被执行器封顶，逐请求重建的代价可接受。

    诚实性约定：``queue``/``pulse`` 在 builder 失败时输出 ``null`` 而不是
    空表/零值——空队列是「没有人卡住」的断言，查询失败给不出这个断言。
    软性判定缺失时对应键降级为 unknown（灰，不是绿）。
    """
    # builder 口径全部固定（无 hours/day 参数），暂不消费查询串；保留参数
    # 是为了与兄弟 payload 入口同形、给未来加过滤留位。
    del query_string
    results = _run_home_builders(_VERDICTS_BUILDERS)
    system = compose_system_verdict(results["imports"], results["chat"])
    soft = results["soft_verdicts"]
    verdicts: dict[str, dict] = {"system": system}
    for name in ("growth", "cost", "evidence"):
        verdict = (soft or {}).get(name)
        verdicts[name] = (
            verdict
            if isinstance(verdict, dict) and verdict.get("level")
            else {"level": "unknown", "reasons": ["软性判定暂不可用"]}
        )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "verdicts": verdicts,
        "queue": results["queue"],
        "pulse": results["pulse"],
    }


def login_page(*, error: bool = False, next_url: str = "/admin/data-track") -> str:
    return data_track._render_admin_login_page(error=error, next_url=next_url)


def user_page(query_string: str, user_id: str) -> tuple[str, str, int]:
    # Mirror admin_data_track_user_page. Returns (kind, body, status):
    # ("text", "user not found", 404) or ("html", <page>, 200).
    try:
        user_id = normalize_data_track_user_id(user_id)
    except InvalidDataTrackUserId:
        return "html", invalid_user_id_page(query_string, user_id), 400
    with registry._users_lock:
        entry = next((dict(u) for u in registry._users if u.get("user_id") == user_id), None)
    if not entry:
        return "text", "user not found", 404
    with bind(query_string):
        view = (request.args.get("view") or "").strip().lower()
        if view == "usage":
            query = replace(
                data_track.admin_usage.parse_usage_query(request.args),
                user_id=user_id,
            )
            try:
                report = data_track._usage_report(query)
            except Exception:
                logging.exception(
                    "admin user usage report failed (other views still served)"
                )
                body = data_track._render_usage_error_page(
                    query, drilldown_user_id=user_id
                )
            else:
                body = data_track._render_usage_page(
                    report, query, drilldown_user_id=user_id
                )
            return "html", body, 200
        body = data_track._render_user_detail_page(
            data_track._build_data_track_user(entry, include_detail=True)
        )
    return "html", body, 200


def store_evict(user_id: str) -> dict:
    # Mirror admin_store_evict's side effect + payload (validation stays in the route).
    evicted = core_store._evict_store(user_id)
    print(f"[admin:store/evict] user_id={user_id} evicted={evicted}")
    return {"evicted": evicted, "user_id": user_id}


# --------------------------------------------------------------------------- #
# hosted_runtime_mode control plane (Hosted Runtime V2 D0 rollout — gated flip
# between resident_cli and db_action_v2 without a direct DB write).
# --------------------------------------------------------------------------- #

def set_runtime_mode(user_id: str, mode: str) -> tuple[dict, int]:
    store = core_store.get_store(user_id)
    if mode == config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2:
        try:
            # Seed first. If profile persistence subsequently fails this row is
            # dormant because every producer is mode-filtered; the reverse order
            # creates a real window where the resident is reaped but no V2 wake
            # schedule exists.
            jobs_store.upsert_wake_schedule(user_id, next_heartbeat_at=time.time())
        except Exception as e:  # noqa: BLE001 — do not report a half-ready flip
            return {"error": "v2_schedule_seed_failed", "detail": str(e)[:160]}, 503
    try:
        persisted_mode = config_store.set_hosted_runtime_mode(store, mode)
    except ValueError as e:
        return {"error": str(e)}, 400
    except Exception:
        return {"error": "runtime_control_unavailable"}, 503
    return {"user_id": user_id, "hosted_runtime_mode": persisted_mode}, 200


def get_runtime_mode(user_id: str) -> tuple[dict, int]:
    store = core_store.get_store(user_id)
    try:
        mode = config_store.get_hosted_runtime_mode_strict(store)
    except Exception:
        return {"error": "runtime_control_unavailable"}, 503
    return {"user_id": user_id, "hosted_runtime_mode": mode}, 200


def set_runtime_allowlist(user_id: str, desired: str, *, note: str = "") -> tuple[dict, int]:
    if desired == "remove":
        removed = db.delete_runtime_allowlist(user_id)
        return {"user_id": user_id, "removed": removed}, 200
    try:
        db.upsert_runtime_allowlist(user_id, desired, updated_by="admin", note=note)
    except ValueError as e:
        return {"error": str(e)}, 400
    return {"user_id": user_id, "desired": desired}, 200


def get_runtime_allowlist() -> dict:
    """Reconciliation view: every allowlist row plus its live fence (mode/state/
    generation) and a ``converged`` bool. A per-row read failure (e.g. transient
    DB hiccup while resolving one user's fence) is captured on that row instead
    of failing the whole endpoint — this is an admin dashboard, one bad row
    should not hide the rest of the fleet."""
    from core import store as core_store  # noqa: PLC0415
    from hosted import config_store as cs  # noqa: PLC0415
    rows = db.list_runtime_allowlist()
    for row in rows:
        try:
            mode, state, gen = cs.get_hosted_runtime_control_strict(
                core_store.get_store(row["user_id"]))
            row["actual"] = {"mode": mode, "state": state, "generation": gen}
            row["converged"] = (
                (row["desired"] == "v2" and state == "v2")
                or (row["desired"] == "resident" and state == "resident"))
        except Exception as e:  # noqa: BLE001 — 对账视图不因单行炸
            row["actual"] = {"error": str(e)[:80]}
            row["converged"] = False
    return {"allowlist": rows}


def list_runtime_modes() -> dict:
    # Enumerate every user with an active provider route, not only users who
    # already have a runtime-profile blob. Otherwise an unset (effective
    # resident) user disappears from the admin view and from V2 scheduler
    # eligibility calculations. The shared normalizer keeps missing/invalid
    # semantics identical to chat routing and the per-user control endpoint.
    result: dict = {
        config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2: [],
        config_store.HOSTED_RUNTIME_MODE_RESIDENT: [],
    }
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT r.user_id, mrt.doc->>'hosted_runtime_mode' AS mode
            FROM model_api_routes r
            LEFT JOIN user_blobs mrt
              ON mrt.user_id = r.user_id
             AND mrt.kind = 'model_api_runtime'
            WHERE r.is_active
            ORDER BY r.user_id
            """
        ).fetchall()
    for row in rows:
        user_id = row[0] if not isinstance(row, dict) else row["user_id"]
        mode = row[1] if not isinstance(row, dict) else row["mode"]
        effective_mode = config_store.effective_hosted_runtime_mode(mode)
        result[effective_mode].append(user_id)
    return result


# --------------------------------------------------------------------------- #
# v2 turn metrics (Task 4 — D4 load-testing consumes these via
# GET /v1/admin/v2-metrics; queue depth/worker liveness/service time/token
# throughput, all sourced from jobs_store's existing DB-backed counters).
# --------------------------------------------------------------------------- #

def v2_metrics(
    *,
    cache_provider: str | None = None,
    cache_model: str | None = None,
    cache_route_fingerprint: str | None = None,
    cache_user_id: str | None = None,
    cache_since_ts: float | None = None,
    cache_until_ts: float | None = None,
) -> dict:
    return {
        "inflight": jobs_store.inflight_job_count(),
        "pending": jobs_store.pending_job_count(),
        "live_workers": jobs_store.live_worker_count(),
        "live_worker_capacity": jobs_store.live_worker_capacity(),
        # Two rows (turn + Genesis) per runner. Use the store's bounded maximum
        # so the production per-CVM gate can prove fleets larger than 25 CVMs
        # instead of silently truncating at the helper's observational default.
        "worker_heartbeats": jobs_store.recent_worker_heartbeats(limit=200),
        "worker_heartbeat_count": jobs_store.recent_worker_heartbeat_count(),
        "runtime_policy": config_store.hosted_runtime_policy_status(),
        "mean_service_sec": jobs_store.recent_mean_service_sec(lane="chat"),
        "recent_mean_tokens_per_turn": jobs_store.recent_mean_tokens_per_turn(lane="chat"),
        "turn_health": jobs_store.recent_chat_operational_health(),
        # All-lane view, including dream/capture.  The legacy ``wake`` field is
        # intentionally narrower (heartbeat/scheduled/manual_wake) and cannot
        # reveal a silent extraction lane.
        "runtime_health": jobs_store.recent_runtime_health(),
        "prompt_cache": jobs_store.recent_prompt_cache_stats(
            lane="chat",
            provider=cache_provider,
            model=cache_model,
            cache_route_fingerprint=cache_route_fingerprint,
            user_id=cache_user_id,
            since_ts=cache_since_ts,
            until_ts=cache_until_ts,
            include_turns=bool(
                cache_user_id
                and cache_since_ts is not None
                and cache_until_ts is not None
            ),
        ),
        "tail_window": {
            lane: jobs_store.recent_tail_window_stats(lane=lane)
            for lane in (
                "chat",
                "heartbeat",
                "scheduled",
                "manual_wake",
                "screen_watch",
            )
        },
        "wake": jobs_store.wake_success_stats(),
        # Deliberately its own block, not extra lanes inside `wake`: capture/dream
        # are not wakes, and folding them in would make a memory-lane outage look
        # like a falling wake success rate. See jobs_store.memory_lane_health().
        "memory_lanes": jobs_store.memory_lane_health(),
        "effects": db.effect_outbox_health(),
        # The genesis import worker rides in the serve_worker process on its own
        # thread, and `run_loop` imports `genesis.worker` lazily — so that thread can
        # die while the turn loops keep beating. Without this field, a dead genesis
        # thread is invisible until a user reports their onboarding distillation
        # stuck. `live_workers` counts kind='turn' only and would not notice.
        "genesis_alive": jobs_store.genesis_worker_alive(),
    }


def delete_user(user_id: str) -> tuple[dict, int]:
    """Delete one account by authoritative DB id and evict its cached state."""
    if not db.user_exists(user_id):
        return {"error": "user_not_found"}, 404

    archive_err = content_core._purge_onboarding_archives_with_retry(user_id)
    if archive_err is not None:
        log.error(
            "[admin:user/delete] onboarding archive cleanup failed user_id=%r: %s",
            user_id,
            archive_err,
        )
        return {"error": "archive_cleanup_failed"}, 503

    with registry._users_lock:
        if not db.delete_user(user_id):
            return {"error": "user_not_found"}, 404
        registry._users[:] = [
            entry for entry in registry._users if entry.get("user_id") != user_id
        ]
        stale_hashes = [
            key_hash
            for key_hash, cached_user_id in registry._key_to_user.items()
            if cached_user_id == user_id
        ]
        for key_hash in stale_hashes:
            registry._key_to_user.pop(key_hash, None)

    audit = {
        "event": "admin_user_delete",
        "who": "admin",
        "user_id": user_id,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    log.info("[admin:user/delete] %s", json.dumps(audit, separators=(",", ":")))

    registry.notify_users_changed()
    wake_bus.notify("blob", user_id)

    for label, cleanup in (
        ("frames-r2", lambda: db.delete_user_frames(user_id)),
        ("chat-files-r2", lambda: db.delete_user_chat_files(user_id)),
        ("db-belt", lambda: db.delete_user_data(user_id)),
    ):
        try:
            cleanup()
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "[admin:user/delete] cleanup failed label=%s user_id=%r: %s",
                label,
                user_id,
                exc,
            )

    with core_store._stores_lock:
        cached_store = core_store._stores.pop(user_id, None)
    if cached_store is not None:
        core_store._wake_store_waiters(cached_store)

    return {"deleted": True, "user_id": user_id}, 200
