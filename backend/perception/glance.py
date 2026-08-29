"""io 侧兼容壳 —— 已搬进 ``perceptkit.glance``。"""
from __future__ import annotations

from perceptkit.algorithms.glance import (  # noqa: F401
    V1_PRESENCE_HINT_FIELDS,
    build_perception_glance,
    perception_glance_fingerprint,
    project_perception_wake_events,
)
