"""Normalized provider transport types (Hosted Runtime V2 PR B / spec B1).

Top-level module (alongside provider_client.py) so both v2 and legacy callers
can import it without a top-level->v2-subpackage dependency. PURE: no
hosted/agent_runtime/db imports. `ProviderResponse` is a typed VIEW over the
dict that provider_client.chat_completion* returns — the dict stays the wire
return type for backward compatibility; PR C uses from_result() for typed access.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any


# Cross-layer stable sentinel for an MCP call whose transport failed after the
# request may have reached the remote server. Hosted produces it; the dependency-
# clean V2 worker consumes it and converts mutating calls to outcome-unknown.
MCP_TRANSPORT_FAILURE_ERROR = "error: mcp_transport_failure"


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
class NativeAssistantTurn:
    """Opaque provider-native assistant output needed for the next tool round.

    Reconstructing an assistant tool-call turn from normalized ``ToolCall`` values is
    lossy for provider-specific fields (for example Gemini thought signatures and
    OpenAI Responses item ids).  Provider parsers therefore retain the exact JSON
    fragment and label the wire that produced it.  It stays in-process only; it is
    never persisted in runtime state.
    """

    wire: str
    payload: Any


@dataclass(frozen=True)
class ToolExchange:
    """One assistant tool-call turn and its complete, call-id-matched results.

    ``assistant_turn`` is present for real provider responses.  It may be ``None``
    for unit-test/legacy fakes that return only normalized calls; provider_client
    has a deterministic synthesis fallback for that compatibility path.
    """

    calls: tuple[ToolCall, ...]
    results: tuple[ToolResult, ...]
    assistant_text: str = ""
    assistant_turn: NativeAssistantTurn | None = None


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cache_miss_tokens: int | None = None


@dataclass(frozen=True)
class ProviderResponse:
    text: str
    tool_calls: list[ToolCall]
    usage: Usage
    raw: dict
    assistant_turn: NativeAssistantTurn | None = None

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
            cache_read_tokens=u.get("cache_read_tokens"),
            cache_write_tokens=u.get("cache_write_tokens"),
            cache_miss_tokens=u.get("cache_miss_tokens"),
        )
        raw_turn = result.get("assistant_turn")
        if isinstance(raw_turn, NativeAssistantTurn):
            assistant_turn = raw_turn
        elif isinstance(raw_turn, dict) and raw_turn.get("wire") and "payload" in raw_turn:
            assistant_turn = NativeAssistantTurn(
                wire=str(raw_turn["wire"]), payload=raw_turn["payload"])
        else:
            assistant_turn = None
        return cls(text=str(result.get("reply") or ""), tool_calls=calls,
                   usage=usage, raw=result, assistant_turn=assistant_turn)
