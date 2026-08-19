"""Backend 协调层：history search / fetch 的候选拉取 + enclave 批量投递。

形态照 ``memory_readside_core``：DB 拿原始密文候选 → 按批量节奏 POST enclave
的 ``/v1/history/*`` 读侧路由（enclave/routes/history.py）→ 组装 planner
（``history_search``）需要的输入/输出。本模块**不做**工具注册、不碰
executor/tool_loop/worker、不做回合级预算记账/串行锁（那是批次 3 的接线）；
对外只有两个入口：

    run_history_search(...)   spec §3.1/§4/§5 的单次调用（含 cursor 翻页）
    run_history_fetch(...)    spec §3.2 的锚点+邻居单窗口

预算值与 cursor HMAC key 全部显式入参（batch 3 的 facade 统一从 env 配置后
传进来），本层只消费不读环境；唯一的 env 读取在 key 派生 helper 与 enclave
URL（与 memory_readside_core.post_enclave_readside 同款）。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from dataclasses import dataclass, replace

import httpx

from core import envelope as core_envelope
import db
from enclave.routes import history as enclave_history
from model_api_runtime.v2 import history_search, jobs_store

# 与 serve_worker._ASSISTANT_ROLES 同一套历史 role 归一（V1 时代写 'agent'，
# 部分路径写 'assistant'，现行 'openclaw'）——模型侧只见 user/assistant 两类。
_ASSISTANT_ROLES = frozenset({"openclaw", "assistant", "agent"})

SNIPPET_MAX_CHARS = 240  # spec §3.1

# history_fetch 的邻居上限（各方向），spec §3.2。默认刻意不对称——before 15 /
# after 4，线索几乎总在前文里，对称分配等于把一半预算浪费在后文。
FETCH_NEIGHBORS_MAX = 15
FETCH_BEFORE_DEFAULT = 15
FETCH_AFTER_DEFAULT = 4

# cursor 签名 key 的 domain-separation 标签。风格对齐 serve_worker 的
# prompt-cache 派生（"feedling:v2:prompt-cache:v3"）。
_CURSOR_KEY_INFO = b"feedling:v2:history-cursor:v1"


def derive_cursor_hmac_key(secret: bytes | None = None) -> bytes:
    """每部署 cursor 签名 key：从 FEEDLING_RUNTIME_TOKEN_SECRET 单向派生。

    复用 runtime token 的部署密钥（backend V2 worker 与 enclave 同 env 注入，
    serve_worker 的 prompt-cache/route HMAC 派生已有同款先例），加独立
    domain-separation 标签：cursor key 推不回 token secret；token secret 轮换
    时所有在途 cursor 自然失效——预期行为，cursor 本就短命（默认 TTL 15min），
    模型按 cursor_invalid 重发首调用即可。
    """
    if secret is None:
        secret = (
            os.environ.get("FEEDLING_RUNTIME_TOKEN_SECRET", "")
            .strip()
            .encode("utf-8")
        )
    if not secret:
        raise RuntimeError("FEEDLING_RUNTIME_TOKEN_SECRET not set")
    return hmac.new(bytes(secret), _CURSOR_KEY_INFO, hashlib.sha256).digest()


@dataclass(frozen=True)
class HistorySearchBudget:
    """单次 history_search 调用的预算（spec §5 表；默认值即 spec 默认）。

    回合级累计（raw 1500 条 / 8s）与「每 round 最多 1 个 history 调用」在
    批次 3 的 turn closure 记账，不在这层。
    """

    raw_batch_rows: int = 128
    raw_batch_bytes: int = 512 * 1024
    call_max_rows: int = 512
    call_deadline_ms: int = 2500
    snippet_max_chars: int = SNIPPET_MAX_CHARS
    fetch_max_chars_per_message: int = 2000
    cursor_ttl_seconds: float = float(history_search.DEFAULT_CURSOR_TTL_SECONDS)


def clamp_to_remaining(
    budget: HistorySearchBudget, *, max_rows: int, deadline_ms: int
) -> HistorySearchBudget:
    """把单次调用的闸压到回合剩余额度以内（spec §5，只降不升）。

    回合闸（1500 行 / 8s）只在调用**之间**检查是不够的：剩 1 行时放行一次
    完整的 512 行扫描，一个回合最坏就能扫 2011 行。剩余额度必须在调用前钳进
    单次 budget。下限保 1：0 会让扫描循环空转一圈却什么都不做，而"剩 0"这种
    情况本来就该被 exhausted 拦在更前面。
    """
    return replace(
        budget,
        call_max_rows=max(1, min(int(budget.call_max_rows), int(max_rows))),
        call_deadline_ms=max(1, min(int(budget.call_deadline_ms), int(deadline_ms))),
    )


def _post_enclave_history(runtime_token: str | None, operation: str, payload: dict) -> dict:
    """POST 一次 enclave history 路由（同 post_enclave_readside 的运输形态）。"""
    enclave_url = os.environ.get("FEEDLING_ENCLAVE_URL", "").rstrip("/")
    if not enclave_url:
        raise RuntimeError("enclave_unavailable")
    if not runtime_token:
        raise RuntimeError("runtime_token_unavailable")
    try:
        with httpx.Client(timeout=20, verify=False) as client:
            resp = client.post(
                f"{enclave_url}/v1/history/{operation}",
                headers={"X-Feedling-Runtime-Token": runtime_token},
                json=payload,
            )
    except httpx.HTTPError as e:
        raise RuntimeError(f"enclave_error:{type(e).__name__}") from e
    if resp.status_code == 404:
        # 版本错位兜底（spec §6）：enclave 镜像还没带上 history 路由时明确
        # 报错，绝不静默当空结果。
        raise RuntimeError("enclave_history_capability_unavailable")
    if resp.status_code >= 400:
        raise RuntimeError(f"enclave_http_{resp.status_code}:{resp.text[:180]}")
    response = resp.json()
    if not isinstance(response, dict):
        raise RuntimeError("enclave_invalid_history_response")
    return response


_TEXT_ENVELOPE_FIELDS = ("body_ct", "nonce", "K_enclave")
_CAPTION_FIELDS = (
    "caption_id", "caption_v", "caption_body_ct", "caption_nonce",
    "caption_K_enclave", "caption_owner_user_id",
)


def _row_payload(row: dict) -> dict:
    """一行候选投给 enclave 的白名单投影。

    caption-only 硬契约（spec §7）：附件行（image/file）只带 caption 信封——
    body_ct 是原始二进制的密文（图片可达 MB 级），既会吹爆字节预算，也绝不该
    在检索路径解密；enclave 侧按 content_type 再兜一道。文本行只带正文信封。
    """
    ctype = str(row.get("content_type") or "text")
    out = {
        "id": row.get("id"),
        "seq": row.get("seq"),
        "ts": row.get("ts"),
        "role": row.get("role"),
        "content_type": ctype,
        "v": row.get("v", 1),
        "owner_user_id": row.get("owner_user_id"),
    }
    if ctype in ("image", "file"):
        for key in _CAPTION_FIELDS:
            if row.get(key) is not None:
                out[key] = row[key]
        if ctype == "file":
            out["file_name"] = row.get("file_name")
        if row.get("caption_body") is not None:
            out["caption_body"] = row.get("caption_body")
    else:
        shape = core_envelope.classify_envelope_shape(row)
        if shape == "sealed":
            for key in _TEXT_ENVELOPE_FIELDS:
                out[key] = row.get(key)
        elif shape == "plaintext_binary":
            out["body_b64"] = row.get("body_b64")
        elif shape == "plaintext_text":
            out["body"] = row.get("body")
    return out


def _row_is_plaintext(row: dict) -> bool:
    ctype = str(row.get("content_type") or "text")
    if ctype in ("image", "file"):
        return row.get("caption_body") is not None or not row.get("caption_body_ct")
    return core_envelope.classify_envelope_shape(row) in (
        "plaintext_text", "plaintext_binary")


def _local_row_text(row: dict, owner_user_id: str) -> str | None:
    ctype = str(row.get("content_type") or "text")
    if ctype in ("image", "file"):
        if row.get("caption_body") is None:
            return ""
        caption_owner = str(
            row.get("caption_owner_user_id") or row.get("owner_user_id") or "")
        if caption_owner != owner_user_id:
            return None
        return str(row.get("caption_body") or "")
    try:
        return core_envelope.read_plaintext_envelope_body(
            row, owner_user_id=owner_user_id).decode("utf-8", errors="replace")
    except ValueError:
        return None


def _local_scan_row(row: dict, owner_user_id: str, query: str, snippet_max: int) -> dict | None:
    text = _local_row_text(row, owner_user_id)
    if text is None or (query and query not in history_search.normalize_for_match(text)):
        return None
    display = text or enclave_history._attachment_marker(row)
    snippet, truncated = enclave_history._build_snippet(display, query, snippet_max)
    return {
        "message_id": str(row.get("id") or ""),
        "seq": int(row.get("seq") or 0),
        "ts": float(row.get("ts") or 0.0),
        "role": str(row.get("role") or ""),
        "snippet": snippet,
        "content_truncated": bool(truncated),
    }


def _project_sealed_row(row: dict) -> dict:
    projected = dict(row)
    projected.pop("body", None)
    projected.pop("body_b64", None)
    projected.pop("caption_body", None)
    return projected


def _scan_rows_partitioned(
    rows: list[dict],
    *,
    owner_user_id: str,
    query: str,
    stop_after_hits: int,
    deadline_ms: int,
    max_rows: int,
    max_ciphertext_bytes: int,
    snippet_max_chars: int,
    post,
) -> tuple[dict, int]:
    hits: list[dict] = []
    checked = 0
    unavailable = 0
    last_checked: int | None = None
    stopped = "exhausted"
    truncated = False
    posts = 0
    index = 0
    while index < len(rows) and checked < max_rows:
        row = rows[index]
        if _row_is_plaintext(row):
            checked += 1
            last_checked = int(row.get("seq") or 0)
            text = _local_row_text(row, owner_user_id)
            if text is None:
                unavailable += 1
            elif not query or query in history_search.normalize_for_match(text):
                hit = _local_scan_row(
                    row, owner_user_id, query, snippet_max_chars)
                if hit is not None:
                    hits.append(hit)
            index += 1
            if stop_after_hits and len(hits) >= stop_after_hits:
                stopped = "hits"
                break
            continue

        end = index + 1
        while end < len(rows) and not _row_is_plaintext(rows[end]):
            end += 1
        remaining_hits = max(0, stop_after_hits - len(hits))
        response = post("scan", {
            "rows": [_project_sealed_row(row) for row in rows[index:end]],
            "query": query,
            "stop_after_hits": remaining_hits,
            "deadline_ms": deadline_ms,
            "max_rows": max_rows - checked,
            "max_ciphertext_bytes": max_ciphertext_bytes,
            "snippet_max_chars": snippet_max_chars,
        })
        posts += 1
        part_checked = int(response.get("checked_count") or 0)
        checked += part_checked
        unavailable += int(response.get("unavailable_count") or 0)
        part_hits = response.get("hits") if isinstance(response.get("hits"), list) else []
        hits.extend(part_hits[:max(0, stop_after_hits - len(hits))])
        if response.get("last_checked_seq") is not None:
            last_checked = int(response["last_checked_seq"])
        part_stopped = str(response.get("stopped") or "exhausted")
        truncated = truncated or bool(response.get("truncated"))
        if part_checked < end - index or part_stopped != "exhausted":
            stopped = part_stopped
            break
        index = end

    if checked >= max_rows and index < len(rows):
        stopped = "budget"
        truncated = True
    return {
        "hits": hits,
        "checked_count": checked,
        "unavailable_count": unavailable,
        "last_checked_seq": last_checked,
        "stopped": stopped,
        "truncated": truncated,
    }, posts


def _payload_ciphertext_size(row_payload: dict) -> int:
    return len(str(row_payload.get("body_ct") or "")) + len(
        str(row_payload.get("caption_body_ct") or ""))


_CIPHERTEXT_PAYLOAD_FIELDS = frozenset(_TEXT_ENVELOPE_FIELDS) | frozenset(_CAPTION_FIELDS)


def _oversize_placeholder(row_payload: dict) -> dict:
    """一行自身就超字节闸时送出的无密文占位。

    密文一个字节都不送（这正是闸要挡的东西），但这一行仍然要出现在批里：
    enclave 把它记成 checked + unavailable 并推进 ``last_checked_seq``，
    cursor 才越得过去。整批直接不发它 = 下一页从同一位置重来 = 死循环。
    """
    out = {k: v for k, v in row_payload.items() if k not in _CIPHERTEXT_PAYLOAD_FIELDS}
    out["oversize"] = True
    return out


def _model_role(raw_role: str) -> str:
    return "assistant" if str(raw_role).strip().lower() in _ASSISTANT_ROLES else "user"


def run_history_search(
    user_id: str,
    *,
    cursor_hmac_key: bytes,
    query: str | None = None,
    start: str | None = None,
    end: str | None = None,
    cursor: str | None = None,
    limit: int | None = None,
    runtime_token: str | None = None,
    budget: HistorySearchBudget | None = None,
    now: float | None = None,
    post_enclave=None,
) -> dict:
    """单次 history_search 调用（spec §3.1 的返回结构）。

    异常契约：输入问题抛 ``history_search.HistorySearchInputError`` /
    ``CursorInvalid`` / ``CursorMismatch``（稳定 slug，facade 直接映射给模型）；
    enclave/基建问题抛 RuntimeError。
    """
    budget = budget or HistorySearchBudget()
    now_ts = time.time() if now is None else float(now)
    post = post_enclave or (
        lambda operation, payload: _post_enclave_history(runtime_token, operation, payload))
    # ``limit is None`` = 调用方省略（默认 3）；显式传值在带 cursor 时是契约
    # 冲突（见下面的 verify_cursor_request），所以这里必须保住"有没有传"的
    # 信号，不能提前折叠成默认值。
    try:
        limit_n = 3 if limit is None else int(limit)
    except (TypeError, ValueError):
        raise history_search.HistorySearchInputError("invalid_limit") from None
    limit_n = max(1, min(limit_n, 5))

    generation = int(db.get_runtime_generation(str(user_id)))
    if cursor:
        cur = history_search.decode_cursor(cursor, key=cursor_hmac_key, now=now_ts)
        history_search.verify_cursor_binding(
            cur, user_id=str(user_id), runtime_generation=generation)
        history_search.verify_cursor_request(
            cur, query=query, start=start, end=end, limit=limit)
        normalized_query = cur.query
        start_ts, end_ts = cur.start_ts, cur.end_ts
        snapshot = int(cur.snapshot_through_seq)
        resume_state = cur.scan_state()
    else:
        if query is None and start is None and end is None:
            raise history_search.HistorySearchInputError("missing_query_or_time_range")
        normalized_query = (
            history_search.normalize_query(query) if query is not None else "")
        start_ts, end_ts = history_search.normalize_time_range(start, end)
        # 首调用钉快照（spec §4）：其后每一页都在同一个 seq 世界里扫。
        snapshot = int(db.chat_max_seq(str(user_id)))
        resume_state = None

    state = resume_state if resume_state is not None else (
        history_search.initial_scan_state(snapshot_through_seq=snapshot))

    matches: list[dict] = []
    scanned = 0
    unavailable = 0
    # 成功投递过几次 /scan。只用来判定「此后的 enclave 错误还能不能算
    # pre-scan」，见循环里的 except 分支。
    scan_posts = 0
    deadline = time.monotonic() + budget.call_deadline_ms / 1000.0
    while (
        len(matches) < limit_n
        and scanned < budget.call_max_rows
        and not history_search.scan_complete(state)
    ):
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        if remaining_ms <= 0:
            break
        batch = history_search.next_batch(
            state,
            batch_limit=min(budget.raw_batch_rows, budget.call_max_rows - scanned))
        if batch is None:  # pragma: no cover — scan_complete 已排除
            break
        candidates = jobs_store.chat_history_candidate_rows(
            str(user_id),
            min_seq=batch.min_seq,
            max_seq=batch.max_seq,
            start_ts=start_ts,
            end_ts=end_ts,
            limit=batch.limit,
        )
        if not candidates:
            state = history_search.advance_scan_state(
                state, batch, exhausted=True)
            continue
        full_rows = jobs_store.chat_history_rows_by_seqs(
            str(user_id), [row["seq"] for row in candidates])
        # 字节预算截前缀（降序 = 最新优先）：没送进去的低位候选留给下一页——
        # advance 用 last_checked_seq 恢复，正好落在截断点上。
        # 判定必须在这一行**加进批之前**：先加后判等于每批都允许超支一整行，
        # 首条 5 MiB 的密文就这样穿过 512 KiB 的闸飞向 enclave。
        payload_rows: list[dict] = []
        used_bytes = 0
        for row in full_rows:
            projected = _row_payload(row)
            size = _payload_ciphertext_size(projected)
            if payload_rows and used_bytes + size > budget.raw_batch_bytes:
                break
            if size > budget.raw_batch_bytes:
                # 这一行自己就超闸：只送不含密文的占位（见 _oversize_placeholder），
                # 本批到此为止。
                payload_rows.append(_oversize_placeholder(projected))
                break
            payload_rows.append(projected)
            used_bytes += size
        try:
            response, posts_used = _scan_rows_partitioned(
                payload_rows,
                owner_user_id=str(user_id),
                query=normalized_query,
                stop_after_hits=limit_n - len(matches),
                deadline_ms=max(1, remaining_ms),
                max_rows=budget.raw_batch_rows,
                max_ciphertext_bytes=budget.raw_batch_bytes,
                snippet_max_chars=budget.snippet_max_chars,
                post=post,
            )
        except RuntimeError as exc:
            # 一旦有过成功的 scan 投递，本次调用就**已经解密过原文**，此后再冒出
            # 的 enclave 错误绝不能带上 route-missing 那个词面：facade 会把它翻成
            # capability_unavailable，而那是 PRE_SCAN_ERROR_CODES 里的豁免码
            # （结算 0 行）。换成普通 upstream 错误 → 按 lease 满额保守结算。
            # 版本错位的真实形态是「第一次投递就 404」，那条路径不受影响。
            if scan_posts and str(exc) == "enclave_history_capability_unavailable":
                raise RuntimeError("enclave_error_after_scan") from exc
            raise
        scan_posts += posts_used
        checked = int(response.get("checked_count") or 0)
        scanned += checked
        unavailable += int(response.get("unavailable_count") or 0)
        hits = response.get("hits") if isinstance(response.get("hits"), list) else []
        for hit in hits:
            if len(matches) >= limit_n:
                break
            matches.append({
                "message_id": str(hit.get("message_id") or ""),
                "ts": float(hit.get("ts") or 0.0),
                "role": _model_role(str(hit.get("role") or "")),
                "snippet": str(hit.get("snippet") or ""),
                "content_truncated": bool(hit.get("content_truncated")),
            })
        last_checked = response.get("last_checked_seq")
        if last_checked is None:
            # enclave 一行都没来得及检查（deadline 立即到）：state 原地不动，
            # 下一页从同一位置重来——绝不凭空推进 cursor。
            break
        covered_whole_window = (
            checked >= len(payload_rows)
            and len(payload_rows) == len(full_rows)
            and len(candidates) < batch.limit
        )
        if covered_whole_window:
            state = history_search.advance_scan_state(
                state, batch, exhausted=True)
        else:
            state = history_search.advance_scan_state(
                state, batch, last_checked_seq=int(last_checked))

    scan_done = history_search.scan_complete(state)

    next_cursor: str | None = None
    if not scan_done:
        out_cursor = history_search.HistoryCursor(
            user_id=str(user_id),
            snapshot_through_seq=snapshot,
            runtime_generation=generation,
            query=normalized_query,
            start_ts=start_ts,
            end_ts=end_ts,
            resume_seq=state.resume_seq,
            expires_at=now_ts + float(budget.cursor_ttl_seconds),
        )
        next_cursor = history_search.encode_cursor(out_cursor, key=cursor_hmac_key)

    result = {
        "matches": matches,
        "complete": bool(scan_done),
        "scanned_count": scanned,
        "unavailable_count": unavailable,
    }
    if next_cursor is not None:
        result["next_cursor"] = next_cursor
    return result


def _fetch_item(item: dict) -> dict:
    out = dict(item)
    out["role"] = _model_role(str(item.get("role") or ""))
    out.pop("seq", None)
    return out


def _local_fetch_item(row: dict, owner_user_id: str, max_chars: int) -> dict:
    ctype = str(row.get("content_type") or "text")
    item = {
        "message_id": str(row.get("id") or ""),
        "seq": int(row.get("seq") or 0),
        "ts": float(row.get("ts") or 0.0),
        "role": str(row.get("role") or ""),
    }
    if ctype != "text":
        item["content_type"] = ctype
    text = _local_row_text(row, owner_user_id)
    if text is None:
        item["content"] = None
        item["unavailable"] = True
        return item
    content = text or (
        enclave_history._attachment_marker(row)
        if ctype in ("image", "file") else "")
    item["content"] = content[:max_chars]
    item["content_truncated"] = len(content) > max_chars
    return item


def run_history_fetch(
    user_id: str,
    *,
    message_id: str,
    before: int = 15,
    after: int = 4,
    runtime_token: str | None = None,
    budget: HistorySearchBudget | None = None,
    post_enclave=None,
) -> dict:
    """单窗口锚点取回（spec §3.2）。

    锚点不可见/不存在统一返回 ``{"error": "not_found_or_not_visible"}``；
    序列化总闸（≤4500 字符）与结构化缩减在 facade（capabilities/history.py）。
    邻居数各钳 [0,15]，请求行数上限 31——31 条不保证都进最终 payload，装不下
    多少如实报进 ``omitted_before``/``omitted_after``。

    **回合租约在这里必须真的花掉**（spec §5 的「两工具合计」）：
    ``budget.call_deadline_ms`` 转发给 enclave 做逐行止损，
    ``budget.call_max_rows`` 钳邻居条数。只把 lease 收进 budget 就丢掉，
    等于回合闸只约束 search，fetch 想扫多少扫多少。
    """
    budget = budget or HistorySearchBudget()
    post = post_enclave or (
        lambda operation, payload: _post_enclave_history(runtime_token, operation, payload))
    try:
        n_before = max(0, min(int(before), FETCH_NEIGHBORS_MAX))
        n_after = max(0, min(int(after), FETCH_NEIGHBORS_MAX))
    except (TypeError, ValueError):
        raise history_search.HistorySearchInputError("invalid_neighbor_count") from None

    # 锚点单独占 1 行。保底 2 而不是 0：额度只剩一两行时把 fetch 削成"只回锚点"
    # 等于白跑一次（锚点的原文模型多半已经从 search 的 snippet 看过了），而
    # "剩 0"这种情况本来就该被回合 exhausted 拦在更前面。
    # 优先 before——这个产品问题（"那家餐厅叫什么"）几乎总是往前找线索。
    neighbor_allowance = max(int(budget.call_max_rows) - 1, 2)
    keep_before = min(n_before, neighbor_allowance)
    keep_after = min(n_after, max(0, neighbor_allowance - keep_before))

    anchor = jobs_store.chat_history_anchor_row(str(user_id), str(message_id))
    if anchor is None:
        return {"error": "not_found_or_not_visible"}
    # ``before_requested``/``after_requested`` 是模型要的窗口，``before``/``after``
    # 是预算钳后真正取密文的条数。store 用前者数出「这个窗口里真有几条」，
    # omitted_* 只认这个 witness（见 _omitted_by_budget）。
    older, newer, available = jobs_store.chat_history_neighbor_rows(
        str(user_id), int(anchor["seq"]),
        before=keep_before, after=keep_after,
        before_requested=n_before, after_requested=n_after)
    rows = [anchor, *older, *newer]
    local_by_id = {
        str(row.get("id") or ""): _local_fetch_item(
            row, str(user_id), budget.fetch_max_chars_per_message)
        for row in rows
        if _row_is_plaintext(row)
    }
    sealed_rows = [row for row in rows if not _row_is_plaintext(row)]
    response = {
        "anchor": None,
        "before": [],
        "after": [],
        "unavailable_count": 0,
    }
    if sealed_rows:
        if not _row_is_plaintext(anchor):
            sealed_anchor = anchor
            sealed_before = [row for row in older if not _row_is_plaintext(row)]
            sealed_after = [row for row in newer if not _row_is_plaintext(row)]
        else:
            sealed_anchor = sealed_rows[0]
            sealed_before = sealed_rows[1:]
            sealed_after = []
        response = post("fetch", {
            "anchor": _project_sealed_row(_row_payload(sealed_anchor)),
            "before": [
                _project_sealed_row(_row_payload(row)) for row in sealed_before
            ],
            "after": [
                _project_sealed_row(_row_payload(row)) for row in sealed_after
            ],
            "max_chars_per_message": budget.fetch_max_chars_per_message,
            "deadline_ms": max(1, int(budget.call_deadline_ms)),
        })
    sealed_items = []
    if isinstance(response.get("anchor"), dict):
        sealed_items.append(response["anchor"])
    sealed_items.extend(
        response.get("before") if isinstance(response.get("before"), list) else [])
    sealed_items.extend(
        response.get("after") if isinstance(response.get("after"), list) else [])
    items_by_id = {
        str(item.get("message_id") or ""): item
        for item in [*local_by_id.values(), *sealed_items]
        if isinstance(item, dict)
    }
    anchor_item = items_by_id.get(str(anchor.get("id") or ""))
    before_items = [
        items_by_id[str(row.get("id") or "")]
        for row in older if str(row.get("id") or "") in items_by_id
    ]
    after_items = [
        items_by_id[str(row.get("id") or "")]
        for row in newer if str(row.get("id") or "") in items_by_id
    ]
    return {
        "anchor": _fetch_item(anchor_item) if isinstance(anchor_item, dict) else None,
        "before": [_fetch_item(i) for i in before_items if isinstance(i, dict)],
        "after": [_fetch_item(i) for i in after_items if isinstance(i, dict)],
        "unavailable_count": int(response.get("unavailable_count") or 0),
        # 预算钳掉的 + enclave 侧（deadline / hard cap）没解成的，合并如实上报。
        # facade 的结构化缩减会在这个基数上继续累加，不是从 0 重置。
        "omitted_before": (
            _omitted_by_budget(available.get("before"), len(older))
            + max(0, int(response.get("omitted_before") or 0))),
        "omitted_after": (
            _omitted_by_budget(available.get("after"), len(newer))
            + max(0, int(response.get("omitted_after") or 0))),
    }


def _omitted_by_budget(available, sent: int) -> int:
    """预算钳掉了几条 = 请求窗口里**真实存在**的条数 − 这次取回的条数。

    ``available`` 是 store 数出来的 witness（``chat_history_neighbor_rows`` 的
    第三个返回值），不是从"取回条数"推断的。推断在两个方向上都会说谎：

    * ``keep_after == 0``（低余额时 before 优先，after 一条都不取）——按"没取满
      就是还有"推，恒报 ``omitted_after = 4``，哪怕锚点就是最后一条消息；
    * ``returned == kept``——分不清"正好只有这些"和"后面确实还有"。

    两种假信号都会让模型为不存在的邻居再翻一页，与 ``omitted_*`` 的契约
    （"实际因预算删掉了多少"）直接冲突。
    """
    return max(0, int(available or 0) - int(sent))
