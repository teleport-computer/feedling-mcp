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

import contextlib
import contextvars
import json
import os

from model_api_runtime.v2 import history_readside, history_search

from capabilities import errors
from capabilities import result_budget
from capabilities.types import CapabilityResult, ok, err

HISTORY_SEARCH_TOOL = "history_search"
HISTORY_FETCH_TOOL = "history_fetch"
HISTORY_TOOL_NAMES = frozenset({HISTORY_SEARCH_TOOL, HISTORY_FETCH_TOOL})

ENABLED_ENV = "FEEDLING_V2_HISTORY_TOOLS_ENABLED"

# 目标序列化尺寸（json.dumps(ensure_ascii=False) 的字符数，与 executor 的渲染
# 完全同一种序列化）。**数值不在这里定**——它们属于共享预算策略
# ``capabilities/result_budget.py``（spec §3.3），executor 的单结果闸和
# tool_loop 的同批水位读的是同一份。facade 只负责"按这个目标做结构化缩减"，
# 自己无权决定预算：只在这一层放宽，下游两道切串闸会原样砍回去。
SEARCH_RESULT_MAX_CHARS_ENV = result_budget.HISTORY_SEARCH_RESULT_MAX_CHARS_ENV
FETCH_RESULT_MAX_CHARS_ENV = result_budget.HISTORY_FETCH_RESULT_MAX_CHARS_ENV
DEFAULT_SEARCH_RESULT_MAX_CHARS = (
    result_budget.DEFAULT_HISTORY_SEARCH_RESULT_MAX_CHARS)
DEFAULT_FETCH_RESULT_MAX_CHARS = (
    result_budget.DEFAULT_HISTORY_FETCH_RESULT_MAX_CHARS)

# facade 层的 dispatch 拒绝（开关关闭）。executor 的 lane 拒绝用同一个词面，
# 模型两处看到的都是 "error: tool_not_allowed"。
TOOL_NOT_ALLOWED = "tool_not_allowed"

# 「这次调用一行原文都没读过」的完整错误码清单（spec §5 的保守结算）。
#
# 回合预算按**可信 metadata** 结算；拿不到计数时（capability 异常被 executor
# 吞成 capability_failed、metadata 通道丢了）必须按 lease 上限保守扣，否则多轮
# 快速失败就能绕过 1500 行累计闸。这个集合是那条规则的唯一豁免：这些码全部
# 在第一次 enclave POST **之前**就返回了，扣它们等于凭空惩罚一次打字错误。
#
# 判据只有一条：**这个码的所有产生点都在 enclave 调用之前**。拿不准就别加进来
# ——漏加的代价是多扣几行，错加的代价是预算闸被绕过。
#
# 🔎 逐项 witness（2026-08-09 重新逐个核过；改这张表时必须重新走一遍，
#    "看着像入参错误"不算证据，要指到具体 raise/return 点）：
#
#   tool_not_allowed             本模块 search()/fetch() 的第一行；executor 的
#                                dispatch gate（executor.py 的 history 分支）。
#                                两处都在进入 readside 之前。
#   capability_invalid_input     本模块 fetch() 的 message_id 空校验；以及
#     (errors.INVALID)           HistorySearchInputError 的空 code 兜底——
#                                history_search 的每个 raise 都带非空 slug，
#                                所以这条实际只由前者产生。均在 readside 之前。
#   capability_unavailable       ①本模块 search() 的 derive_cursor_hmac_key()
#     (errors.UNAVAILABLE)       失败（还没碰 store/enclave）；
#                                ②_infra_err 把 enclave 的
#                                ``enclave_history_capability_unavailable``
#                                （HTTP 404 = 镜像没带 history 路由）翻成它。
#                                ②的产生点是 _post_enclave_history，而它在
#                                leaf-hints / scan / fetch 三处都会被调到——
#                                单看这里**推不出**"一定在扫描前"。所以 readside
#                                在第一次 scan 投递成功之后就不再让这个码逃出来
#                                （run_history_search 里改抛 enclave_error_after_scan
#                                → 走 UPSTREAM → 按 lease 满额扣）。**那段代码是
#                                本行豁免成立的前提，别删。**
#   cursor_invalid / cursor_mismatch
#                                decode_cursor / verify_cursor_binding /
#                                verify_cursor_request，全在扫描循环之前。
#   missing_query_or_time_range / invalid_limit
#                                run_history_search 进循环前的参数分支。
#   query_empty / query_too_long normalize_query（同上，循环前）。
#   invalid_time / invalid_time_range
#                                parse_rfc3339_utc / normalize_time_range（同上）。
#   invalid_neighbor_count       run_history_fetch 钳邻居数时，早于 store 查询。
#   not_found_or_not_visible     run_history_fetch 锚点查空，fetch POST 根本没发。
#
# ❌ **cursor_overflow 不在这里**（2026-08-09 移除）：它由 encode_cursor 抛出，
#    而 encode_cursor 在扫描循环**结束之后**才调用（history_readside 的
#    next_cursor 分支）。曾经错列在此，于是"enclave 已扫过 N 行 → cursor 编码
#    超长 → 结算 0 行"，同一个超长 user_id 反复调用即可绕过回合累计预算。
PRE_SCAN_ERROR_CODES = frozenset({
    TOOL_NOT_ALLOWED,
    errors.INVALID,
    errors.UNAVAILABLE,
    "cursor_invalid",
    "cursor_mismatch",
    "missing_query_or_time_range",
    "invalid_limit",
    "invalid_neighbor_count",
    "query_empty",
    "query_too_long",
    "invalid_time",
    "invalid_time_range",
    "not_found_or_not_visible",
})


# 回合剩余额度 → 本次调用的上限（spec §5）。worker 的 _HistoryTurnBudget 在
# dispatch 之前 reserve 出剩余额度并用 ``call_limits`` 钉住这一次调用；
# capability 在这里读。走 contextvar 而不是参数，是因为 run_capability 的签名
# 对全部 capability 共用，为一个工具加一个 kwarg 会波及所有实现；contextvar
# 由**可信的 worker** 设置，模型参数永远进不来，且 asyncio.to_thread 会把
# 上下文带进执行线程。未设置（wake/subagent/直连/测试）= 纯 env 预算，行为不变。
_CALL_LIMITS: contextvars.ContextVar[tuple[int, int] | None] = contextvars.ContextVar(
    "feedling_history_call_limits", default=None
)


@contextlib.contextmanager
def call_limits(*, max_rows: int, deadline_ms: int):
    """Pin one history call's ceiling to what is left of the chat turn."""
    token = _CALL_LIMITS.set((int(max_rows), int(deadline_ms)))
    try:
        yield
    finally:
        _CALL_LIMITS.reset(token)


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
    budget = history_readside.HistorySearchBudget(
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
    limits = _CALL_LIMITS.get()
    if limits is not None:
        budget = history_readside.clamp_to_remaining(
            budget, max_rows=limits[0], deadline_ms=limits[1])
    return budget


def _search_result_max_chars() -> int:
    return result_budget.for_tool(HISTORY_SEARCH_TOOL).result_cap


def _fetch_result_max_chars() -> int:
    return result_budget.for_tool(HISTORY_FETCH_TOOL).result_cap


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


# 骨架里每条消息保留的短摘录长度：够模型看出"这里有哪些消息、大概在说什么"，
# 不够的部分由第 ② 步按远近补回来。
_FETCH_EXCERPT_CHARS = 60


def _neighbors_nearest_first(out: dict) -> list[dict]:
    """锚点 → 贴身邻居 → 更远，交替向外。第 ② 步的补全顺序。"""
    ordered: list[dict] = []
    if isinstance(out.get("anchor"), dict):
        ordered.append(out["anchor"])
    before, after = out["before"], out["after"]
    for step in range(max(len(before), len(after))):
        if step < len(before):
            ordered.append(before[len(before) - 1 - step])
        if step < len(after):
            ordered.append(after[step])
    return ordered


def _shrink_fetch_result(result: dict, *, max_chars: int) -> dict:
    """序列化前把 fetch 结果缩到 ``max_chars`` 内（spec §3.2 的缩减顺序）。

    ① 先保证**所有**消息的结构 + 短摘录都在（让模型看得到这里有哪些消息）；
    ② 再按离锚点由近到远补全正文；
    ③ 最后才丢最远的消息，并如实计入 ``omitted_before``/``omitted_after``。

    顺序不能反：先砍最远邻居会恰好砍掉这次把窗口放宽到 15 想找回的前文线索
    ——"那家餐厅"的来龙去脉常常就在七八轮之前。锚点永远不丢。
    绝不序列化后切串。
    """
    out = dict(result)
    anchor = result.get("anchor")
    out["anchor"] = dict(anchor) if isinstance(anchor, dict) else anchor
    out["before"] = [dict(i) for i in (result.get("before") or [])]
    out["after"] = [dict(i) for i in (result.get("after") or [])]
    # 计数字段常驻：模型必须能区分"就这么多"和"删过"，不能靠字段在不在猜。
    # 从 readside 报上来的基数起算，**不是从 0 重置**：预算钳掉的行（回合租约
    # 只剩几行）和 enclave 因 deadline 没解成的行也已经"删过"了，重置等于把这
    # 两类删减吞掉，模型会以为窗口是完整的。
    out["omitted_before"] = max(0, int(result.get("omitted_before") or 0))
    out["omitted_after"] = max(0, int(result.get("omitted_after") or 0))
    if _rendered_chars(out) <= max_chars:
        return out

    # ① 全员降到短摘录。原文（连同 enclave 给的 content_truncated）留一份，
    # 第 ② 步按远近原样补回去。
    originals = {
        id(item): (item.get("content"), item.get("content_truncated"))
        for item in _neighbors_nearest_first(out)
        if isinstance(item.get("content"), str)
    }
    for item in _neighbors_nearest_first(out):
        _truncate_item_content(item, _FETCH_EXCERPT_CHARS)

    # ③（骨架都装不下才走）丢最远的：before 头部最旧 / after 尾部最新，长边
    # 优先，锚点永不参与。
    while _rendered_chars(out) > max_chars and (out["before"] or out["after"]):
        if out["before"] and len(out["before"]) >= len(out["after"]):
            out["before"].pop(0)
            out["omitted_before"] += 1
        else:
            out["after"].pop()
            out["omitted_after"] += 1
    if _rendered_chars(out) > max_chars and isinstance(out["anchor"], dict):
        # 只剩锚点还超：截它的摘录（这是最后一道，锚点本身绝不删）。
        for cap in (48, 24, 0):
            _truncate_item_content(out["anchor"], cap)
            if _rendered_chars(out) <= max_chars:
                break
        return out

    # ② 由近及远补全正文；某条补不进去不影响继续试更远的短消息。
    for item in _neighbors_nearest_first(out):
        content, truncated_flag = originals.get(id(item), (None, None))
        if not isinstance(content, str) or content == item.get("content"):
            continue
        excerpt = item.get("content")
        item["content"] = content
        if truncated_flag is None:
            item.pop("content_truncated", None)
        else:
            item["content_truncated"] = truncated_flag
        if _rendered_chars(out) > max_chars:
            item["content"] = excerpt
            item["content_truncated"] = True
    return out


def _shrunk_or_error(shrunk: dict, *, max_chars: int, kind: str) -> CapabilityResult:
    """缩减后复核一次尺寸：还超就返回错误结果，绝不把超限对象交下去。

    结构化缩减是有下界的——``next_cursor``（search 的三态语义载体）和锚点
    （fetch 的调用目的）永远不丢。cap 被配到那个下界以下时，缩减尽了力仍然
    超限，而下游两道闸都是**序列化后切串**：交下去等于让模型收到半截 JSON
    （``"{...[truncated]"``），既解析不了、也看不出发生了什么。

    此时唯一正确的输出是一条完整、稳定、合法的错误结果：模型至少知道"这次
    取回失败了"，可以换更窄的窗口重试。启动期的
    ``result_budget.validate_result_caps`` 已经把会走到这里的配置挡在门外，
    这层是纵深防御（env 在运行期被改、或未来新增 atomic 生产者时仍然兜得住）。
    """
    rendered = _rendered_chars(shrunk)
    if rendered <= max_chars:
        return ok(data=errors.cap_data(shrunk))
    return err(
        result_budget.OVERFLOW_ERROR_CODE,
        f"{kind} result does not fit its configured budget",
        retryable=False,
    )


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
    try:
        result = history_readside.run_history_search(
            str(store.user_id),
            cursor_hmac_key=cursor_key,
            query=params.get("query"),
            start=params.get("start"),
            end=params.get("end"),
            cursor=params.get("cursor"),
            # 原样传 None（= 模型省略了 limit）。折叠成默认值会让"续页只传
            # cursor"的契约检查看不出模型到底传没传，cursor+limit 就会被静默
            # 接受并改掉页大小。
            limit=params.get("limit"),
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
    max_chars = _search_result_max_chars()
    return _shrunk_or_error(
        _shrink_search_result(result, max_chars=max_chars),
        max_chars=max_chars, kind=HISTORY_SEARCH_TOOL)


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
            # spec §3.2 的不对称默认（before 15 / after 4）。
            before=(history_readside.FETCH_BEFORE_DEFAULT
                    if before is None else before),
            after=(history_readside.FETCH_AFTER_DEFAULT
                   if after is None else after),
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
    max_chars = _fetch_result_max_chars()
    return _shrunk_or_error(
        _shrink_fetch_result(result, max_chars=max_chars),
        max_chars=max_chars, kind=HISTORY_FETCH_TOOL)
