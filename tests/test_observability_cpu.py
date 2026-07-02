from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from observability.cpu import CpuRateTracker  # noqa: E402

def test_first_sample_is_none():
    t = CpuRateTracker()
    assert t.update("backend", 1_000_000, now=100.0) is None

def test_second_sample_computes_pct():
    t = CpuRateTracker()
    t.update("backend", 1_000_000, now=100.0)
    # 1s 墙钟里多用了 0.5s CPU → 50%
    assert t.update("backend", 1_500_000, now=101.0) == 50.0

def test_multicore_over_100():
    t = CpuRateTracker()
    t.update("ar", 0, now=0.0)
    # 1s 里用了 2s CPU → 200%
    assert t.update("ar", 2_000_000, now=1.0) == 200.0

def test_counter_reset_returns_none():
    t = CpuRateTracker()
    t.update("e", 5_000_000, now=10.0)
    assert t.update("e", 1_000_000, now=11.0) is None  # 重启导致回退

def test_none_usage_returns_none():
    t = CpuRateTracker()
    assert t.update("x", None, now=1.0) is None
