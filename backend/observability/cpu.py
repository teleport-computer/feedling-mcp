from __future__ import annotations


class CpuRateTracker:
    """cgroup cpu.stat usage_usec 累计值 → CPU%。每个 service 独立保留上次读数。"""

    def __init__(self) -> None:
        self._last: dict[str, tuple[int, float]] = {}  # service -> (usage_usec, wall_sec)

    def update(self, service: str, usage_usec: int | None, now: float) -> float | None:
        if usage_usec is None:
            self._last.pop(service, None)
            return None
        prev = self._last.get(service)
        self._last[service] = (usage_usec, now)
        if prev is None:
            return None
        prev_usage, prev_now = prev
        dt = now - prev_now
        if dt <= 0 or usage_usec < prev_usage:  # 无进展 / 计数回退（服务重启）
            return None
        busy_sec = (usage_usec - prev_usage) / 1_000_000.0
        return round(busy_sec / dt * 100.0, 1)
