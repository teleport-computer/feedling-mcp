"""io 侧兼容壳 —— 已搬进 ``perception_kernel.glance``。"""
from __future__ import annotations

from perception_kernel.glance import (  # noqa: F401
    V1_PRESENCE_HINT_FIELDS,
    build_perception_glance,
    perception_glance_fingerprint,
    project_perception_wake_events,
)
