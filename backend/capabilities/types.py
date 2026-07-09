"""Uniform result shape for the capability facade (Hosted Runtime V2).

Domain `*_core` functions return heterogeneous shapes — `(body, status)`
tuples, `ScreenResult` dataclasses, or raise `AgentRouteError`. The V2 worker's
planner/executor need ONE shape; `CapabilityResult` is it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CapabilityResult:
    ok: bool
    data: dict = field(default_factory=dict)
    error: Optional[dict] = None          # {"code","message","retryable"}
    trace: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)

    def to_dict(self) -> dict:
        if self.ok:
            return {"ok": True, "data": self.data, "trace": self.trace,
                    "warnings": self.warnings}
        return {"ok": False, "error": self.error}


def ok(data: Optional[dict] = None, *, trace: Optional[dict] = None,
       warnings: Optional[list] = None) -> CapabilityResult:
    return CapabilityResult(ok=True, data=data or {}, trace=trace or {},
                            warnings=warnings or [])


def err(code: str, message: str, *, retryable: bool = False,
        trace: Optional[dict] = None) -> CapabilityResult:
    return CapabilityResult(ok=False,
                            error={"code": code, "message": message,
                                   "retryable": retryable},
                            trace=trace or {})
