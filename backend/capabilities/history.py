"""History capabilities — thin facade over model_api_runtime/v2/history_readside.py.

形态照 ``capabilities/memory.py``：参数校验 + 稳定错误码 + CapabilityResult。
本模块额外承担 spec §5/§6 的两件事：

1. **配置落地**：batch 2 的 ``run_history_search``/``run_history_fetch`` 把预算和
   cursor HMAC key 全部显式收参；这里统一从 env 读取后传入（env 名与默认值见
   ``_budget_from_env``），调用层（executor/worker）不再各自读配置。
2. **序列化前结构化缩减**：executor 的单结果闸（2000）与 tool_loop 的同批
   8000 字符水位都是"切串"式截断，会把 JSON 切成半截；history 结果必须在
   **进入序列化之前**缩到预算内（search ≤1800 / fetch ≤1600，env 可调），
   见 ``_shrink_search_result`` / ``_shrink_fetch_result``。

kill switch：``FEEDLING_V2_HISTORY_TOOLS_ENABLED``，**默认 ON**（回滚闸，不是
feature gate——工作区纪律）。关掉后本 facade 直接拒绝（``tool_not_allowed``），
与 offer gate（工具目录移除）、executor dispatch gate（lane 判定）构成双层闸的
最深一层：即使目录隐藏和 executor 都被绕过，capability 本体仍然拒绝。

错误码约定（照 memory.py 用 ``err()``，但 code 直接用 history 的稳定 slug）：
executor 喂回模型的失败结果**只暴露 error code、不回显 message**（见
``executor._summarize_capability_result``），所以 ``cursor_invalid`` /
``cursor_mismatch`` / ``missing_query_or_time_range`` 这些模型需要据以自我修正
的语义必须放在 code 位，放 message 里等于没说。
"""
from __future__ import annotations

import json
import os

from model_api_runtime.v2 import history_readside, history_search

from capabilities import errors
from capabilities.types import CapabilityResult, ok, err

HISTORY_SEARCH_TOOL = "history_search"
HISTORY_FETCH_TOOL = "history_fetch"
HISTORY_TOOL_NAMES = frozenset({HISTORY_SEARCH_TOOL, HISTORY_FETCH_TOOL})

ENABLED_ENV = "FEEDLING_V2_HISTORY_TOOLS_ENABLED"

# 目标序列化尺寸（json.dumps(ensure_ascii=False) 的字符数，与 executor 的渲染
# 完全同一种序列化）。search 1800 < executor 2000 单结果闸；fetch 1600（spec
# §3.2 的完整 payload 限制）。注意 tool_loop 的同批 8000 字符水位在 8 个大结果
# 混合时可把单结果额度压到 ~1000——每 round 只许 1 个 history 调用（worker 的
# round gate）让其余 7 个都是 history 的概率为零，但**不能**保证兄弟结果总和
# ≤6200；水位裁剪对 >1000 字符的结果仍是残余风险（cursor 本身可达 1024 字符，
# 目标压到 1000 以下不可行）。
SEARCH_RESULT_MAX_CHARS_ENV = "FEEDLING_V2_HISTORY_SEARCH_RESULT_MAX_CHARS"
FETCH_RESULT_MAX_CHARS_ENV = "FEEDLING_V2_HISTORY_FETCH_RESULT_MAX_CHARS"
DEFAULT_SEARCH_RESULT_MAX_CHARS = 1800
DEFAULT_FETCH_RESULT_MAX_CHARS = 1600

# facade 层的 dispatch 拒绝（开关关闭）。executor 的 lane 拒绝用同一个词面，
# 模型两处看到的都是 "error: tool_not_allowed"。
TOOL_NOT_ALLOWED = "tool_not_allowed"


def enabled() -> bool:
    """Kill switch 读取（调用时读，不在 import 时钉死；默认 ON）。"""
    return str(os.environ.get(ENABLED_ENV, "1")).strip().lower() not in {
        "0", "false", "no", "off",
    }


def _int_env(name: str, default: int) -> int:
    try:
        value = int(str(os.environ.get(name, "")).strip() or default)
    except (TypeError, ValueError):
        return int(default)
    return value if value > 0 else int(default)


def _float_env(name: str, default: float) -> float:
    try:
        value = float(str(os.environ.get(name, "")).strip() or default)
    except (TypeError, ValueError):
        return float(default)
    return value if value > 0 else float(default)


def _budget_from_env() -> history_readside.HistorySearchBudget:
    """spec §5 预算表 → env 可调（默认即 spec 默认值）。

    cursor 确定性要求（history_readside._leaf_hint_ranges 的注释）：同一部署内
    这些值保持稳定，翻页期间重算的叶子命中段才一致；env 只该在部署级设置。
    """
    return history_readside.HistorySearchBudget(
        leaf_call_max_leaves=_int_env("FEEDLING_V2_HISTORY_LEAF_CALL_MAX_LEAVES", 64),
        leaf_call_max_bytes=_int_env(
            "FEEDLING_V2_HISTORY_LEAF_CALL_MAX_BYTES", 256 * 1024),
        raw_batch_rows=_int_env("FEEDLING_V2_HISTORY_RAW_BATCH_ROWS", 128),
        raw_batch_bytes=_int_env("FEEDLING_V2_HISTORY_RAW_BATCH_BYTES", 512 * 1024),
        call_max_rows=_int_env("FEEDLING_V2_HISTORY_CALL_MAX_ROWS", 512),
        call_deadline_ms=_int_env("FEEDLING_V2_HISTORY_CALL_DEADLINE_MS", 2500),
        snippet_max_chars=_int_env(
            "FEEDLING_V2_HISTORY_SNIPPET_MAX_CHARS",
            history_readside.SNIPPET_MAX_CHARS),
        fetch_max_chars_per_message=_int_env(
            "FEEDLING_V2_HISTORY_FETCH_MAX_CHARS_PER_MESSAGE", 800),
        cursor_ttl_seconds=_float_env(
            "FEEDLING_V2_HISTORY_CURSOR_TTL_SECONDS",
            float(history_search.DEFAULT_CURSOR_TTL_SECONDS)),
    )


def _search_result_max_chars() -> int:
    return _int_env(SEARCH_RESULT_MAX_CHARS_ENV, DEFAULT_SEARCH_RESULT_MAX_CHARS)


def _fetch_result_max_chars() -> int:
    return _int_env(FETCH_RESULT_MAX_CHARS_ENV, DEFAULT_FETCH_RESULT_MAX_CHARS)


def _rendered_chars(payload) -> int:
    """与 executor._summarize_capability_result 完全相同的序列化度量。"""
    return len(json.dumps(payload, ensure_ascii=False))


_SNIPPET_SHRINK_STEPS = (160, 96, 48)


def _shrink_search_result(result: dict, *, max_chars: int) -> dict:
    """序列化前把 search 结果缩到 ``max_chars`` 内（spec §5：先 snippet 后条数）。

    先逐级截短 snippet（标 ``content_truncated``），仍超再从尾部（扫描优先级
    最低）丢 match。丢掉的 match 这一页拿不回（cursor 已越过它），这是预算
    应急阀的已接受代价——snippet 先缩使其极少发生。``next_cursor`` / 三态
    字段绝不动：截断绝不能伪装成"扫完了"。
    """
    out = dict(result)
    out["matches"] = [dict(m) for m in (result.get("matches") or [])]
    if _rendered_chars(out) <= max_chars:
        return out
    for cap in _SNIPPET_SHRINK_STEPS:
        for match in out["matches"]:
            snippet = str(match.get("snippet") or "")
            if len(snippet) > cap:
                match["snippet"] = snippet[:cap]
                match["content_truncated"] = True
        if _rendered_chars(out) <= max_chars:
            return out
    while out["matches"]:
        out["matches"].pop()
        if _rendered_chars(out) <= max_chars:
            break
    return out


def _truncate_item_content(item: dict, cap: int) -> None:
    content = item.get("content")
    if isinstance(content, str) and len(content) > cap:
        item["content"] = content[:cap]
        item["content_truncated"] = True


_NEIGHBOR_CONTENT_SHRINK_STEPS = (300, 150)
_ANCHOR_CONTENT_SHRINK_STEPS = (800, 400, 200, 100)


def _shrink_fetch_result(result: dict, *, max_chars: int) -> dict:
    """序列化前把 fetch 结果缩到 ``max_chars`` 内（spec §3.2）。

    顺序：① 邻居正文逐级截短 → ② 砍最远的邻居（before 头部最旧 / after 尾部
    最新，长边优先）→ ③ 最后才截锚点正文——锚点是这次调用的目的，尽量保全。
    绝不序列化后切串。
    """
    out = dict(result)
    anchor = result.get("anchor")
    out["anchor"] = dict(anchor) if isinstance(anchor, dict) else anchor
    out["before"] = [dict(i) for i in (result.get("before") or [])]
    out["after"] = [dict(i) for i in (result.get("after") or [])]
    if _rendered_chars(out) <= max_chars:
        return out
    for cap in _NEIGHBOR_CONTENT_SHRINK_STEPS:
        for item in out["before"] + out["after"]:
            _truncate_item_content(item, cap)
        if _rendered_chars(out) <= max_chars:
            return out
    while out["before"] or out["after"]:
        if out["before"] and len(out["before"]) >= len(out["after"]):
            out["before"].pop(0)
        else:
            out["after"].pop()
        if _rendered_chars(out) <= max_chars:
            return out
    if isinstance(out["anchor"], dict):
        for cap in _ANCHOR_CONTENT_SHRINK_STEPS:
            _truncate_item_content(out["anchor"], cap)
            if _rendered_chars(out) <= max_chars:
                return out
    return out


def _infra_err(exc: RuntimeError, *, default_msg: str) -> CapabilityResult:
    """enclave/基建 RuntimeError → 稳定 capability 错误。

    route 缺失（版本错位，spec §6）必须给出明确的 capability-unavailable，
    绝不静默空结果；其余 enclave/传输问题按 upstream 可重试处理。message
    只用固定文案（enclave 响应文本可能带数据，不回显）。
    """
    code = str(exc)
    if code == "enclave_history_capability_unavailable":
        return err(
            errors.UNAVAILABLE,
            "history capability is not available on this deployment yet",
            retryable=False,
        )
    return err(errors.UPSTREAM, default_msg, retryable=True)


def search(store, *, api_key=None, runtime_token=None, params=None) -> CapabilityResult:
    """history_search（spec §3.1）。用户身份永远取自认证上下文（store），
    绝不来自参数或 cursor。"""
    del api_key  # 与其余 capability 签名一致；本能力只走 runtime_token。
    if not enabled():
        return err(TOOL_NOT_ALLOWED, "history tools are disabled", retryable=False)
    params = params or {}
    try:
        cursor_key = history_readside.derive_cursor_hmac_key()
    except RuntimeError:
        return err(errors.UNAVAILABLE, "history cursor key unavailable", retryable=False)
    limit = params.get("limit")
    try:
        result = history_readside.run_history_search(
            str(store.user_id),
            cursor_hmac_key=cursor_key,
            query=params.get("query"),
            start=params.get("start"),
            end=params.get("end"),
            cursor=params.get("cursor"),
            limit=3 if limit is None else limit,
            runtime_token=runtime_token,
            budget=_budget_from_env(),
        )
    except history_search.CursorMismatch as exc:
        return err(exc.code, "pass only the cursor when paging", retryable=False)
    except history_search.CursorInvalid as exc:
        return err(exc.code, "cursor rejected; restart without cursor", retryable=False)
    except history_search.HistorySearchInputError as exc:
        return err(exc.code or errors.INVALID, "invalid history_search input",
                   retryable=False)
    except RuntimeError as exc:
        return _infra_err(exc, default_msg="history search unavailable")
    return ok(data=errors.cap_data(
        _shrink_search_result(result, max_chars=_search_result_max_chars())))


def fetch(store, *, api_key=None, runtime_token=None, params=None) -> CapabilityResult:
    """history_fetch（spec §3.2）：单窗口锚点 + 邻居。"""
    del api_key
    if not enabled():
        return err(TOOL_NOT_ALLOWED, "history tools are disabled", retryable=False)
    params = params or {}
    message_id = str(params.get("message_id") or "").strip()
    if not message_id:
        return err(errors.INVALID, "message_id is required for history_fetch",
                   retryable=False)
    before = params.get("before")
    after = params.get("after")
    try:
        result = history_readside.run_history_fetch(
            str(store.user_id),
            message_id=message_id,
            before=2 if before is None else before,
            after=2 if after is None else after,
            runtime_token=runtime_token,
            budget=_budget_from_env(),
        )
    except history_search.HistorySearchInputError as exc:
        return err(exc.code or errors.INVALID, "invalid history_fetch input",
                   retryable=False)
    except RuntimeError as exc:
        return _infra_err(exc, default_msg="history fetch unavailable")
    if isinstance(result, dict) and result.get("error"):
        # 锚点不可见/不存在统一 not_found_or_not_visible（不区分，权限语义）。
        return err(str(result["error"]), "message not found or not visible",
                   retryable=False)
    return ok(data=errors.cap_data(
        _shrink_fetch_result(result, max_chars=_fetch_result_max_chars())))
