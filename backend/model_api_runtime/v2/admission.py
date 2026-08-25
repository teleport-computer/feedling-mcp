"""§6 queue-wait telemetry 的纯函数。

无 DB、无 hosted import（守依赖方向）。DB 读由 jobs_store 提供、chat_send_core 注入。
live overload 的估算只用于 telemetry；它不在持久化前拒绝用户消息。
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
    """返回 est-wait = ceil(inflight / workers) × 服务时长。

    workers<=0 → 0（防除零；供给死交给独立的上游 liveness gate）。
    inflight<=0 → 0。mean_service_sec None → 用 default_service_sec。
    """
    if workers <= 0 or inflight <= 0:
        return 0.0
    service = mean_service_sec if mean_service_sec is not None else default_service_sec
    batches = math.ceil(inflight / workers)
    return float(batches) * float(service)


def should_admit(est_wait_sec: float, *, sla_sec: float) -> bool:
    """分类 est_wait 是否未超过 SLA（边界相等为 true）。

    Hosted Chat 仅把该分类写成 telemetry；它不代表 pre-persistence 拒绝。
    """
    return est_wait_sec <= sla_sec
