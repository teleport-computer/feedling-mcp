"""Pure scan-planning + cursor-codec kernel for V2 history search.

History search treats raw ``chat_messages`` as the retrieval ground truth;
decrypted level-0 summary leaves are only a *scan-priority hint* (spec §4).
This module is deliberately pure — no database, envelope, enclave, or worker
imports — so the planner's resume rules and the cursor's authentication rules
can be tested without a running service, and the enclave side can reuse the
exact same normalization functions on decrypted plaintext.

Three responsibilities live here:

1. Input normalization — ``normalize_query`` / ``normalize_for_match`` share
   one rule set (NFKC + casefold + whitespace collapse) so a query normalized
   at the API edge matches haystack text normalized inside the enclave.
   RFC3339 parsing converts offset-carrying instants to UTC epoch floats.

2. Scan planner — given the cursor-pinned snapshot (``snapshot_through_seq``,
   ``summary_watermark_seq``) and the request shape, produce descending seq
   batches in spec §4 priority order:

       无 query（只有时间范围） → recent-first 直接扫原文（单 phase）
       有 query → ① 未压缩区间 (watermark, snapshot] 至多一批
                  ② 摘要叶子命中段（精确 exact 段范围，recent-first）
                  ③ 其余未扫范围 recent-first 兜底

   legacy_opaque 段没有精确 source witness，绝不进入 ②的范围推断——其覆盖区
   由 ③ 的 raw 兜底覆盖；raw 行缺失时由调用方置 ``coverage_gap``（本模块只
   负责保证 ③ 会走到那片区间）。

3. Cursor codec — an HMAC-SHA256-authenticated resumable snapshot.  The key
   is injected by the caller (enclave-held; key sourcing is out of scope
   here).  用户身份永远来自认证上下文，绝不从 cursor 取信：payload 里的
   ``user_id`` 只用于一致性校验，校验失败按不可区分的 ``cursor_invalid``
   拒绝（不向调用方确认"这是另一个用户的合法 cursor"）。

Cursor 正确性硬规则（spec §4）：批内密集命中提前凑够 limit 时，resume 位置
必须落在**最后一条实际检查过的候选 seq** 上——``advance_scan_state`` 只接受
``last_checked_seq``，绝不自动跳到批末尾，剩余命中由下一页重新覆盖。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import unicodedata
import zlib
from dataclasses import dataclass, replace
from datetime import datetime, timezone

# 128（而非最初的 256）：query 归一化后要原样进 cursor payload（续页匹配必须
# 拿得回原文），128 个全独立 CJK/4 字节字符的最坏情况实测仍稳在 CURSOR_MAX_CHARS
# 之内；256 时理论上可顶破 1024 触发 cursor_overflow（收得回但翻不了页）。
QUERY_MAX_CHARS = 128
CURSOR_MAX_CHARS = 1024
CURSOR_VERSION = 1
# 默认 cursor 生存期。上层可 env 覆盖；本模块只提供常量，不读环境。
DEFAULT_CURSOR_TTL_SECONDS = 15 * 60

PHASE_UNCOMPRESSED = "uncompressed"
PHASE_LEAF_HITS = "leaf_hits"
PHASE_RECENT = "recent"
PHASE_DONE = "done"
_PHASES = (PHASE_UNCOMPRESSED, PHASE_LEAF_HITS, PHASE_RECENT, PHASE_DONE)


class HistorySearchInputError(ValueError):
    """Stable-slug rejection of one user-supplied search parameter."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = str(code)
        self.detail = str(detail)
        super().__init__(self.code)


class CursorInvalid(ValueError):
    """Cursor cannot be trusted: format/signature/version/expiry/binding.

    Cross-user and stale-generation cursors are deliberately reported with
    this same slug — a rejected cursor must be indistinguishable from
    garbage, so the response never confirms that some other user's (or a
    pre-clear) cursor was otherwise well-formed.
    """

    code = "cursor_invalid"

    def __init__(self, detail: str = "") -> None:
        self.detail = str(detail)
        super().__init__(self.code)


class CursorMismatch(ValueError):
    """Cursor is authentic but the caller re-sent conflicting parameters."""

    code = "cursor_mismatch"

    def __init__(self, detail: str = "") -> None:
        self.detail = str(detail)
        super().__init__(self.code)


# ---------------------------------------------------------------------------
# 输入归一化（enclave 侧匹配复用同一套规则）
# ---------------------------------------------------------------------------


def normalize_for_match(raw: str) -> str:
    """NFKC + casefold + 空白归一。查询词和被匹配正文必须走同一个函数，
    否则全角/大小写/多空格差异会造成假阴性。无长度/非空校验——正文侧可为空。"""

    collapsed = " ".join(unicodedata.normalize("NFKC", str(raw)).casefold().split())
    return collapsed


def normalize_query(raw: str) -> str:
    """Validate + normalize one user-supplied query (spec §3.1).

    ≤256 字符在归一化前后都强制（NFKC 可能展开合字），归一化后必须非空。
    """

    text = str(raw)
    if len(text) > QUERY_MAX_CHARS:
        raise HistorySearchInputError("query_too_long")
    normalized = normalize_for_match(text)
    if not normalized:
        raise HistorySearchInputError("query_empty")
    if len(normalized) > QUERY_MAX_CHARS:
        raise HistorySearchInputError("query_too_long")
    return normalized


def parse_rfc3339_utc(raw: str) -> float:
    """Parse one RFC3339 instant into a UTC epoch float.

    RFC3339 要求显式 offset（``Z`` 或 ``±hh:mm``）；无 offset 的 naive 时间
    一律拒绝——相对时间由模型自己换算（spec §3.1）。
    """

    text = str(raw).strip()
    if not text:
        raise HistorySearchInputError("invalid_time")
    candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        raise HistorySearchInputError("invalid_time", text) from None
    if parsed.tzinfo is None:
        raise HistorySearchInputError("invalid_time", "missing_utc_offset")
    return parsed.astimezone(timezone.utc).timestamp()


def normalize_time_range(
    start: str | None, end: str | None
) -> tuple[float | None, float | None]:
    """Parse the optional [start, end) range; start inclusive, end exclusive."""

    start_ts = parse_rfc3339_utc(start) if start is not None else None
    end_ts = parse_rfc3339_utc(end) if end is not None else None
    if start_ts is not None and end_ts is not None and start_ts >= end_ts:
        raise HistorySearchInputError("invalid_time_range", "start_must_precede_end")
    return start_ts, end_ts


# ---------------------------------------------------------------------------
# 扫描 planner
# ---------------------------------------------------------------------------


def normalize_leaf_hit_ranges(
    ranges,
    *,
    summary_watermark_seq: int,
    snapshot_through_seq: int,
) -> tuple[tuple[int, int], ...]:
    """Clip/merge exact-leaf hit ranges into descending non-overlapping runs.

    只接受 exact 段的 (start_seq, end_seq)。legacy_opaque 段没有精确 source
    witness，调用方不得把它的覆盖区喂进来（spec §4）——那片区域由 recent
    兜底 phase 覆盖。范围裁剪到 [1, min(watermark, snapshot)]。
    """

    upper = min(int(summary_watermark_seq), int(snapshot_through_seq))
    clipped: list[tuple[int, int]] = []
    for raw_start, raw_end in ranges:
        start, end = int(raw_start), int(raw_end)
        if start <= 0 or end < start:
            raise ValueError("invalid leaf hit range")
        start, end = max(1, start), min(end, upper)
        if end >= start:
            clipped.append((start, end))
    clipped.sort(key=lambda item: item[1], reverse=True)
    merged: list[tuple[int, int]] = []
    for start, end in clipped:
        if merged and start <= merged[-1][1] + 1 and merged[-1][0] <= end + 1:
            merged[-1] = (min(start, merged[-1][0]), max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return tuple(merged)


@dataclass(frozen=True)
class ScanShape:
    """快照钉死的请求形态：planner 的全部只读输入。

    ``leaf_hit_ranges`` 每次翻页由调用方基于同一快照重新推导（叶子段
    append-only、cursor 钉住 snapshot，推导是确定性的），所以 cursor 里
    不需要携带范围列表。
    """

    snapshot_through_seq: int
    summary_watermark_seq: int
    has_query: bool
    leaf_hit_ranges: tuple[tuple[int, int], ...] = ()

    def __post_init__(self) -> None:
        snapshot = int(self.snapshot_through_seq)
        watermark = int(self.summary_watermark_seq)
        if snapshot < 0 or watermark < 0:
            raise ValueError("scan shape seqs must be >= 0")
        normalized = normalize_leaf_hit_ranges(
            self.leaf_hit_ranges,
            summary_watermark_seq=watermark,
            snapshot_through_seq=snapshot,
        )
        if not self.has_query and normalized:
            raise ValueError("leaf hits require a query")
        object.__setattr__(self, "snapshot_through_seq", snapshot)
        object.__setattr__(self, "summary_watermark_seq", watermark)
        object.__setattr__(self, "leaf_hit_ranges", normalized)


@dataclass(frozen=True)
class ScanState:
    """Resumable per-phase position; the only planner state a cursor stores.

    ``resume_seq``：当前 phase 内剩余区域的 inclusive 上界（扫描一律降序）。
    ``uncompressed_floor``：phase-① 那【至多一批】实际检查到的最低 seq；
    recent 兜底 phase 据此跳过 [floor, snapshot]，为 0 表示 ① 没跑过。
    """

    phase: str
    resume_seq: int
    uncompressed_floor: int = 0

    def __post_init__(self) -> None:
        if self.phase not in _PHASES:
            raise ValueError("unknown scan phase")
        if int(self.resume_seq) < 0 or int(self.uncompressed_floor) < 0:
            raise ValueError("scan state seqs must be >= 0")
        object.__setattr__(self, "resume_seq", int(self.resume_seq))
        object.__setattr__(self, "uncompressed_floor", int(self.uncompressed_floor))


@dataclass(frozen=True)
class ScanBatch:
    """One descending metadata-candidate window: seq ∈ [min_seq, max_seq]."""

    phase: str
    min_seq: int
    max_seq: int
    limit: int


def initial_scan_state(shape: ScanShape) -> ScanState:
    """Entry state for a fresh (non-cursor) call, spec §4 分流."""

    if not shape.has_query:
        # 只有时间范围：跳过摘要提示，全范围 recent-first 扫原文。ts 过滤
        # 由 SQL 层做，planner 只管 seq 区间与顺序。
        state = ScanState(PHASE_RECENT, shape.snapshot_through_seq)
    else:
        state = ScanState(PHASE_UNCOMPRESSED, shape.snapshot_through_seq)
    return _normalize_state(shape, state)


def scan_complete(state: ScanState) -> bool:
    return state.phase == PHASE_DONE


def _covered_intervals(shape: ScanShape, state: ScanState) -> tuple[tuple[int, int], ...]:
    """recent 兜底 phase 必须跳过的已扫区间（降序、互不相邻）。

    进入 recent 时 ①（若跑过）只覆盖 [floor, snapshot]，②的命中段已全部
    扫完（advance 只在 leaf phase 无剩余范围时才转 recent），所以这两类
    区间在每一页都可确定性重建，无需写进 cursor。
    """

    intervals = list(shape.leaf_hit_ranges)
    if state.uncompressed_floor > 0:
        intervals.append((state.uncompressed_floor, shape.snapshot_through_seq))
    intervals.sort(key=lambda item: item[1], reverse=True)
    merged: list[tuple[int, int]] = []
    for start, end in intervals:
        if merged and start <= merged[-1][1] + 1 and merged[-1][0] <= end + 1:
            merged[-1] = (min(start, merged[-1][0]), max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return tuple(merged)


def _highest_uncovered_run(
    shape: ScanShape, state: ScanState, ceiling: int
) -> tuple[int, int] | None:
    """最高的一段未覆盖连续区间 [lo, hi]（hi <= ceiling），没有则 None。"""

    top = min(int(ceiling), shape.snapshot_through_seq)
    for start, end in _covered_intervals(shape, state):
        if top < start:
            # 整段覆盖区间都在 top 之上，不影响。
            continue
        if top > end:
            # top 落在这段覆盖区间之上的空隙里：区间下界是该覆盖段 end+1。
            return (end + 1, top)
        # top 落在覆盖区间内：跳到覆盖区间下方继续找。
        top = start - 1
        if top < 1:
            return None
    return (1, top) if top >= 1 else None


def _normalize_state(shape: ScanShape, state: ScanState) -> ScanState:
    """Fast-forward through empty phases so ``phase`` always has work or DONE."""

    current = state
    while True:
        if current.phase == PHASE_UNCOMPRESSED:
            lo = shape.summary_watermark_seq + 1
            hi = min(current.resume_seq, shape.snapshot_through_seq)
            if shape.has_query and hi >= lo:
                return current
            current = replace(
                current,
                phase=PHASE_LEAF_HITS,
                resume_seq=shape.snapshot_through_seq,
            )
        elif current.phase == PHASE_LEAF_HITS:
            if shape.has_query and any(
                start <= current.resume_seq for start, _end in shape.leaf_hit_ranges
            ):
                return current
            current = replace(
                current,
                phase=PHASE_RECENT,
                resume_seq=shape.snapshot_through_seq,
            )
        elif current.phase == PHASE_RECENT:
            if _highest_uncovered_run(shape, current, current.resume_seq) is not None:
                return current
            current = replace(current, phase=PHASE_DONE, resume_seq=0)
        else:
            return current


def next_batch(shape: ScanShape, state: ScanState, *, batch_limit: int) -> ScanBatch | None:
    """下一个要扫的降序候选窗口；扫完返回 None（=complete）。

    ``batch_limit`` 是本批候选行数上限（预算余量由调用方换算），必须为正。
    """

    limit = int(batch_limit)
    if limit <= 0:
        raise ValueError("batch_limit must be positive")
    current = _normalize_state(shape, state)
    if current.phase == PHASE_DONE:
        return None
    if current.phase == PHASE_UNCOMPRESSED:
        lo = shape.summary_watermark_seq + 1
        hi = min(current.resume_seq, shape.snapshot_through_seq)
        return ScanBatch(PHASE_UNCOMPRESSED, lo, hi, limit)
    if current.phase == PHASE_LEAF_HITS:
        # 命中段按 end_seq 降序（recent-first）逐段扫。
        for start, end in shape.leaf_hit_ranges:
            if start <= current.resume_seq:
                return ScanBatch(
                    PHASE_LEAF_HITS, start, min(end, current.resume_seq), limit
                )
        raise AssertionError("normalized leaf_hits state must have a range")
    run = _highest_uncovered_run(shape, current, current.resume_seq)
    if run is None:  # pragma: no cover — _normalize_state 已排除
        raise AssertionError("normalized recent state must have a run")
    return ScanBatch(PHASE_RECENT, run[0], run[1], limit)


def advance_scan_state(
    shape: ScanShape,
    state: ScanState,
    batch: ScanBatch,
    *,
    last_checked_seq: int | None = None,
    exhausted: bool = False,
) -> ScanState:
    """Consume one scanned batch and return the resumable successor state.

    cursor 正确性硬规则（spec §4）：``last_checked_seq`` 是本批**最后一条实际
    检查过的候选 seq**（降序扫描的最低位置）。批内密集命中提前凑够 limit 时，
    调用方必须传实际停下的位置——resume 恰好落在它下面一格，剩余候选由下一页
    重新拿到，绝不跳到批末尾。``exhausted=True`` 表示整个窗口都检查完了
    （DB 返回行数 < limit），等价于 last_checked = batch.min_seq。
    """

    current = _normalize_state(shape, state)
    if current.phase != batch.phase:
        raise ValueError("batch does not belong to the current scan phase")
    if exhausted:
        checked_floor = int(batch.min_seq)
    else:
        if last_checked_seq is None:
            raise ValueError("last_checked_seq required unless exhausted")
        checked_floor = int(last_checked_seq)
        if not (int(batch.min_seq) <= checked_floor <= int(batch.max_seq)):
            raise ValueError("last_checked_seq outside the batch window")
    if current.phase == PHASE_UNCOMPRESSED:
        # ①【至多一批】：无论有没有扫完未压缩区间，都立即让位给摘要命中段
        # （spec §4——防 compaction backlog 吃光预算）。没扫到的
        # (watermark, floor) 剩余部分由 recent 兜底 phase 补扫。
        successor = ScanState(
            PHASE_LEAF_HITS,
            shape.snapshot_through_seq,
            uncompressed_floor=checked_floor,
        )
    else:
        successor = replace(current, resume_seq=max(0, checked_floor - 1))
    return _normalize_state(shape, successor)


# ---------------------------------------------------------------------------
# Cursor codec（HMAC-SHA256，key 由调用方注入）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HistoryCursor:
    """Authenticated resumable snapshot of one in-flight search."""

    user_id: str
    snapshot_through_seq: int
    summary_watermark_seq: int
    runtime_generation: int
    query: str  # 归一化后的 query；"" = 无 query（纯时间范围）
    start_ts: float | None
    end_ts: float | None
    phase: str
    resume_seq: int
    uncompressed_floor: int
    expires_at: float
    version: int = CURSOR_VERSION

    def __post_init__(self) -> None:
        if not str(self.user_id):
            raise ValueError("cursor user_id required")
        if self.phase not in _PHASES:
            raise ValueError("unknown cursor phase")
        for field in (
            "snapshot_through_seq",
            "summary_watermark_seq",
            "runtime_generation",
            "resume_seq",
            "uncompressed_floor",
        ):
            if int(getattr(self, field)) < 0:
                raise ValueError(f"cursor {field} must be >= 0")
        if len(str(self.query)) > QUERY_MAX_CHARS:
            raise ValueError("cursor query too long")
        if not self.query and self.start_ts is None and self.end_ts is None:
            raise ValueError("cursor needs a query or a time range")

    def scan_state(self) -> ScanState:
        return ScanState(self.phase, self.resume_seq, self.uncompressed_floor)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    padded = text + "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _require_key(key: bytes) -> bytes:
    if not isinstance(key, (bytes, bytearray)) or len(key) < 16:
        raise ValueError("cursor HMAC key must be >= 16 bytes")
    return bytes(key)


def encode_cursor(cursor: HistoryCursor, *, key: bytes) -> str:
    """Serialize + sign one cursor; result is the opaque ≤1024-char token.

    Payload 走 compact JSON；比 zlib 短就用 zlib（CJK 长 query 时接近上限，
    见 spec §3.1 的 1024 限制）。签名覆盖 payload 段的确切字节。
    """

    signing_key = _require_key(key)
    payload = {
        "v": int(cursor.version),
        "exp": float(cursor.expires_at),
        "u": str(cursor.user_id),
        "ss": int(cursor.snapshot_through_seq),
        "sw": int(cursor.summary_watermark_seq),
        "rg": int(cursor.runtime_generation),
        "q": str(cursor.query),
        "t0": cursor.start_ts,
        "t1": cursor.end_ts,
        "ph": str(cursor.phase),
        "rs": int(cursor.resume_seq),
        "uf": int(cursor.uncompressed_floor),
    }
    raw = json.dumps(
        payload, separators=(",", ":"), ensure_ascii=False, sort_keys=True
    ).encode("utf-8")
    compressed = zlib.compress(raw, 9)
    body = b"z" + compressed if len(compressed) < len(raw) else b"r" + raw
    segment = _b64url(body)
    signature = _b64url(
        hmac.new(signing_key, segment.encode("ascii"), hashlib.sha256).digest()
    )
    token = f"{segment}.{signature}"
    if len(token) > CURSOR_MAX_CHARS:
        # QUERY_MAX_CHARS=128 后正常路径到不了这里；保留兜底：宁可稳定报错
        # 也不能发出一个自己都收不回的 cursor。
        raise HistorySearchInputError("cursor_overflow")
    return token


def decode_cursor(token: str, *, key: bytes, now: float) -> HistoryCursor:
    """Authenticate + parse one cursor token.

    任何格式/签名/版本/过期问题 → ``CursorInvalid``。字段一致性（用户、
    generation、query/时间范围）由下面两个 verify 函数分别负责。
    """

    signing_key = _require_key(key)
    text = str(token or "")
    if not text or len(text) > CURSOR_MAX_CHARS:
        raise CursorInvalid("bad_length")
    segment, _, signature = text.partition(".")
    if not segment or not signature:
        raise CursorInvalid("bad_format")
    expected = _b64url(
        hmac.new(signing_key, segment.encode("ascii"), hashlib.sha256).digest()
    )
    if not hmac.compare_digest(expected, signature):
        raise CursorInvalid("bad_signature")
    try:
        body = _b64url_decode(segment)
        if body[:1] == b"z":
            # 已验签才解压；即便如此也设膨胀上限，杜绝构造型 zlib 炸弹。
            decompressor = zlib.decompressobj()
            raw = decompressor.decompress(body[1:], 64 * 1024)
            if decompressor.unconsumed_tail:
                raise CursorInvalid("payload_too_large")
        elif body[:1] == b"r":
            raw = body[1:]
        else:
            raise CursorInvalid("bad_payload_tag")
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise CursorInvalid("bad_payload")
        if int(payload.get("v", 0)) != CURSOR_VERSION:
            raise CursorInvalid("unsupported_version")
        cursor = HistoryCursor(
            user_id=str(payload["u"]),
            snapshot_through_seq=int(payload["ss"]),
            summary_watermark_seq=int(payload["sw"]),
            runtime_generation=int(payload["rg"]),
            query=str(payload.get("q", "")),
            start_ts=None if payload.get("t0") is None else float(payload["t0"]),
            end_ts=None if payload.get("t1") is None else float(payload["t1"]),
            phase=str(payload["ph"]),
            resume_seq=int(payload["rs"]),
            uncompressed_floor=int(payload["uf"]),
            expires_at=float(payload["exp"]),
        )
    except CursorInvalid:
        raise
    except (KeyError, TypeError, ValueError, binascii.Error, zlib.error, UnicodeDecodeError):
        raise CursorInvalid("bad_payload") from None
    if not cursor.expires_at > float(now):
        raise CursorInvalid("expired")
    return cursor


def verify_cursor_binding(
    cursor: HistoryCursor, *, user_id: str, runtime_generation: int
) -> None:
    """认证上下文一致性：用户与 runtime generation 必须与签发时相同。

    ``user_id`` 来自认证上下文（绝不从 cursor 取信）；不一致按
    ``cursor_invalid`` 拒绝，不区分"别人的 cursor"与垃圾。generation 变了
    说明中间发生过 clear——快照里的 seq 世界已不存在，同样按 invalid 拒。
    """

    if not hmac.compare_digest(str(cursor.user_id), str(user_id)):
        raise CursorInvalid("user_binding")
    if int(cursor.runtime_generation) != int(runtime_generation):
        raise CursorInvalid("generation_changed")


def verify_cursor_request(
    cursor: HistoryCursor,
    *,
    query: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> None:
    """带 cursor 的调用只该传 cursor（spec §3.1）；显式重传的参数必须与
    payload 记录严格一致，否则 ``cursor_mismatch``。None = 省略 = 通过。"""

    if query is not None and normalize_query(query) != cursor.query:
        raise CursorMismatch("query")
    if start is not None and parse_rfc3339_utc(start) != cursor.start_ts:
        raise CursorMismatch("start")
    if end is not None and parse_rfc3339_utc(end) != cursor.end_ts:
        raise CursorMismatch("end")
