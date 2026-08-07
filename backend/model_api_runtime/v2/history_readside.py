"""Backend 协调层：history search / fetch 的候选拉取 + enclave 批量投递。

形态照 ``memory_readside_core``：DB 拿密文候选/叶子 → 按批量节奏 POST enclave
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
from dataclasses import dataclass

import httpx

import db
from model_api_runtime.v2 import history_search, jobs_store

# 与 serve_worker._ASSISTANT_ROLES 同一套历史 role 归一（V1 时代写 'agent'，
# 部分路径写 'assistant'，现行 'openclaw'）——模型侧只见 user/assistant 两类。
_ASSISTANT_ROLES = frozenset({"openclaw", "assistant", "agent"})

SNIPPET_MAX_CHARS = 240  # spec §3.1

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

    leaf_call_max_leaves: int = 64
    leaf_call_max_bytes: int = 256 * 1024
    raw_batch_rows: int = 128
    raw_batch_bytes: int = 512 * 1024
    call_max_rows: int = 512
    call_deadline_ms: int = 2500
    snippet_max_chars: int = SNIPPET_MAX_CHARS
    fetch_max_chars_per_message: int = 2000
    cursor_ttl_seconds: float = float(history_search.DEFAULT_CURSOR_TTL_SECONDS)


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
    else:
        for key in _TEXT_ENVELOPE_FIELDS:
            out[key] = row.get(key)
    return out


def _payload_ciphertext_size(row_payload: dict) -> int:
    return len(str(row_payload.get("body_ct") or "")) + len(
        str(row_payload.get("caption_body_ct") or ""))


def _model_role(raw_role: str) -> str:
    return "assistant" if str(raw_role).strip().lower() in _ASSISTANT_ROLES else "user"


def _leaf_partition(leaves: list[dict]) -> tuple[list[dict], list[dict]]:
    exact = [l for l in leaves if str(l.get("coverage_kind") or "") == "exact"]
    legacy = [l for l in leaves if str(l.get("coverage_kind") or "") == "legacy_opaque"]
    return exact, legacy


def _leaf_hint_ranges(
    leaves: list[dict],
    normalized_query: str,
    *,
    budget: HistorySearchBudget,
    post,
) -> tuple[tuple[tuple[int, int], ...], bool]:
    """解密叶子摘要拿命中段。返回 (exact 命中段, legacy 叶子是否命中)。

    确定性要求（cursor 契约）：叶子列表来自同一 snapshot（append-only +
    end_seq<=snapshot 过滤）、预算同值 → 每一页重算出的命中段一致，
    ``_covered_intervals`` 才能在翻页间稳定重建（见 history_search.ScanShape
    的注释）。预算值由 facade 保持 env 稳定。
    """
    if not normalized_query or not leaves:
        return (), False
    # recent-first 已由 list_level0_summary_leaves 的 end_seq DESC 保证；超出
    # 单调用叶子预算的部分不送（enclave 同预算再 clamp 一道），由 raw 兜底扫。
    picked: list[dict] = []
    used = 0
    for leaf in leaves:
        if len(picked) >= budget.leaf_call_max_leaves or used >= budget.leaf_call_max_bytes:
            break
        env = leaf.get("summary_envelope")
        used += len(str((env or {}).get("body_ct") or "")) if isinstance(env, dict) else 0
        picked.append({
            "segment_id": leaf.get("segment_id"),
            "coverage_kind": leaf.get("coverage_kind"),
            "start_seq": leaf.get("start_seq"),
            "end_seq": leaf.get("end_seq"),
            "summary_envelope": env,
        })
    response = post("leaf-hints", {
        "leaves": picked,
        "query": normalized_query,
        "max_leaves": budget.leaf_call_max_leaves,
        "max_ciphertext_bytes": budget.leaf_call_max_bytes,
    })
    hits = response.get("hits") if isinstance(response.get("hits"), list) else []
    ranges = tuple(
        (int(h.get("start_seq") or 0), int(h.get("end_seq") or 0))
        for h in hits
        if isinstance(h, dict) and int(h.get("start_seq") or 0) > 0
    )
    legacy_hit = bool(response.get("legacy_opaque_hits"))
    return ranges, legacy_hit


def run_history_search(
    user_id: str,
    *,
    cursor_hmac_key: bytes,
    query: str | None = None,
    start: str | None = None,
    end: str | None = None,
    cursor: str | None = None,
    limit: int = 3,
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
    try:
        limit_n = int(limit)
    except (TypeError, ValueError):
        raise history_search.HistorySearchInputError("invalid_limit") from None
    limit_n = max(1, min(limit_n, 5))

    generation = int(db.get_runtime_generation(str(user_id)))
    if cursor:
        cur = history_search.decode_cursor(cursor, key=cursor_hmac_key, now=now_ts)
        history_search.verify_cursor_binding(
            cur, user_id=str(user_id), runtime_generation=generation)
        history_search.verify_cursor_request(cur, query=query, start=start, end=end)
        normalized_query = cur.query
        start_ts, end_ts = cur.start_ts, cur.end_ts
        snapshot = int(cur.snapshot_through_seq)
        watermark = int(cur.summary_watermark_seq)
        resume_state = cur.scan_state()
    else:
        if query is None and start is None and end is None:
            raise history_search.HistorySearchInputError("missing_query_or_time_range")
        normalized_query = (
            history_search.normalize_query(query) if query is not None else "")
        start_ts, end_ts = history_search.normalize_time_range(start, end)
        # 首调用钉快照（spec §4）：其后每一页都在同一个 seq 世界里扫。
        snapshot = int(db.chat_max_seq(str(user_id)))
        frontier = jobs_store.get_summary_frontier_state(str(user_id))
        watermark = int((frontier or {}).get("watermark_seq") or 0)
        resume_state = None

    # 叶子列表在 query / 纯时间两种模式都取：query 模式做提示扫描，两种模式
    # 都要 legacy_opaque 元数据做 coverage_gap 判定（旧 retention 清理过的
    # 区间，raw 扫不到不等于"历史里没有"）。
    leaves = jobs_store.list_level0_summary_leaves(str(user_id), through_seq=snapshot)
    _exact_leaves, legacy_leaves = _leaf_partition(leaves)
    leaf_hit_ranges: tuple[tuple[int, int], ...] = ()
    if normalized_query:
        # 原始 end_seq 降序整单送入（exact 与 legacy 混排）：预算截断的
        # recent-first 语义要以叶子的真实时间序为准，不能按 kind 重排。
        leaf_hit_ranges, _legacy_query_hit = _leaf_hint_ranges(
            leaves,
            normalized_query,
            budget=budget,
            post=post,
        )

    shape = history_search.ScanShape(
        snapshot_through_seq=snapshot,
        summary_watermark_seq=watermark,
        has_query=bool(normalized_query),
        leaf_hit_ranges=leaf_hit_ranges,
    )
    state = resume_state if resume_state is not None else (
        history_search.initial_scan_state(shape))

    matches: list[dict] = []
    scanned = 0
    unavailable = 0
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
            shape, state,
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
                shape, state, batch, exhausted=True)
            continue
        full_rows = jobs_store.chat_history_rows_by_seqs(
            str(user_id), [row["seq"] for row in candidates])
        # 字节预算截前缀（降序 = 最新优先）：没送进去的低位候选留给下一页——
        # advance 用 last_checked_seq 恢复，正好落在截断点上。
        payload_rows: list[dict] = []
        used_bytes = 0
        for row in full_rows:
            projected = _row_payload(row)
            payload_rows.append(projected)
            used_bytes += _payload_ciphertext_size(projected)
            if used_bytes >= budget.raw_batch_bytes:
                break
        response = post("scan", {
            "rows": payload_rows,
            "query": normalized_query,
            "stop_after_hits": limit_n - len(matches),
            "deadline_ms": max(1, remaining_ms),
            "max_rows": budget.raw_batch_rows,
            "max_ciphertext_bytes": budget.raw_batch_bytes,
            "snippet_max_chars": budget.snippet_max_chars,
        })
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
                shape, state, batch, exhausted=True)
        else:
            state = history_search.advance_scan_state(
                shape, state, batch, last_checked_seq=int(last_checked))

    scan_done = history_search.scan_complete(state)

    # coverage_gap（spec §4）：legacy_opaque 叶子覆盖的原文可能已被旧 retention
    # 清理——其覆盖区一条可见 raw 行都不剩时，扫完也不能声称"历史里没有"。
    coverage_gap = False
    if legacy_leaves:
        legacy_through = max(
            max(int(l.get("legacy_opaque_through_seq") or 0), int(l.get("end_seq") or 0))
            for l in legacy_leaves
        )
        if legacy_through >= 1:
            probe = jobs_store.chat_history_candidate_rows(
                str(user_id), min_seq=1, max_seq=min(legacy_through, snapshot), limit=1)
            coverage_gap = not probe

    # 语义三态（spec §3.1）：complete=true 扫完且无缺口；complete=false+cursor
    # 还能翻页；complete=false 无 cursor 且 coverage_gap=true → 原文已不存在，
    # 结论不确定。
    next_cursor: str | None = None
    if not scan_done:
        out_cursor = history_search.HistoryCursor(
            user_id=str(user_id),
            snapshot_through_seq=snapshot,
            summary_watermark_seq=watermark,
            runtime_generation=generation,
            query=normalized_query,
            start_ts=start_ts,
            end_ts=end_ts,
            phase=state.phase,
            resume_seq=state.resume_seq,
            uncompressed_floor=state.uncompressed_floor,
            expires_at=now_ts + float(budget.cursor_ttl_seconds),
        )
        next_cursor = history_search.encode_cursor(out_cursor, key=cursor_hmac_key)
    complete = bool(scan_done and not coverage_gap)

    result = {
        "matches": matches,
        "complete": complete,
        "scanned_count": scanned,
        "unavailable_count": unavailable,
        "coverage_gap": bool(coverage_gap),
    }
    if next_cursor is not None:
        result["next_cursor"] = next_cursor
    return result


def _fetch_item(item: dict) -> dict:
    out = dict(item)
    out["role"] = _model_role(str(item.get("role") or ""))
    out.pop("seq", None)
    return out


def run_history_fetch(
    user_id: str,
    *,
    message_id: str,
    before: int = 2,
    after: int = 2,
    runtime_token: str | None = None,
    budget: HistorySearchBudget | None = None,
    post_enclave=None,
) -> dict:
    """单窗口锚点取回（spec §3.2）。

    锚点不可见/不存在统一返回 ``{"error": "not_found_or_not_visible"}``；
    序列化总闸（≤1600 字符）与结构化缩减在批次 3 的 facade。
    """
    budget = budget or HistorySearchBudget()
    post = post_enclave or (
        lambda operation, payload: _post_enclave_history(runtime_token, operation, payload))
    try:
        n_before = max(0, min(int(before), 4))
        n_after = max(0, min(int(after), 4))
    except (TypeError, ValueError):
        raise history_search.HistorySearchInputError("invalid_neighbor_count") from None

    anchor = jobs_store.chat_history_anchor_row(str(user_id), str(message_id))
    if anchor is None:
        return {"error": "not_found_or_not_visible"}
    older, newer = jobs_store.chat_history_neighbor_rows(
        str(user_id), int(anchor["seq"]), before=n_before, after=n_after)
    response = post("fetch", {
        "anchor": _row_payload(anchor),
        "before": [_row_payload(row) for row in older],
        "after": [_row_payload(row) for row in newer],
        "max_chars_per_message": budget.fetch_max_chars_per_message,
    })
    anchor_item = response.get("anchor")
    before_items = response.get("before") if isinstance(response.get("before"), list) else []
    after_items = response.get("after") if isinstance(response.get("after"), list) else []
    return {
        "anchor": _fetch_item(anchor_item) if isinstance(anchor_item, dict) else None,
        "before": [_fetch_item(i) for i in before_items if isinstance(i, dict)],
        "after": [_fetch_item(i) for i in after_items if isinstance(i, dict)],
        "unavailable_count": int(response.get("unavailable_count") or 0),
    }
