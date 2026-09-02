"""io 侧兼容壳 —— 已搬进 ``perceptkit.fields``。"""
from __future__ import annotations

from perceptkit.fields import (  # noqa: F401
    AGENT_PERCEPTION_SIGNALS,
    AGENT_SIGNAL_FIELDS,
    FAST_AGENT_PERCEPTION_SIGNALS,
    PULL_ONLY_AGENT_PERCEPTION_SIGNALS,
    SLOW_AGENT_PERCEPTION_SIGNALS,
    project_signal,
)
