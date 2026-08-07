# backend/enclave/routes/history.py
"""history 读侧三路由（history search spec §4/§5/§7 的 enclave 侧）。

形态照 memory.py 的 readside 批处理：auth.extract_auth → resolve_read_caller →
content_sk_or_503 → payload 校验 → anyio.to_thread 里逐行解密过滤 → JSONResponse。
匹配规则与 backend 侧 planner 同源——直接 import
``model_api_runtime.v2.history_search.normalize_for_match``（刻意的纯模块，
零 IO），query 在 API 边缘和 enclave 内走同一套 NFKC+casefold+空白归一，
否则全角/大小写差异会造成假阴性。

预算契约（spec §5）：预算数值全部从 payload 收（backend 统一配置），本模块只
负责执行；行数/字节另有本地硬上限 clamp 一道，防调用方 bug 把无界批次塞进来。
内部路由：enclave app 本就不出 OpenAPI（docs/openapi_url 全关）。
"""

from __future__ import annotations

import time
import unicodedata

import anyio.to_thread
from fastapi import APIRouter
from starlette.requests import Request
from starlette.responses import JSONResponse

from enclave import auth, envelope
from enclave.routes._body import read_json_payload
from enclave.routes._errors import content_sk_or_503
from model_api_runtime.v2.history_search import normalize_for_match

router = APIRouter()

# 调用方预算的硬上限（clamp，不是默认值——默认值由 backend 侧统一配置后随
# payload 送来）。数值锚点：spec §5 的「单调用 raw 512 条」「叶子 ≤64/256KiB」。
_LEAF_ROWS_HARD_MAX = 256
_LEAF_BYTES_HARD_MAX = 1 << 20          # 1 MiB 摘要密文
_SCAN_ROWS_HARD_MAX = 512               # spec §5 单调用 raw 上限
_SCAN_BYTES_HARD_MAX = 2 << 20          # 2 MiB
_FETCH_ROWS_HARD_MAX = 16               # 1 anchor + 4 before + 4 after 的富余
_SNIPPET_HARD_MAX = 240                 # spec §3.1
_DEADLINE_MS_HARD_MAX = 30_000
_FETCH_CONTENT_HARD_MAX = 4_000

# 测试可注入的单调时钟（deadline 判定用；见 test_v2_history_readside.py）。
_monotonic = time.monotonic


def _clamped_int(payload: dict, key: str, *, default: int, lo: int, hard_max: int) -> int:
    raw = payload.get(key)
    try:
        value = int(raw) if raw is not None else int(default)
    except (TypeError, ValueError):
        value = int(default)
    return max(int(lo), min(value, int(hard_max)))


def _ciphertext_size(row: dict) -> int:
    """字节预算的计量：正文密文 + caption 密文的近似（b64 串长度）。"""
    return len(str(row.get("body_ct") or "")) + len(str(row.get("caption_body_ct") or ""))


def _caption_envelope(row: dict) -> dict | None:
    """从 ``caption_*`` 前缀字段重建 caption 信封；无密文时 None。

    镜像 chat.py:_decrypt_caption / serve_worker._caption_envelope：AEAD AAD 是
    ``owner_user_id||v||id``，**必须**用 ``caption_id``（不是消息自己的 id），
    用错 id 会 AEAD 校验失败。
    """
    ct = str(row.get("caption_body_ct") or "").strip()
    if not ct:
        return None
    v = row.get("caption_v", row.get("v", 1))
    return {
        "id": row.get("caption_id") or row.get("id"),
        "v": int(v or 1),
        "body_ct": ct,
        "nonce": row.get("caption_nonce"),
        "K_enclave": row.get("caption_K_enclave"),
        "owner_user_id": row.get("caption_owner_user_id") or row.get("owner_user_id"),
    }


def _attachment_marker(row: dict) -> str:
    if str(row.get("content_type") or "") == "file":
        return f"[file: {str(row.get('file_name') or 'file')}]"
    return "[image]"


def _decrypt_row_text(row: dict, authorized_user_id: str, content_sk):
    """一行候选的可匹配文本。返回 str（可为空）；None = unavailable。

    caption-only 硬契约（spec §7）：图片/文件行**只解密 caption 信封，绝不碰
    body_ct**——附件 body 明文是原始二进制（图片字节 / PDF 字节），既不该在
    检索路径出 enclave，也会把解码直接炸掉（serve_worker._file_row 的教训）。
    调用方投影时已剥掉附件行的 body 密文，这里再按 content_type 分支兜一道。
    """
    ctype = str(row.get("content_type") or "text")
    if ctype in ("image", "file"):
        cap_env = _caption_envelope(row)
        if cap_env is None:
            # 本就没有 caption：行仍然"检查过"（时间模式可命中，标记兜底），
            # 不算 unavailable。
            return ""
        try:
            return envelope.decrypt_envelope(
                cap_env, authorized_user_id, content_sk
            ).decode("utf-8", errors="replace")
        except envelope.DecryptFailure:
            return None
    if not row.get("body_ct") or not row.get("K_enclave"):
        return None
    try:
        return envelope.decrypt_envelope(
            row, authorized_user_id, content_sk
        ).decode("utf-8", errors="replace")
    except envelope.DecryptFailure:
        return None


def _build_snippet(text: str, normalized_query: str, max_chars: int) -> tuple[str, bool]:
    """围绕命中位置截取 ≤max_chars 的 snippet（spec §3.1）。

    匹配判定在完全归一化（NFKC+casefold+空白折叠）的文本上做，但 snippet 要
    尽量回显原文：先在 casefold 原文里定位，其次在 NFKC 原文里定位（casefold
    可能改变长度——ß→ss——窗口允许有轻微偏移，宁偏勿假）；只有空白折叠后才
    命中的情况定位不回原文位置，退回开头截取——诚实优先。
    返回 (snippet, content_truncated)。
    """
    src = str(text).strip()
    if normalized_query:
        hay, idx = src, src.casefold().find(normalized_query)
        if idx < 0:
            nfkc = unicodedata.normalize("NFKC", src)
            idx2 = nfkc.casefold().find(normalized_query)
            if idx2 >= 0:
                hay, idx = nfkc, min(idx2, max(0, len(nfkc) - 1))
        if idx >= 0:
            lead = max(0, idx - max_chars // 3)
            snippet = hay[lead:lead + max_chars]
            return snippet, (lead > 0 or len(hay) > lead + len(snippet))
    snippet = src[:max_chars]
    return snippet, len(src) > len(snippet)


@router.post("/v1/history/leaf-hints")
async def v1_history_leaf_hints(request: Request):
    """解密一批 level-0 摘要叶子并返回 query 命中段（扫描优先级提示）。

    exact 叶子命中 → 返回 (segment_id, start_seq, end_seq)；legacy_opaque 叶子
    命中 → 只返回标记（spec §4：无精确 source witness，绝不参与范围推断，其
    覆盖区由 raw 兜底 phase 扫）。
    """
    ctx = auth.extract_auth(request)
    user_id, error = await auth.resolve_read_caller(ctx)
    if error is not None:
        body, status = error
        return JSONResponse(body, status_code=status)
    content_sk, err_response = await content_sk_or_503()
    if err_response is not None:
        return err_response
    payload = await read_json_payload(request)
    leaves = payload.get("leaves")
    if not isinstance(leaves, list):
        return JSONResponse({"error": "leaves must be a list"}, status_code=400)
    query = normalize_for_match(str(payload.get("query") or ""))
    if not query:
        # 叶子提示只存在于 query 模式（时间模式跳过摘要提示，spec §4）。
        return JSONResponse({"error": "query required"}, status_code=400)
    max_leaves = _clamped_int(
        payload, "max_leaves", default=64, lo=1, hard_max=_LEAF_ROWS_HARD_MAX)
    max_bytes = _clamped_int(
        payload, "max_ciphertext_bytes",
        default=256 * 1024, lo=1024, hard_max=_LEAF_BYTES_HARD_MAX)

    def _work():
        hits: list[dict] = []
        legacy_hits: list[str] = []
        unavailable = 0
        checked = 0
        used_bytes = 0
        truncated = False
        for leaf in leaves:
            if not isinstance(leaf, dict):
                continue
            if checked >= max_leaves or used_bytes >= max_bytes:
                # 先到者停（spec §5）；没扫到的叶子由调用方的 raw 兜底覆盖。
                truncated = True
                break
            checked += 1
            env = leaf.get("summary_envelope")
            if isinstance(env, dict):
                used_bytes += len(str(env.get("body_ct") or ""))
            if not isinstance(env, dict) or not env.get("K_enclave"):
                unavailable += 1
                continue
            try:
                text = envelope.decrypt_envelope(
                    env, user_id or "", content_sk
                ).decode("utf-8", errors="replace")
            except envelope.DecryptFailure:
                unavailable += 1
                continue
            if query not in normalize_for_match(text):
                continue
            if str(leaf.get("coverage_kind") or "") == "legacy_opaque":
                legacy_hits.append(str(leaf.get("segment_id") or ""))
            else:
                hits.append({
                    "segment_id": leaf.get("segment_id"),
                    "start_seq": int(leaf.get("start_seq") or 0),
                    "end_seq": int(leaf.get("end_seq") or 0),
                })
        return hits, legacy_hits, checked, unavailable, truncated

    hits, legacy_hits, checked, unavailable, truncated = (
        await anyio.to_thread.run_sync(_work))
    return JSONResponse({
        "user_id": user_id,
        "hits": hits,
        "legacy_opaque_hits": legacy_hits,
        "checked_count": checked,
        "unavailable_count": unavailable,
        "truncated": truncated,
    })


@router.post("/v1/history/scan")
async def v1_history_scan(request: Request):
    """逐行解密一批候选并做归一化子串匹配（query 为空 = 纯时间模式，全命中）。

    ``last_checked_seq`` 是 cursor 正确性的硬规则载体（spec §4）：**最后一条
    实际检查过的候选 seq**。密集命中提前凑够 ``stop_after_hits`` 时它停在命中
    行本身，绝不跳到批末尾——同批剩余候选由调用方的下一页重新拿到。
    """
    ctx = auth.extract_auth(request)
    user_id, error = await auth.resolve_read_caller(ctx)
    if error is not None:
        body, status = error
        return JSONResponse(body, status_code=status)
    content_sk, err_response = await content_sk_or_503()
    if err_response is not None:
        return err_response
    payload = await read_json_payload(request)
    rows = payload.get("rows")
    if not isinstance(rows, list):
        return JSONResponse({"error": "rows must be a list"}, status_code=400)
    query = normalize_for_match(str(payload.get("query") or ""))
    max_rows = _clamped_int(
        payload, "max_rows", default=128, lo=1, hard_max=_SCAN_ROWS_HARD_MAX)
    max_bytes = _clamped_int(
        payload, "max_ciphertext_bytes",
        default=512 * 1024, lo=1024, hard_max=_SCAN_BYTES_HARD_MAX)
    snippet_max = _clamped_int(
        payload, "snippet_max_chars", default=240, lo=40, hard_max=_SNIPPET_HARD_MAX)
    stop_after = _clamped_int(
        payload, "stop_after_hits", default=0, lo=0, hard_max=500)
    deadline_ms = _clamped_int(
        payload, "deadline_ms", default=2500, lo=1, hard_max=_DEADLINE_MS_HARD_MAX)

    def _work():
        # deadline 检查必须在逐行循环里（spec §5）：外层 anyio.to_thread 的
        # 取消/超时停不掉一个正在同步解密的线程，唯一可靠的止损点在这里。
        deadline = _monotonic() + deadline_ms / 1000.0
        hits: list[dict] = []
        checked = 0
        unavailable = 0
        used_bytes = 0
        last_checked_seq: int | None = None
        stopped = "exhausted"
        truncated = False
        for row in rows:
            if not isinstance(row, dict):
                continue
            if checked >= max_rows or used_bytes >= max_bytes:
                truncated = True
                stopped = "budget"
                break
            if _monotonic() >= deadline:
                stopped = "deadline"
                break
            checked += 1
            used_bytes += _ciphertext_size(row)
            seq = int(row.get("seq") or 0)
            # 本行进入检查即推进 last_checked_seq：解密失败（unavailable）也算
            # "检查过"，否则 cursor 会永远卡在一条坏行前面重扫。
            last_checked_seq = seq
            text = _decrypt_row_text(row, user_id or "", content_sk)
            if text is None:
                unavailable += 1
                continue
            if query and query not in normalize_for_match(text):
                continue
            display = text or _attachment_marker(row)
            snippet, content_truncated = _build_snippet(display, query, snippet_max)
            hits.append({
                "message_id": str(row.get("id") or ""),
                "seq": seq,
                "ts": float(row.get("ts") or 0.0),
                "role": str(row.get("role") or ""),
                "snippet": snippet,
                "content_truncated": bool(content_truncated),
            })
            if stop_after and len(hits) >= stop_after:
                # 提前停：last_checked_seq 已停在本行（spec §4 硬规则）。
                stopped = "hits"
                break
        return hits, checked, unavailable, last_checked_seq, stopped, truncated

    hits, checked, unavailable, last_checked_seq, stopped, truncated = (
        await anyio.to_thread.run_sync(_work))
    return JSONResponse({
        "user_id": user_id,
        "hits": hits,
        "checked_count": checked,
        "unavailable_count": unavailable,
        "last_checked_seq": last_checked_seq,
        "stopped": stopped,
        "truncated": truncated,
    })


@router.post("/v1/history/fetch")
async def v1_history_fetch(request: Request):
    """解密锚点 + 邻居行（调用方已按 seq 序选好），结构照 spec §3.2。

    序列化 payload 的 1600 字符总闸和结构化缩减属批次 3 的 facade；这里只按
    ``max_chars_per_message`` 截单条正文并标 ``content_truncated``。
    """
    ctx = auth.extract_auth(request)
    user_id, error = await auth.resolve_read_caller(ctx)
    if error is not None:
        body, status = error
        return JSONResponse(body, status_code=status)
    content_sk, err_response = await content_sk_or_503()
    if err_response is not None:
        return err_response
    payload = await read_json_payload(request)
    anchor = payload.get("anchor")
    if not isinstance(anchor, dict):
        return JSONResponse({"error": "anchor must be an object"}, status_code=400)
    before = payload.get("before") or []
    after = payload.get("after") or []
    if not isinstance(before, list) or not isinstance(after, list):
        return JSONResponse(
            {"error": "before/after must be lists"}, status_code=400)
    max_chars = _clamped_int(
        payload, "max_chars_per_message",
        default=2000, lo=80, hard_max=_FETCH_CONTENT_HARD_MAX)
    # 邻居 clamp 保留离锚点最近的行：before 是旧→新（尾部贴锚点），after 是
    # 旧→新（头部贴锚点）。
    keep = max(0, _FETCH_ROWS_HARD_MAX - 1)
    before = [r for r in before if isinstance(r, dict)][-(keep // 2):]
    after = [r for r in after if isinstance(r, dict)][:keep - len(before)]

    def _work():
        unavailable = 0

        def _item(row: dict) -> dict:
            nonlocal unavailable
            ctype = str(row.get("content_type") or "text")
            item = {
                "message_id": str(row.get("id") or ""),
                "seq": int(row.get("seq") or 0),
                "ts": float(row.get("ts") or 0.0),
                "role": str(row.get("role") or ""),
            }
            if ctype != "text":
                item["content_type"] = ctype
            text = _decrypt_row_text(row, user_id or "", content_sk)
            if text is None:
                unavailable += 1
                item["content"] = None
                item["unavailable"] = True
                return item
            content = text or (_attachment_marker(row) if ctype in ("image", "file") else "")
            item["content"] = content[:max_chars]
            item["content_truncated"] = len(content) > max_chars
            return item

        anchor_item = _item(anchor)
        before_items = [_item(row) for row in before]
        after_items = [_item(row) for row in after]
        return anchor_item, before_items, after_items, unavailable

    anchor_item, before_items, after_items, unavailable = (
        await anyio.to_thread.run_sync(_work))
    return JSONResponse({
        "user_id": user_id,
        "anchor": anchor_item,
        "before": before_items,
        "after": after_items,
        "unavailable_count": unavailable,
    })
