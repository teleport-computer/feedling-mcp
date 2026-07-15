"""Redact the small user-visible V2 turn-status vocabulary.

Status rows carry only a fixed label and optional coarse count; conversation
text, tool arguments, and capability results never cross this boundary.
"""
from __future__ import annotations

from typing import Any

# Fixed labels for the only status kinds the unified loop emits.
_KIND_LABEL: dict[str, str] = {
    "processing": "处理中",
    "writing_reply": "正在回复",
    "done": "完成",
    "error": "出问题了",
}


def redact_status(kind: str, *, count: int | None = None) -> dict[str, Any]:
    """Return a public payload containing no model or capability data."""
    label = _KIND_LABEL.get(kind, "处理中")
    detail: dict[str, Any] = {}
    if count is not None and count > 0:
        detail["count"] = int(count)
        label = f"{label}（{int(count)}）"
    return {"kind": kind, "label": label, "detail": detail}
