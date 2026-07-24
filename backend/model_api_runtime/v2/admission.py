"""§6 admission ceiling —— 纯函数：估算 send 排队等待、判定是否放行。

无 DB、无 hosted import（守依赖方向）。DB 读由 jobs_store 提供、chat_send_core 注入。
当前 prod 用户量极小，此闸是安全阀、几乎不触发；刻意保持最小近似（不按 priority 加权）。
"""
from __future__ import annotations

import math
import os


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


SLA_SEC: float = _env_float("V2_ADMISSION_SLA_SEC", 60.0)
DEFAULT_SERVICE_SEC: float = _env_float("V2_ADMISSION_DEFAULT_SERVICE_SEC", 20.0)
SERVICE_SAMPLE_N: int = _env_int("V2_ADMISSION_SAMPLE_N", 50)


def estimate_wait_sec(
    *,
    inflight: int,
    workers: int,
    mean_service_sec: float | None,
    default_service_sec: float,
) -> float:
    """est-wait = ceil(inflight / workers) × 服务时长。

    workers<=0 → 0（防除零；供给死交给上游存活闸，不在这里拦）。
    inflight<=0 → 0。mean_service_sec None → 用 default_service_sec。
    """
    if workers <= 0 or inflight <= 0:
        return 0.0
    service = mean_service_sec if mean_service_sec is not None else default_service_sec
    batches = math.ceil(inflight / workers)
    return float(batches) * float(service)


def should_admit(est_wait_sec: float, *, sla_sec: float) -> bool:
    """est_wait ≤ sla → 放行（边界相等放行）。"""
    return est_wait_sec <= sla_sec
