from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from observability import fivexx  # noqa: E402

def test_counts_only_5xx_and_drains():
    fivexx.drain()  # 清零
    for c in (200, 500, 404, 503, 500):
        fivexx.record(c)
    assert fivexx.drain() == 3   # 两个 500 + 一个 503
    assert fivexx.drain() == 0   # drain 后清零
