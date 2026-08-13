"""Pure unit tests for the V2 history-search kernel (no DB, no IO).

覆盖 spec §8.2/§8.3 的纯逻辑部分：输入归一化、planner 分流与恢复位置、
cursor 签名/过期/篡改/跨用户/条件错配、密集命中提前停不丢候选。
DB 侧（叶子快照、候选元数据过滤）见 test_v2_history_search_store.py。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import history_search as hs


KEY = b"unit-test-hmac-key-0123456789abcdef"


def _cursor(**overrides) -> hs.HistoryCursor:
    base = dict(
        user_id="u_hist",
        snapshot_through_seq=500,
        summary_watermark_seq=400,
        runtime_generation=3,
        query="老地方 餐厅",
        start_ts=None,
        end_ts=None,
        phase=hs.PHASE_UNCOMPRESSED,
        resume_seq=500,
        uncompressed_floor=0,
        expires_at=2_000.0,
    )
    base.update(overrides)
    return hs.HistoryCursor(**base)


# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------


def test_normalize_query_nfkc_casefold_whitespace():
    # 全角 → NFKC 半角、大写 → casefold、多空白（含全角空格/换行）→ 单空格
    assert hs.normalize_query("　Ｑｕｅｒｙ  ＡＢＣ\n\tx　") == "query abc x"
    # 匹配侧走同一套规则
    assert hs.normalize_for_match("ＱｕｅｒｙＡＢＣ") == "queryabc"
    # 德语 ß casefold 展开
    assert hs.normalize_query("Straße") == "strasse"
    # 中文原样保留
    assert hs.normalize_query("上个月的餐厅") == "上个月的餐厅"


def test_normalize_query_rejects_empty_and_too_long():
    with pytest.raises(hs.HistorySearchInputError) as excinfo:
        hs.normalize_query("  　\t ")
    assert excinfo.value.code == "query_empty"
    with pytest.raises(hs.HistorySearchInputError) as excinfo:
        hs.normalize_query("x" * (hs.QUERY_MAX_CHARS + 1))
    assert excinfo.value.code == "query_too_long"
    # NFKC 展开（ﬃ → ffi）后超限也拒
    with pytest.raises(hs.HistorySearchInputError) as excinfo:
        hs.normalize_query("ﬃ" * 100)  # 100 字符 → 归一化后 300
    assert excinfo.value.code == "query_too_long"


def test_parse_rfc3339_offset_converts_to_utc():
    assert hs.parse_rfc3339_utc("1970-01-01T00:00:00Z") == 0.0
    # +08:00 offset 换算到 UTC
    assert hs.parse_rfc3339_utc("1970-01-01T08:00:00+08:00") == 0.0
    assert hs.parse_rfc3339_utc("2026-08-07T00:00:00Z") == hs.parse_rfc3339_utc(
        "2026-08-07T08:00:00+08:00"
    )


def test_parse_rfc3339_rejects_naive_and_garbage():
    for bad in ("2026-08-07T00:00:00", "not-a-time", "", "2026-13-40T99:00:00Z"):
        with pytest.raises(hs.HistorySearchInputError) as excinfo:
            hs.parse_rfc3339_utc(bad)
        assert excinfo.value.code == "invalid_time"


def test_normalize_time_range_start_must_precede_end():
    start, end = hs.normalize_time_range("2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z")
    assert start is not None and end is not None and start < end
    assert hs.normalize_time_range(None, "2026-02-01T00:00:00Z")[0] is None
    with pytest.raises(hs.HistorySearchInputError) as excinfo:
        hs.normalize_time_range("2026-02-01T00:00:00Z", "2026-02-01T00:00:00Z")
    assert excinfo.value.code == "invalid_time_range"


# ---------------------------------------------------------------------------
# planner：分流
# ---------------------------------------------------------------------------


def _drain(shape, state, *, batch_limit=100, max_batches=50):
    """扫完全部批次（每批都 exhausted），返回 [(phase, min, max)]。"""
    out = []
    for _ in range(max_batches):
        batch = hs.next_batch(shape, state, batch_limit=batch_limit)
        if batch is None:
            return out, state
        out.append((batch.phase, batch.min_seq, batch.max_seq))
        state = hs.advance_scan_state(shape, state, batch, exhausted=True)
    raise AssertionError("scan did not terminate")


def test_time_only_request_scans_recent_first_raw_single_phase():
    shape = hs.ScanShape(
        snapshot_through_seq=500, summary_watermark_seq=400, has_query=False
    )
    state = hs.initial_scan_state(shape)
    batches, final = _drain(shape, state)
    # 无 query：跳过摘要提示，整个快照 recent-first 一个 phase 扫完
    assert batches == [(hs.PHASE_RECENT, 1, 500)]
    assert hs.scan_complete(final)


def test_query_request_uncompressed_then_leaf_hits_then_recent():
    shape = hs.ScanShape(
        snapshot_through_seq=500,
        summary_watermark_seq=400,
        has_query=True,
        leaf_hit_ranges=((100, 150), (300, 350)),
    )
    state = hs.initial_scan_state(shape)
    batches, final = _drain(shape, state)
    assert batches == [
        # ① 未压缩区间 (watermark, snapshot]
        (hs.PHASE_UNCOMPRESSED, 401, 500),
        # ② 命中段 recent-first（end_seq 降序）
        (hs.PHASE_LEAF_HITS, 300, 350),
        (hs.PHASE_LEAF_HITS, 100, 150),
        # ③ 其余区间 recent-first，跳过已扫范围
        (hs.PHASE_RECENT, 351, 400),
        (hs.PHASE_RECENT, 151, 299),
        (hs.PHASE_RECENT, 1, 99),
    ]
    assert hs.scan_complete(final)


def test_query_without_uncompressed_region_starts_at_leaf_hits():
    shape = hs.ScanShape(
        snapshot_through_seq=400,
        summary_watermark_seq=400,
        has_query=True,
        leaf_hit_ranges=((380, 400),),
    )
    state = hs.initial_scan_state(shape)
    assert state.phase == hs.PHASE_LEAF_HITS
    batches, _ = _drain(shape, state)
    assert batches[0] == (hs.PHASE_LEAF_HITS, 380, 400)


def test_query_without_leaf_hits_falls_through_to_recent():
    shape = hs.ScanShape(
        snapshot_through_seq=500, summary_watermark_seq=400, has_query=True
    )
    state = hs.initial_scan_state(shape)
    batches, final = _drain(shape, state)
    assert batches == [
        (hs.PHASE_UNCOMPRESSED, 401, 500),
        (hs.PHASE_RECENT, 1, 400),
    ]
    assert hs.scan_complete(final)


def test_uncompressed_phase_takes_at_most_one_batch():
    """① 只许一批：没扫完的部分让位给命中段，由 recent 兜底补扫（spec §4）。"""
    shape = hs.ScanShape(
        snapshot_through_seq=10_000,
        summary_watermark_seq=100,
        has_query=True,
        leaf_hit_ranges=((50, 80),),
    )
    state = hs.initial_scan_state(shape)
    batch = hs.next_batch(shape, state, batch_limit=128)
    assert (batch.phase, batch.min_seq, batch.max_seq) == (
        hs.PHASE_UNCOMPRESSED, 101, 10_000,
    )
    # 一批只扫到 seq 9000 就没预算了（compaction backlog 很深）
    state = hs.advance_scan_state(shape, state, batch, last_checked_seq=9000)
    batch = hs.next_batch(shape, state, batch_limit=128)
    # 立即让位给命中段，绝不继续吃未压缩区间
    assert (batch.phase, batch.min_seq, batch.max_seq) == (hs.PHASE_LEAF_HITS, 50, 80)
    state = hs.advance_scan_state(shape, state, batch, exhausted=True)
    # recent 兜底：最高未扫连续段 = [81, 8999]——① 没扫到的 (100, 9000) 加上
    # 命中段之上的压缩区尾巴 81..100，连成一段
    batch = hs.next_batch(shape, state, batch_limit=128)
    assert (batch.phase, batch.min_seq, batch.max_seq) == (hs.PHASE_RECENT, 81, 8999)
    state = hs.advance_scan_state(shape, state, batch, exhausted=True)
    batch = hs.next_batch(shape, state, batch_limit=128)
    assert (batch.phase, batch.min_seq, batch.max_seq) == (hs.PHASE_RECENT, 1, 49)


def test_leaf_ranges_clipped_to_watermark_and_merged():
    shape = hs.ScanShape(
        snapshot_through_seq=500,
        summary_watermark_seq=400,
        # 与 watermark 之上重叠的部分被裁掉；相邻段合并
        leaf_hit_ranges=((390, 450), (351, 389)),
        has_query=True,
    )
    assert shape.leaf_hit_ranges == ((351, 400),)


# ---------------------------------------------------------------------------
# planner：恢复位置硬规则（密集命中不丢）
# ---------------------------------------------------------------------------


def test_dense_hits_resume_at_last_checked_candidate():
    """一批内命中 20 条只返回 3 条时，其余 17 条必须能被下一页拿到。"""
    shape = hs.ScanShape(
        snapshot_through_seq=1000, summary_watermark_seq=0, has_query=True
    )
    state = hs.initial_scan_state(shape)
    batch = hs.next_batch(shape, state, batch_limit=128)
    assert (batch.min_seq, batch.max_seq) == (1, 1000)
    # 降序扫描在 seq=997 处凑够 limit 提前停（998/999/1000 已检查并命中）
    state = hs.advance_scan_state(shape, state, batch, last_checked_seq=997)
    batch = hs.next_batch(shape, state, batch_limit=128)
    # 下一页从 996 继续——绝不跳到本批末尾（seq 1..996 的候选一个不丢）
    assert (batch.min_seq, batch.max_seq) == (1, 996)


def test_resume_inside_leaf_hit_range():
    shape = hs.ScanShape(
        snapshot_through_seq=400,
        summary_watermark_seq=400,
        has_query=True,
        leaf_hit_ranges=((100, 200), (300, 380)),
    )
    state = hs.initial_scan_state(shape)
    batch = hs.next_batch(shape, state, batch_limit=64)
    assert (batch.min_seq, batch.max_seq) == (300, 380)
    # 段内提前停在 320
    state = hs.advance_scan_state(shape, state, batch, last_checked_seq=320)
    batch = hs.next_batch(shape, state, batch_limit=64)
    assert (batch.phase, batch.min_seq, batch.max_seq) == (hs.PHASE_LEAF_HITS, 300, 319)
    # 扫完该段 → 下一个命中段
    state = hs.advance_scan_state(shape, state, batch, exhausted=True)
    batch = hs.next_batch(shape, state, batch_limit=64)
    assert (batch.phase, batch.min_seq, batch.max_seq) == (hs.PHASE_LEAF_HITS, 100, 200)


def test_advance_validates_last_checked_inside_batch():
    shape = hs.ScanShape(
        snapshot_through_seq=100, summary_watermark_seq=0, has_query=True
    )
    state = hs.initial_scan_state(shape)
    batch = hs.next_batch(shape, state, batch_limit=10)
    with pytest.raises(ValueError):
        hs.advance_scan_state(shape, state, batch, last_checked_seq=101)
    with pytest.raises(ValueError):
        hs.advance_scan_state(shape, state, batch)  # 既没 exhausted 也没位置


def test_scan_state_roundtrips_through_cursor_payload():
    """cursor 只存 (phase, resume, floor)，翻页后 planner 能原位续扫。"""
    shape = hs.ScanShape(
        snapshot_through_seq=500,
        summary_watermark_seq=400,
        has_query=True,
        leaf_hit_ranges=((100, 150),),
    )
    state = hs.initial_scan_state(shape)
    batch = hs.next_batch(shape, state, batch_limit=32)
    state = hs.advance_scan_state(shape, state, batch, last_checked_seq=430)
    cursor = _cursor(
        phase=state.phase,
        resume_seq=state.resume_seq,
        uncompressed_floor=state.uncompressed_floor,
    )
    token = hs.encode_cursor(cursor, key=KEY)
    restored = hs.decode_cursor(token, key=KEY, now=1_000.0).scan_state()
    assert restored == state
    # 续扫的第一批就是命中段（floor=430 已记住 ① 的进度）
    batch = hs.next_batch(shape, restored, batch_limit=32)
    assert (batch.phase, batch.min_seq, batch.max_seq) == (hs.PHASE_LEAF_HITS, 100, 150)
    # recent 兜底会补扫 ① 没扫完的 (400, 430) 连同命中段之上的 151..400，
    # 连成一段 [151, 429]
    restored = hs.advance_scan_state(shape, restored, batch, exhausted=True)
    batch = hs.next_batch(shape, restored, batch_limit=32)
    assert (batch.phase, batch.min_seq, batch.max_seq) == (hs.PHASE_RECENT, 151, 429)


# ---------------------------------------------------------------------------
# cursor codec
# ---------------------------------------------------------------------------


def test_cursor_roundtrip_and_length_budget():
    cursor = _cursor()
    token = hs.encode_cursor(cursor, key=KEY)
    assert len(token) <= hs.CURSOR_MAX_CHARS
    decoded = hs.decode_cursor(token, key=KEY, now=1_000.0)
    assert decoded == cursor
    hs.verify_cursor_binding(decoded, user_id="u_hist", runtime_generation=3)
    hs.verify_cursor_request(decoded)  # 只传 cursor：合法


def test_cursor_tamper_rejected():
    token = hs.encode_cursor(_cursor(), key=KEY)
    segment, _, signature = token.partition(".")
    # 换 payload、保签名
    other = hs.encode_cursor(_cursor(resume_seq=1), key=KEY)
    forged = other.partition(".")[0] + "." + signature
    for bad in (
        forged,
        segment,  # 无签名段
        token[:-2],  # 截断签名
        "AAAA." + signature,
        token + "x",
        "",
        "x" * (hs.CURSOR_MAX_CHARS + 1),
    ):
        with pytest.raises(hs.CursorInvalid):
            hs.decode_cursor(bad, key=KEY, now=1_000.0)


def test_cursor_wrong_key_rejected():
    token = hs.encode_cursor(_cursor(), key=KEY)
    with pytest.raises(hs.CursorInvalid):
        hs.decode_cursor(token, key=b"another-key-0123456789abcdef!!", now=1_000.0)


def test_cursor_expiry_rejected():
    token = hs.encode_cursor(_cursor(expires_at=2_000.0), key=KEY)
    assert hs.decode_cursor(token, key=KEY, now=1_999.0)
    with pytest.raises(hs.CursorInvalid) as excinfo:
        hs.decode_cursor(token, key=KEY, now=2_000.0)
    assert excinfo.value.detail == "expired"


def test_cursor_cross_user_and_stale_generation_are_invalid_not_mismatch():
    decoded = hs.decode_cursor(hs.encode_cursor(_cursor(), key=KEY), key=KEY, now=1.0)
    # 跨用户：按 cursor_invalid 拒（不确认"这是别人的合法 cursor"）
    with pytest.raises(hs.CursorInvalid):
        hs.verify_cursor_binding(decoded, user_id="u_other", runtime_generation=3)
    # clear 之后 generation 变了：快照世界已不存在，同样 invalid
    with pytest.raises(hs.CursorInvalid):
        hs.verify_cursor_binding(decoded, user_id="u_hist", runtime_generation=4)


def test_cursor_condition_mismatch_vs_consistent_resend():
    decoded = hs.decode_cursor(
        hs.encode_cursor(
            _cursor(start_ts=hs.parse_rfc3339_utc("2026-01-01T00:00:00Z")), key=KEY
        ),
        key=KEY,
        now=1.0,
    )
    # 与 payload 严格一致的重传：放行（含归一化等价形式）
    hs.verify_cursor_request(
        decoded, query="老地方　餐厅", start="2026-01-01T08:00:00+08:00"
    )
    with pytest.raises(hs.CursorMismatch):
        hs.verify_cursor_request(decoded, query="别的词")
    with pytest.raises(hs.CursorMismatch):
        hs.verify_cursor_request(decoded, start="2026-01-02T00:00:00Z")
    with pytest.raises(hs.CursorMismatch):
        hs.verify_cursor_request(decoded, end="2026-03-01T00:00:00Z")


def test_cursor_paging_rejects_any_explicit_limit():
    """续页只传 cursor（spec §3.1）：limit 不进 cursor payload，重传任何值都
    会改页大小 → 一律 cursor_mismatch，词面与 query/start/end 冲突一致。"""
    decoded = hs.decode_cursor(hs.encode_cursor(_cursor(), key=KEY), key=KEY, now=1.0)
    hs.verify_cursor_request(decoded)            # 省略 limit：合法
    hs.verify_cursor_request(decoded, limit=None)
    for bad in (1, 3, 5):
        with pytest.raises(hs.CursorMismatch):
            hs.verify_cursor_request(decoded, limit=bad)


def test_cursor_long_cjk_query_roundtrip_within_limit():
    # 128 个不重复汉字（可压缩性差的近似）仍在 1024 内往返
    query = "".join(chr(0x4E00 + i) for i in range(128))
    token = hs.encode_cursor(_cursor(query=query), key=KEY)
    assert len(token) <= hs.CURSOR_MAX_CHARS
    assert hs.decode_cursor(token, key=KEY, now=1.0).query == query


def test_cursor_requires_query_or_time_range():
    with pytest.raises(ValueError):
        _cursor(query="", start_ts=None, end_ts=None)


def test_cursor_weak_key_rejected():
    with pytest.raises(ValueError):
        hs.encode_cursor(_cursor(), key=b"short")
