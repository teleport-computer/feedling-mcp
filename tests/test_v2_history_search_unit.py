"""Pure tests for the raw-Chat-only V2 history-search kernel."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
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
        runtime_generation=3,
        query="老地方 餐厅",
        start_ts=None,
        end_ts=None,
        resume_seq=500,
        expires_at=2_000.0,
    )
    base.update(overrides)
    return hs.HistoryCursor(**base)


def test_normalize_query_nfkc_casefold_whitespace():
    assert hs.normalize_query("　Ｑｕｅｒｙ  ＡＢＣ\n\tx　") == "query abc x"
    assert hs.normalize_for_match("ＱｕｅｒｙＡＢＣ") == "queryabc"
    assert hs.normalize_query("Straße") == "strasse"
    assert hs.normalize_query("上个月的餐厅") == "上个月的餐厅"


def test_normalize_query_rejects_empty_and_too_long():
    with pytest.raises(hs.HistorySearchInputError) as empty:
        hs.normalize_query("  　\t ")
    assert empty.value.code == "query_empty"
    with pytest.raises(hs.HistorySearchInputError) as long:
        hs.normalize_query("x" * (hs.QUERY_MAX_CHARS + 1))
    assert long.value.code == "query_too_long"
    with pytest.raises(hs.HistorySearchInputError) as expanded:
        hs.normalize_query("ﬃ" * 100)
    assert expanded.value.code == "query_too_long"


def test_parse_rfc3339_offset_converts_to_utc():
    assert hs.parse_rfc3339_utc("1970-01-01T00:00:00Z") == 0.0
    assert hs.parse_rfc3339_utc("1970-01-01T08:00:00+08:00") == 0.0


def test_parse_rfc3339_rejects_naive_and_garbage():
    for bad in ("2026-08-07T00:00:00", "not-a-time", ""):
        with pytest.raises(hs.HistorySearchInputError) as excinfo:
            hs.parse_rfc3339_utc(bad)
        assert excinfo.value.code == "invalid_time"


def test_normalize_time_range_start_must_precede_end():
    start, end = hs.normalize_time_range(
        "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z"
    )
    assert start is not None and end is not None and start < end
    with pytest.raises(hs.HistorySearchInputError) as excinfo:
        hs.normalize_time_range(
            "2026-02-01T00:00:00Z", "2026-02-01T00:00:00Z"
        )
    assert excinfo.value.code == "invalid_time_range"


def test_raw_scan_is_one_newest_to_oldest_phase():
    state = hs.initial_scan_state(snapshot_through_seq=500)
    first = hs.next_batch(state, batch_limit=128)
    assert first == hs.ScanBatch(min_seq=1, max_seq=500, limit=128)
    state = hs.advance_scan_state(state, first, last_checked_seq=373)
    second = hs.next_batch(state, batch_limit=128)
    assert second == hs.ScanBatch(min_seq=1, max_seq=372, limit=128)
    state = hs.advance_scan_state(state, second, exhausted=True)
    assert hs.scan_complete(state)
    assert hs.next_batch(state, batch_limit=128) is None


def test_dense_hits_resume_after_last_checked_candidate():
    state = hs.initial_scan_state(snapshot_through_seq=1_000)
    batch = hs.next_batch(state, batch_limit=128)
    state = hs.advance_scan_state(state, batch, last_checked_seq=997)
    assert hs.next_batch(state, batch_limit=128).max_seq == 996


def test_advance_validates_last_checked_inside_batch():
    state = hs.initial_scan_state(snapshot_through_seq=100)
    batch = hs.next_batch(state, batch_limit=10)
    with pytest.raises(ValueError):
        hs.advance_scan_state(state, batch, last_checked_seq=101)
    with pytest.raises(ValueError):
        hs.advance_scan_state(state, batch)


def test_cursor_roundtrip_and_length_budget():
    cursor = _cursor()
    token = hs.encode_cursor(cursor, key=KEY)
    assert len(token) <= hs.CURSOR_MAX_CHARS
    decoded = hs.decode_cursor(token, key=KEY, now=1_000.0)
    assert decoded == cursor
    hs.verify_cursor_binding(decoded, user_id="u_hist", runtime_generation=3)
    hs.verify_cursor_request(decoded)


def test_cursor_tamper_wrong_key_and_expiry_rejected():
    token = hs.encode_cursor(_cursor(), key=KEY)
    segment, _, signature = token.partition(".")
    other_segment = hs.encode_cursor(_cursor(resume_seq=1), key=KEY).partition(".")[0]
    for bad in (
        other_segment + "." + signature,
        segment,
        token[:-2],
        token + "x",
        "",
        "x" * (hs.CURSOR_MAX_CHARS + 1),
    ):
        with pytest.raises(hs.CursorInvalid):
            hs.decode_cursor(bad, key=KEY, now=1_000.0)
    with pytest.raises(hs.CursorInvalid):
        hs.decode_cursor(
            token,
            key=b"another-key-0123456789abcdef!!",
            now=1_000.0,
        )
    with pytest.raises(hs.CursorInvalid) as expired:
        hs.decode_cursor(token, key=KEY, now=2_000.0)
    assert expired.value.detail == "expired"


def test_summary_aware_cursor_version_is_rejected_as_cursor_invalid():
    old_payload = {
        "v": 1,
        "exp": 2_000.0,
        "u": "u_hist",
        "ss": 500,
        "sw": 400,
        "rg": 3,
        "q": "query",
        "t0": None,
        "t1": None,
        "ph": "recent",
        "rs": 500,
        "uf": 0,
    }
    raw = json.dumps(
        old_payload,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    body = base64.urlsafe_b64encode(b"r" + raw).rstrip(b"=").decode()
    signature = base64.urlsafe_b64encode(
        hmac.new(KEY, body.encode(), hashlib.sha256).digest()
    ).rstrip(b"=").decode()

    with pytest.raises(hs.CursorInvalid) as excinfo:
        hs.decode_cursor(f"{body}.{signature}", key=KEY, now=1_000.0)

    assert excinfo.value.detail == "unsupported_version"


def test_cursor_cross_user_generation_and_request_mismatch():
    decoded = hs.decode_cursor(
        hs.encode_cursor(
            _cursor(start_ts=hs.parse_rfc3339_utc("2026-01-01T00:00:00Z")),
            key=KEY,
        ),
        key=KEY,
        now=1.0,
    )
    with pytest.raises(hs.CursorInvalid):
        hs.verify_cursor_binding(decoded, user_id="u_other", runtime_generation=3)
    with pytest.raises(hs.CursorInvalid):
        hs.verify_cursor_binding(decoded, user_id="u_hist", runtime_generation=4)
    hs.verify_cursor_request(
        decoded,
        query="老地方　餐厅",
        start="2026-01-01T08:00:00+08:00",
    )
    with pytest.raises(hs.CursorMismatch):
        hs.verify_cursor_request(decoded, query="别的词")
    with pytest.raises(hs.CursorMismatch):
        hs.verify_cursor_request(decoded, limit=3)


def test_cursor_long_cjk_query_roundtrip_within_limit():
    query = "".join(chr(0x4E00 + index) for index in range(128))
    token = hs.encode_cursor(_cursor(query=query), key=KEY)
    assert len(token) <= hs.CURSOR_MAX_CHARS
    assert hs.decode_cursor(token, key=KEY, now=1.0).query == query


def test_cursor_requires_query_or_time_range_and_strong_key():
    with pytest.raises(ValueError):
        _cursor(query="", start_ts=None, end_ts=None)
    with pytest.raises(ValueError):
        hs.encode_cursor(_cursor(), key=b"short")
