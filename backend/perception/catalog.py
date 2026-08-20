"""io 侧兼容壳 —— 能力表已搬进 ``perception_kernel.catalog``。

保留这个模块是为了不动 service / routes / ingress 里几十处
``from perception.catalog import ...``。新代码请直接 import 内核。
"""
from __future__ import annotations

from perception_kernel.catalog import (  # noqa: F401
    CAPABILITIES,
    COMPOSITE_KEYS,
    Capability,
    IGNORED_KEYS,
    KEY_ALIASES,
    KIND_CAPABILITY,
    PHOTO_CLUSTER_SEC,
    PHOTO_METADATA_FIELDS,
    RECENT_APPS_LIMIT,
    RECENT_APPS_TOOL_LIMIT,
    RECENT_APPS_TOOL_MAX,
    SCENE_HINTS,
    SENSITIVE_PHOTO_SCENES,
    SIGNALS,
    Signal,
    UNLOCK_BACK_THRESHOLD_SEC,
)
