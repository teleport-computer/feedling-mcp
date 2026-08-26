"""io 侧兼容壳 —— 已搬进 ``sensegate.history``。"""
from __future__ import annotations

from sensegate.history import (  # noqa: F401
    CUMULATIVE,
    DURATION_BY_STATE,
    EVENT_LIST,
    MAIN_OF_DAY,
    NUMERIC_DIST,
    PLACE_DWELL,
    SHAPE,
    SUBJECTIVE,
    TALLY,
    comparable_signals,
    cross_domain_recent,
    is_historized,
    notable_changes,
    read_trend,
    record_daily,
)
