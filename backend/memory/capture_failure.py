"""落卡失败的**逃生阀**判断：同一个游标连续失败到阈值，就跳过那一批。

V1（``proactive.capture_scheduler``）和 V2（``model_api_runtime.v2.jobs_store``）
共用这里的纯函数。放在 memory 层而不是 proactive 里，是因为 V2 的存储层
不该反向依赖 V1 的调度模块；两边各写一遍又必然漂，而漂了不报错
（2026-09-13 就漂过一次：两处各想漏了同一个边界）。

这里只算「状态该怎么改」，不读写数据库、不发事件。
"""
from __future__ import annotations

import re
from typing import Any, Callable, Mapping


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default



#: 同一个窗口连续失败几次之后，**跳过它**并把游标推过去。
#:
#: ## 为什么必须有这个闸
#:
#: 落卡只在成功时推进游标。一条让模型吐出非法 JSON 的消息（比如它引用用户
#: 原话时把半角引号写进 JSON 字符串却不转义）会造成队头阻塞：
#:
#:     解析失败 → 游标不动 → 下次还从同一条消息开始 → 又失败 → 永远
#:
#: 结果是那个用户**从此不再有任何新记忆**，而且每一步都"正常失败"、有退避、
#: 没有告警。2026-09-12 实测：prod 上 152 个有落卡活动的用户里，
#: 58 个处于这个状态（连续两天 0 成功）。
#:
#: 阈值取 3 是为了区分两种失败：provider 超时/5xx 这类是偶发的，重试就好；
#: 解析失败是确定性的 —— 同样的输入必然同样失败，重试一万次也一样。
#: 连续 3 次同一窗口失败，基本只可能是后者。
#:
#: 🔴 跳过意味着**那一批对话的记忆永久丢了**。这是有意的取舍：丢一批，
#: 换这个用户后面还能继续记。但必须**大声记下来**（V1 见 capture_scheduler._record_skipped_window，V2 见 jobs_store._capture_fail_on_cursor），
#: 否则就成了又一处"静默丢数据"。
CAPTURE_POISON_SKIP_AFTER = 3

#: 其它失败（存不进去、provider 抖动、拿不到密钥…）要连续失败这么多次才跳过。
#:
#: ## 为什么要分两档（2026-09-14 prod 实测）
#:
#: 上面那个 3 次的阈值，是按「解析失败是确定性的」设计的 —— 同样的输入必然
#: 同样的输出，重试一万次也一样，早跳早好。
#:
#: 但上线后发现：引号修好之后，同一批窗口能解析了，却在**写入**那步栽了
#: （``capture_shared_envelope_requires_enclave_key``）。而这类失败**重试是能好的**：
#:
#:     第一批  失败 失败 成功          ← 第三次过了，记忆保住
#:     第二批  失败 失败 失败 → 跳过   ← 被 3 次阈值跳掉，这 9 条消息的记忆丢了
#:
#: 用确定性失败的阈值去处理会自己好的失败，就是在丢本来保得住的记忆。
#: 所以非确定性失败多给几次机会；但仍然**有上限** —— 万一某批的写入失败其实是
#: 确定性的，用户也不能被永久卡死（那正是这整套逃生阀要修的问题）。
CAPTURE_TRANSIENT_SKIP_AFTER = 6

#: 走 3 次快速跳过的失败原因前缀。**只列确定性的**：同样的窗口必然同样失败。
#:
#: 🔴 用白名单不用黑名单：遇到一个没见过的失败原因时，默认走保守的 6 次档。
#: 反过来的话，新冒出来的一类会自己好的失败会被 3 次就跳掉，又开始悄悄丢记忆。
#:
#: V1 报的是裸原因（``json_decode_error:JSONDecodeError``），V2 会先归一成
#: ``extraction_failed:json_decode_error``。两种写法比对前都会先剥掉
#: ``extraction_failed:`` 前缀（见 skip_threshold_for）—— 不剥的话 V2 的解析
#: 失败一律落进 6 次档，快速跳过在 V2 上等于没接。
DETERMINISTIC_FAILURE_KINDS = (
    "json_decode_error",
    "no_json_object",
    "not_an_object",
    "invalid_card",
    "format_error",
    "missing_cards_list",
)

_V2_FAILURE_SCOPE = "extraction_failed:"

#: 问题出在**账号或模型服务**上、不在这批消息里的失败：不按次数跳过。
#:
#: 跳过只对「这批消息本身有毒」有用。账号坏了（余额不足、密钥失效、登录过期）
#: 或服务不可用时跳过这批，下一批照样失败、照样被跳 —— 账号坏多久，那段时间的
#: 记忆就丢多久；而不跳过的话，用户充值/重新登录后积压的记忆能补上。
#:
#: 2026-09-13 prod 实测：触发过旧逃生阀的 42 人里 41 人后续仍失败，**0 人是引号复发**，
#: 41 人全卡在模型调用：33 人是自己的账号问题（密钥失效/余额不足），其余是渠道/上游不可用。
#:
#: V2 用 extraction 的公开 provider 分类（剥掉 ``extraction_failed:`` 后）。
#: 🔴 ``provider_config`` **不在此列**：provider_client 把 400/415/422 全归到它，
#: 其中包括「消息太长超出上下文」这种内容引起的失败 —— 当账号问题永不跳过会把人永久卡死。
ACCOUNT_FAILURE_KINDS = frozenset({
    "auth_invalid",
    "quota_insufficient",
    "model_not_found",
    "rate_limited",
    "upstream_unavailable",
})

#: V1 报的是 CLI/模型原始错误文本，交给仓库统一的错误对照表（notices.error_contract）认 ——
#: App 给用户弹的原因提示也查这张表，两边不会再各认各的（上一版自己列关键词，漏了
#: 「Not logged in · Please run /login」）。这里列的是对照表里属于账号/服务的那些类别。
ACCOUNT_ERROR_CONTRACT_CODES = frozenset({
    "quota_insufficient",
    "provider_account_expired",
    "auth_invalid",
    "model_not_found",
    "rate_limited",
    "upstream_unavailable",
    "resident_agent_cli_logged_out",
    "cli_config_invalid",
})

#: 对照表之外、prod 上出现过或 V1 consumer 自己也按账号问题处理的原始文本
#: （``"invalid key" in lowered`` → provider_auth；「Insufficient balance」是某中转站的原话），
#: 见 account_error_code。

#: 账号/服务类失败**同一批**持续这么久仍没好，才跳过。
#:
#: 为什么不是「永不跳过」：有的模型服务「内容被拒」和「密钥无效」回的都是 403，
#: 被判成账号问题的失败里可能混着真正的毒消息。永不跳过 = 这类用户永久卡死，
#: 正是逃生阀要修的问题。7 天足够用户发现提示、去充值或重新登录；
#: 退避间隔会拉长到最多 6 小时一次，7 天内也就重试几十次。
CAPTURE_ACCOUNT_SKIP_AFTER_SEC = 7 * 86400


#: 账号类失败的 7 天计时，中间超过这么久没有新失败就重新计时。
#: 退避上限是 6 小时，正常重试时两次失败间隔不会超过它；留 4 倍余量。
ACCOUNT_CLOCK_GAP_RESET_SEC = 24 * 3600


#: 明确是我们这边的故障前缀（剥掉 extraction_failed: 之后比对）。见 account_error_code。
OUR_SIDE_FAILURE_PREFIXES = (
    "database_pool_timeout",
    "capture_memory_write_failed",
    "memory_write_rejected",
    # 平台回收崩溃/卡死任务时记的码（V2 租约回收器和 watchdog，见
    # jobs_store._recover_capture_claim）。worker 挂了或卡住是我们这边的问题；
    # 不在这里先认掉的话，错误对照表会因为字面里有 timeout 把它认成「模型服务不可用」，
    # 提示就会让用户以为是自己的模型服务坏了。
    "lease_timeout",
    "slot_watchdog_timeout",
    "watchdog_requeue_exhausted",
)


#: V2 provider 解析失败里**用户自己要去设置里修**的那几种（hosted/config_store）。
#: worker 记成 ``provider_setup:<slug>``。解密失败、runtime token 签发失败是我们的问题，不在此列。
PROVIDER_SETUP_USER_ERRORS = frozenset({
    "model_api_not_configured",
    "model_api_not_tested",
    "model_api_key_envelope_missing",
    "model_api_config_invalid",
})
PROVIDER_SETUP_ACCOUNT_CODE = "provider_setup"


def window_key(window: Mapping | None) -> str:
    """这次失败卡在哪个**游标**上。用来判断"和上次是同一个队头阻塞吗"。

    🔴 只看起点（``after_message_id``），**不能带终点**。

    卡住的是起点：游标停在毒消息前面不动。而终点会随新消息不断前移 ——
    把终点也算进身份的话：

        第 1 次  msg_a → msg_c  失败   key = "msg_a|msg_c"
                 新消息进来
        第 2 次  msg_a → msg_d  失败   key = "msg_a|msg_d"  ← 变了
                 streak 被重置成 1，永远到不了阈值

    结果是逃生阀永远不触发，用户还是被永久卡死 —— 也就是这个改动本来要修的
    那个问题。（我第一版就是这么写的，被变异测试抓出来。）

    起点有两种写法：消息 id，或者 seq。新用户第一次落卡时游标还没有 id
    （``after_message_id`` 为空、``after_seq`` 为 0）—— 这时要用 seq 当身份，
    **不能回落到终点**，否则又是上面那个「终点前移 → 永远不触发」。
    （0 也是合法的起点，别拿 truthy 判断把它吞掉。）

    退化情况：两种起点都拿不到时才回落到终点+seq，至少"同一批消息反复失败"
    仍能被识别。
    """
    w = window if isinstance(window, Mapping) else {}
    after = str(w.get("after_message_id") or "")[:160]
    if after:
        return f"after:{after}"
    after_seq = w.get("after_seq")
    if after_seq is not None and after_seq != "":
        try:
            return f"after_seq:{max(0, int(float(after_seq)))}"
        except (TypeError, ValueError):
            pass
    until = str(w.get("until_message_id") or "")[:160]
    seq = int(_safe_float(w.get("through_seq"), 0.0))
    return f"until:{until}|{seq}" if (until or seq) else ""


def poison_skip_patch(state: Mapping, window: Mapping | None, *,
                       now_ts: float,
                       threshold: int = CAPTURE_POISON_SKIP_AFTER) -> dict | None:
    """同一窗口连续失败到阈值 → 返回"把游标推过它"的补丁；否则 None。

    返回 None 时调用方照旧只累加 streak。
    """
    w = window if isinstance(window, Mapping) else {}
    until_id = str(w.get("until_message_id") or "")[:160]
    if not until_id:
        # 拿不到窗口终点就没法安全跳过 —— 宁可继续卡着，也不要把游标推到
        # 一个我们说不清的位置。
        return None
    streak = int(_safe_float(state.get("capture_fail_streak"), 0.0)) + 1
    if streak < threshold:
        return None
    through_seq = max(0, int(_safe_float(w.get("through_seq"), 0.0)))
    return {
        "last_captured_until_message_id": until_id,
        "last_captured_until_ts": _safe_float(w.get("until_ts"), 0.0),
        # 🔴 V1 的窗口（capture_scheduler._current_window）**没有 through_seq**。
        # 以前这里照样写 seq=0 + 已初始化，调度器从此信任这个 0，从历史起点
        # 重新发现消息（Codex 2026-09-14 抓到，V1 逃生阀上线时就带着）。
        # 拿不到可靠 seq 时标成「未初始化」：下次读游标会按 until_message_id
        # 重新翻译出真实 seq —— 和老数据升级走的是同一条路。
        "last_captured_until_seq": through_seq,
        "capture_seq_initialized": through_seq > 0,
        # 跳过之后 streak 归零：下一批是干净的，不该带着旧账退避。
        "capture_fail_streak": 0,
        "capture_parse_fail_streak": 0,
        "capture_window_fail_count": 0,
        "capture_account_fail_since": 0.0,
        "capture_fail_window_key": "",
        "capture_skipped_windows": max(
            0, int(_safe_float(state.get("capture_skipped_windows"), 0.0))
        ) + 1,
        "last_capture_skipped_at": now_ts,
    }


#: 「服务不可用」的强证据：明确的 5xx 状态语境、超时、连接失败、过载。见 account_error_code。
_STRONG_UPSTREAM_EVIDENCE = re.compile(
    r"provider_http_5\d\d"
    r"|(?:http|status|status[_ ]code|api error|error code|returned|responded)\W{0,3}5\d\d\b"
    r"|\b5\d\d\s+(?:internal server error|bad gateway|service unavailable|gateway time-?out)"
    r"|timed?[ _-]?out|timeout|connection (?:refused|reset|error|aborted)"
    r"|service unavailable|bad gateway|overloaded|temporarily unavailable"
    # 对照表里已认定的其他上游瞬时故障形状（Codex 第 7 轮：漏了会在第 6 次被跳过）
    r"|unreachable|stream disconnected|ended without finish_reason",
    re.IGNORECASE,
)


def account_error_code(reason: str) -> str:
    """账号/服务类失败对应错误对照表里的哪一类（如 ``quota_insufficient``）；不是账号类返回空串。

    用于：① 判断不按次数跳过 ② 给用户的提示里说清原因（「额度不足，充值后即可恢复」）。
    两处用同一个判断，不会出现「按账号问题处理了、提示却只说连续失败」。
    """
    raw = str(reason or "").strip()
    text = raw.lower()
    kind = text[len(_V2_FAILURE_SCOPE):] if text.startswith(_V2_FAILURE_SCOPE) else text
    if any(kind.startswith(p) for p in DETERMINISTIC_FAILURE_KINDS):
        return ""
    if any(kind.startswith(p) for p in OUR_SIDE_FAILURE_PREFIXES):
        # 我们自己的数据库/写库超时，不是用户的模型服务 —— 不能提示「你的模型服务不可用」，
        # 也不该按账号类等 7 天（独立审查）。
        return ""
    if kind in ACCOUNT_FAILURE_KINDS:
        return kind
    if kind.startswith(PROVIDER_SETUP_ACCOUNT_CODE + ":"):
        return PROVIDER_SETUP_ACCOUNT_CODE
    # 先认对照表之外的原话：prod 上某中转站回「401 {"error":"Insufficient balance"}」，
    # 对照表按 401 认成「密钥无效」，提示就会让用户去重新填 key，而真实原因是没钱了。
    if "insufficient balance" in text:
        return "quota_insufficient"
    from notices import error_contract  # 延迟导入：只在失败路径上用

    spec = error_contract.classify_text(raw)
    if spec is not None and spec.code in ACCOUNT_ERROR_CONTRACT_CODES:
        # 对照表的「服务不可用」是给聊天报错用的，裸三位 5 开头数字就算（``\b5\d{2}\b``）。
        # 逃生阀要更严：「max_tokens must be <= 500」「rejected at byte 512」是请求/内容问题，
        # 误判成服务故障会把 6 次兜底拖成 7 天（Codex 第 6 轮复现）。
        if spec.code == "upstream_unavailable" and not (
            _STRONG_UPSTREAM_EVIDENCE.search(raw)
            # 中转站通用 403「Request failed. Please try again later.」—— 对照表按形状锚定在
            # 开头，这里原因可能带着 capture_agent_call_failed: 等前缀，所以不锚定再认一次。
            or re.search(error_contract._GENERIC_UPSTREAM_403_SHAPE, raw)
        ):
            return ""
        return spec.code
    if "invalid key" in text:
        return "auth_invalid"
    return ""


def failure_class(reason: str) -> str:
    """一次失败属于哪类：``parse``（坏 JSON，连续 3 次快跳）/ ``account``（账号或服务，
    持续 7 天才跳）/ ``other``（6 次兜底）。"""
    text = str(reason or "").strip().lower()
    kind = text[len(_V2_FAILURE_SCOPE):] if text.startswith(_V2_FAILURE_SCOPE) else text
    if any(kind.startswith(p) for p in DETERMINISTIC_FAILURE_KINDS):
        return "parse"
    return "account" if account_error_code(reason) else "other"


def skip_threshold_for(reason: str) -> int:
    """这次失败要连续失败几次才跳过。见 CAPTURE_TRANSIENT_SKIP_AFTER 的说明。"""
    text = str(reason or "").strip().lower()
    if text.startswith(_V2_FAILURE_SCOPE):
        text = text[len(_V2_FAILURE_SCOPE):]
    if any(text.startswith(p) for p in DETERMINISTIC_FAILURE_KINDS):
        return CAPTURE_POISON_SKIP_AFTER
    return CAPTURE_TRANSIENT_SKIP_AFTER



def windowless_failure_patch(state, *, now_ts: float, reason: str = "") -> dict:
    """说不清是哪个窗口的失败（平台回收崩溃任务、批次丢失、老任务没带窗口…）怎么改状态。

    只累加总连续失败数（退避 + 提示），**永不跳过**。三个按窗口数的子计数分别这样处理：

    - ``capture_parse_fail_streak``：它数的是**连续的**解析失败。中间夹了一次非解析失败，
      连续性就断了，必须清零 —— 否则「解析 ×2 → worker 崩溃 → 解析 ×1」会被当成连续 3 次
      解析失败，立刻跳过这一批（Codex 第 12 轮复现）。带窗口的「其他」失败本来就清零，这里对齐。
      说不清窗口的**解析**失败保持不动：不知道是不是同一批，既不能接着数、也没理由打断。
    - ``capture_window_fail_count``：同一窗口里非账号失败的**累计**数（6 次兜底），不是连续计数，
      带窗口的账号失败也不清它。说不清窗口的失败不能算进某一批（崩溃多半是我们的问题，
      算进去就等于拿平台故障去凑跳过次数），也没有理由清掉已经数到的次数 —— 保持不动。
    - ``capture_account_fail_since``（7 天计时）：带窗口的非账号失败不重置它，这里同样不动；
      计时的「空窗期」判断看 ``last_capture_failed_at``，这次失败会刷新它 —— 期间确实在重试，
      不算空窗，和带窗口的失败一致。说不清窗口的账号失败也不开始计时（没有窗口可锚）。
    """
    patch: dict[str, Any] = {
        "capture_fail_streak": int(_safe_float(state.get("capture_fail_streak"), 0.0)) + 1,
        "capture_account_error_code": account_error_code(reason),
        "last_capture_failed_at": now_ts,
    }
    if failure_class(reason) != "parse":
        patch["capture_parse_fail_streak"] = 0
    return patch


def capture_failure_patch(state, window, *, now_ts: float, reason: str = ""):
    """一次落卡失败要怎么改状态。返回 ``(补丁, streak, 是否跳过)``。

    三种情形：

        拿不到窗口标识  → 照老行为累加 streak，**绝不跳过**
        同一个游标      → 累加；到阈值就跳过
        换了游标        → streak 从 1 重数

    🔴 第一种必须保持老行为。落卡退避告警是按 streak 到 3 才发的
    （见 tests/test_memory_backoff_notice.py），拿不到窗口时如果把 streak
    重置成 1，**整个退避机制就哑了** —— 那是我第一版引入的回归，CI 抓到的。

    ## 两档阈值各数各的

    ``capture_window_fail_count`` 数同一窗口里**非账号类**的失败，到 6 次兜底跳过；
    ``capture_parse_fail_streak`` 只数**连续的**解析类失败，到 3 次快速跳过，
    中间夹一次别的失败就清零。账号类失败不计次数，同一批从第一次账号类失败起持续
    ``CAPTURE_ACCOUNT_SKIP_AFTER_SEC``（7 天）仍失败才跳（``capture_account_fail_since``）。
    ``capture_fail_streak`` 仍是退避和告警用的总连续失败数。

    以前两档共用一个 streak、阈值只看本次原因，于是

        写入失败 → 写入失败 → 解析失败   streak=3，按「解析 3 次」立刻跳

    一次解析失败就继承了前两次写入失败，绕过了 6 次保护（Codex 第四轮抓到）。

    抽成一个函数是因为 V1 / V2 两条线各写一遍必然漂，而漂了不报错。
    """
    key = window_key(window)
    if not key:
        # 说不清是哪个窗口 —— 只累加，不跳过（跳过需要知道推到哪）。
        patch = windowless_failure_patch(state, now_ts=now_ts, reason=reason)
        return (patch, int(patch["capture_fail_streak"]), False)
    same = key == str(state.get("capture_fail_window_key") or "")
    kind = failure_class(reason)
    # capture_fail_streak 仍是退避/告警用的总连续失败数，语义不变。
    streak = (int(_safe_float(state.get("capture_fail_streak"), 0.0)) + 1
              if same else 1)
    prev_parse = (int(_safe_float(state.get("capture_parse_fail_streak"), 0.0))
                  if same else 0)
    prev_count = (int(_safe_float(state.get("capture_window_fail_count"), 0.0))
                  if same else 0)
    parse_streak = prev_parse + 1 if kind == "parse" else 0
    # 账号类失败**不计入**次数：否则余额不足失败 8 次、充值后再偶发一次超时，
    # 就会因为「已经 9 次了」立刻跳掉。账号类按**持续时间**算，见 CAPTURE_ACCOUNT_SKIP_AFTER_SEC。
    window_count = prev_count if kind == "account" else prev_count + 1
    prev_since = (_safe_float(state.get("capture_account_fail_since"), 0.0)
                  if same else 0.0)
    last_failed = _safe_float(state.get("last_capture_failed_at"), 0.0)
    if prev_since and last_failed and now_ts - last_failed > ACCOUNT_CLOCK_GAP_RESET_SEC:
        # 中间很久没失败（用户关了落卡、VPS 离线一周…）：那段时间没在重试，不算「持续失败」。
        # 否则「429 一次 → 关掉落卡 8 天 → 重开又 429 一次」就会立刻跳过（独立审查复现）。
        prev_since = 0.0
    account_since = (prev_since or now_ts) if kind == "account" else prev_since
    account_expired = (kind == "account" and account_since > 0
                       and now_ts - account_since >= CAPTURE_ACCOUNT_SKIP_AFTER_SEC)
    if account_expired or (kind != "account" and (
            parse_streak >= CAPTURE_POISON_SKIP_AFTER
            or window_count >= CAPTURE_TRANSIENT_SKIP_AFTER)):
        # 阈值 1 只是复用「推游标」的补丁；该不该跳已经在这里判断过。
        skip = poison_skip_patch(state, window, now_ts=now_ts, threshold=1)
    else:
        skip = None
    account_code = account_error_code(reason) if kind == "account" else ""
    if skip is not None:
        return ({**skip, "capture_account_error_code": account_code,
                 "last_capture_failed_at": now_ts}, streak, True)
    return ({"capture_fail_streak": streak,
             "capture_account_error_code": account_code,
             "capture_parse_fail_streak": parse_streak,
             "capture_window_fail_count": window_count,
             "capture_account_fail_since": account_since,
             "capture_fail_window_key": key,
             "last_capture_failed_at": now_ts}, streak, False)


#: 落卡**真正成功**（游标推进）时要清掉的整套失败子状态。V1 两个记录函数和 V2 提交共用。
#:
#: 以前只清 streak 和失败时间，``capture_account_error_code`` 等留着：
#:
#:     余额不足失败 → 成功提交 → 连续 3 次「批次丢失」（服务端问题）
#:     → 提示读到残留的 quota_insufficient，告诉用户「额度不足」（Codex 第 8 轮复现）
SUCCESS_RESET_PATCH: dict[str, Any] = {
    "capture_fail_streak": 0,
    "last_capture_failed_at": 0.0,
    "capture_account_error_code": "",
    "capture_parse_fail_streak": 0,
    "capture_window_fail_count": 0,
    "capture_account_fail_since": 0.0,
    "capture_fail_window_key": "",
}


def window_from_batch_row(batch: Mapping[str, Any]) -> dict[str, Any]:
    """V2 持久批次行（v2_capture_batches）→ 逃生阀认得的窗口。只取游标字段，不碰内容。

    prepared 批次重试、以及提交时被语义拒绝，这两处手上都只有批次行、没有
    worker 那边算好的窗口 —— 不还原的话窗口是空壳，逃生阀永远不触发。
    """
    return {
        "after_message_id": str(batch.get("after_message_id") or "")[:160],
        "after_seq": max(0, int(_safe_float(batch.get("after_seq"), 0.0))),
        "until_message_id": str(batch.get("until_message_id") or "")[:160],
        "until_ts": _safe_float(batch.get("until_ts"), 0.0),
        "through_seq": max(0, int(_safe_float(batch.get("through_seq"), 0.0))),
    }


def window_after_seq(window: Mapping[str, Any] | None) -> int:
    """窗口起点 seq（缺失/非法按 0）。"""
    w = window if isinstance(window, Mapping) else {}
    return max(0, int(_safe_float(w.get("after_seq"), 0.0)))


def frontier_seq(state: Mapping[str, Any] | None,
                 translate_message_id: Callable[[str], Any]) -> int:
    """「已经记到第几条了」—— 所有读落卡进度的地方都必须用这一个函数。

    进度有两份记法：数字 ``last_captured_until_seq`` 和消息 id
    ``last_captured_until_message_id``。两份本该一致，但有几条路只更新了 id：
    V1 的窗口没有 seq、V1 旧逃生阀跳过时把 seq 写成 0（prod 上 42 人触发过）。
    以前三处各读各的（worker 读数字、提交读数字、V1 调度器按标志位二选一），
    数字和 id 对不上时就会：

        worker 按 id 算出从第 N 条开始 → 提交时读到数字 0 → 对不上 → 提交被拒 → 永远重来

    规则：两份都是「已经处理过」的边界，**取靠后的那个**。数字可信（已初始化，
    或老数据只有数字）才参与比较；id 查不到（被清理）时只看数字。
    ``translate_message_id`` 由调用方给（查 chat_messages 的 seq），本模块不碰数据库。
    """
    st = state if isinstance(state, Mapping) else {}
    stored_usable = bool(st.get("capture_seq_initialized")) or (
        "capture_seq_initialized" not in st and "last_captured_until_seq" in st
    )
    stored = (max(0, int(_safe_float(st.get("last_captured_until_seq"), 0.0)))
              if stored_usable else 0)
    message_id = str(st.get("last_captured_until_message_id") or "")
    translated = 0
    if message_id:
        translated = max(0, int(_safe_float(translate_message_id(message_id), 0.0)))
    return max(stored, translated)
