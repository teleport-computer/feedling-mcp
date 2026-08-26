"""io 侧兼容壳 —— 已搬进 ``sensegate.fields``。"""
from __future__ import annotations

from sensegate.fields import (  # noqa: F401
    ALLOW_VALUES,
    DENIED_VALUES,
    OFF_VALUES,
    SIGNAL_PERMISSION_KEYS,
    boolish_doc_reason,
    permission_state_reason,
    permission_states_reason,
)
