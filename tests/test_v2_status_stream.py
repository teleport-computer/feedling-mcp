"""V2 status 脱敏 + 合并 + 限频（spec §9 两条红线）。Pure-unit。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from model_api_runtime.v2 import status_stream as ss  # noqa: E402


def test_status_kind_mapping():
    assert ss.status_kind_for_action("memory_fetch") == "reading_memory"
    assert ss.status_kind_for_action("perception_snapshot") == "reading_perception"
    assert ss.status_kind_for_action("memory_write") == "capturing_memory"
    assert ss.status_kind_for_action("final_response") == "writing_reply"
    assert ss.status_kind_for_action("totally_unknown") == "processing"


def test_redact_carries_only_label_and_coarse_count():
    ev = ss.redact_status("reading_memory", count=3)
    assert ev["kind"] == "reading_memory"
    assert ev["detail"] == {"count": 3}
    # NO plaintext / bodies / ids — only a label and the coarse count.
    assert set(ev["detail"].keys()) <= {"count"}
    assert "3" in ev["label"]


def test_merge_parallel_reads_collapses_burst_to_one():
    kinds = ["reading_memory", "reading_perception", "reading_screen"]
    merged = ss.merge_parallel_reads(kinds)
    assert len(merged) == 1
    assert merged[0]["kind"] == "reading_memory"
    assert merged[0]["detail"]["kinds"] == kinds


def test_merge_passes_non_mergeable_through_in_order():
    kinds = ["reading_memory", "capturing_memory", "reading_perception"]
    merged = ss.merge_parallel_reads(kinds)
    assert [e["kind"] for e in merged] == [
        "reading_memory", "capturing_memory", "reading_perception"]


def test_redact_status_error_kind_has_neutral_label_not_fallback():
    """Task 3: terminal-failure error surface. "error" must be a real _KIND_LABEL
    entry (a neutral, runtime-authored system-chip label), not the "处理中"
    unknown-kind fallback that redact_status uses for anything not in the map."""
    ev = ss.redact_status("error")
    assert ev["kind"] == "error"
    assert ev["label"] != "处理中"
    assert ev["label"] == ss._KIND_LABEL["error"]


def test_rate_limiter_drops_bursts_of_same_kind():
    clock = {"t": 0.0}
    rl = ss.RateLimiter(min_interval=1.0, now=lambda: clock["t"])
    assert rl.allow("reading_memory") is True
    assert rl.allow("reading_memory") is False   # too soon
    clock["t"] = 1.5
    assert rl.allow("reading_memory") is True     # window elapsed
    # a different kind is tracked independently
    assert rl.allow("writing_reply") is True
