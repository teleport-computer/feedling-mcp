"""Normalized provider transport types (Hosted Runtime V2 PR B / spec B1).

Top-level module (alongside provider_client.py) so both v2 and legacy callers
can import it without a top-level->v2-subpackage dependency. PURE: no
hosted/agent_runtime/db imports. `ProviderResponse` is a typed VIEW over the
dict that provider_client.chat_completion* returns — the dict stays the wire
return type for backward compatibility; PR C uses from_result() for typed access.
"""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict  # JSON Schema object


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    args: dict
    args_raw: str = ""      # provider's raw args string when JSON parse failed
    args_ok: bool = True    # False -> args_raw holds the unparseable original


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    content: str


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None


@dataclass(frozen=True)
class ProviderResponse:
    text: str
    tool_calls: list[ToolCall]
    usage: Usage
    raw: dict

    @classmethod
    def from_result(cls, result: dict) -> "ProviderResponse":
        raw_calls = result.get("tool_calls") or []
        calls = [
            ToolCall(
                id=str(c.get("id") or ""),
                name=str(c.get("name") or ""),
                args=dict(c.get("args") or {}),
                args_raw=str(c.get("args_raw") or ""),
                args_ok=bool(c.get("args_ok", True)),
            )
            for c in raw_calls
        ]
        u = result.get("usage") or {}
        usage = Usage(
            prompt_tokens=u.get("prompt_tokens"),
            completion_tokens=u.get("completion_tokens"),
            total_tokens=u.get("total_tokens"),
        )
        return cls(text=str(result.get("reply") or ""), tool_calls=calls,
                   usage=usage, raw=result)
