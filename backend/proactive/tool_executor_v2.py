"""Executable contract for Proactive/Perception V2 tools.

PR3 keeps this layer independent from hosted/resident cutover. Callers can
inject output adapters for side-effecting actions while the shared catalog,
budgeting, unavailable-tool behavior, and traces stay identical.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import time
import uuid
from typing import Any, Callable, Mapping, Sequence

import db
from perception.agent_fields import project_signal
from proactive.agent_protocol_v2 import sanitize_visible_message_text_v2
from proactive.tool_catalog_v2 import FAST, SLOW, CostClass, ToolCatalogV2, default_tool_catalog_v2

TOOL_TRACE_STREAM_V2 = "proactive_tool_traces_v2"

# proactive perception.* tool -> agent signal name. Both paths project through
# perception.agent_fields.project_signal so proactive sees the SAME signals/fields
# as the CLI-tools path (A-lite unification). now / now_playing / photo_recent are
# special-cased in _execute_available.
_PERCEPTION_TOOL_SIGNAL_V2 = {
    "perception.location": "location",
    "perception.calendar": "calendar",
    "perception.motion": "motion",
    "perception.audio_route": "audio_route",
    "perception.weather": "weather",
    "perception.steps": "steps",
    "perception.sleep_last_night": "sleep",
    "perception.workout": "workout",
    "perception.vitals": "vitals",
    "perception.reminders": "reminders",
    "perception.activity": "activity",
    "perception.body": "body",
    "perception.metabolic": "metabolic",
    "perception.cycle": "cycle",
    "perception.mood": "mood",
}

PR3_UNIMPLEMENTED_TOOLS_V2 = frozenset({"schedule_wake", "cancel_wake"})  # screen.read/screen.recent now implemented in _execute_available
MEMORY_WRITE_TOOL_NAMES_V2 = frozenset({
    "memory.add",
    "memory.supersede",
    "memory.delete",
    "memory.retype",
})


def _new_tool_call_id() -> str:
    return "tool_" + uuid.uuid4().hex[:16]


@dataclass(frozen=True)
class ToolCallV2:
    name: str
    args: Mapping[str, Any] = field(default_factory=dict)
    user_id: str = ""
    wake_id: str = ""
    turn_id: str = ""
    call_id: str = field(default_factory=_new_tool_call_id)


@dataclass(frozen=True)
class ToolTraceV2:
    call_id: str
    name: str
    cost_class: CostClass
    outcome: str
    latency_ms: float
    wake_id: str = ""
    turn_id: str = ""
    user_id: str = ""
    error_code: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "name": self.name,
            "cost_class": self.cost_class,
            "outcome": self.outcome,
            "latency_ms": self.latency_ms,
            "wake_id": self.wake_id,
            "turn_id": self.turn_id,
            "user_id": self.user_id,
            "error_code": self.error_code,
        }


class DBToolTraceSinkV2:
    def __call__(self, trace: ToolTraceV2) -> None:
        doc = {
            "kind": "tool_trace_v2",
            **trace.as_dict(),
            "ts": time.time(),
        }
        db.log_append(
            trace.user_id,
            TOOL_TRACE_STREAM_V2,
            doc,
            ts=doc["ts"],
            item_key=trace.call_id,
        )


@dataclass(frozen=True)
class ToolResultV2:
    ok: bool
    outcome: str
    result: Mapping[str, Any] = field(default_factory=dict)
    error_code: str = ""
    error_message: str = ""
    needs_background: bool = False
    trace: ToolTraceV2 | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "outcome": self.outcome,
            "result": dict(self.result or {}),
            "error_code": self.error_code,
            "error_message": self.error_message,
            "needs_background": self.needs_background,
            "trace": self.trace.as_dict() if self.trace else None,
        }


@dataclass(frozen=True)
class ToolBudgetV2:
    fast_hard_limit: int = 6
    slow_inline_limit: int = 1
    background_hard_limit: int = 25
    background: bool = False


@dataclass
class ToolBudgetStateV2:
    fast_calls: int = 0
    slow_calls: int = 0
    background_calls: int = 0

    def note(self, cost_class: CostClass, *, background: bool = False) -> None:
        if background:
            self.background_calls += 1
        elif cost_class == SLOW:
            self.slow_calls += 1
        else:
            self.fast_calls += 1


@dataclass(frozen=True)
class ToolRuntimeAdaptersV2:
    perception_snapshot: Callable[[str], Mapping[str, Any]] | None = None
    perception_pull_snapshot: Callable[[str], Mapping[str, Any]] | None = None
    perception_recent_apps: Callable[[str, Mapping[str, Any]], Mapping[str, Any]] | None = None
    photos_recent: Callable[[str, int], Mapping[str, Any]] | None = None
    memory_load: Callable[[str], Sequence[Mapping[str, Any]]] | None = None
    memory_index: Callable[[str, Mapping[str, Any]], Mapping[str, Any]] | None = None
    memory_fetch: Callable[[str, Mapping[str, Any]], Mapping[str, Any]] | None = None
    memory_action: Callable[[str, Mapping[str, Any]], Mapping[str, Any]] | None = None
    send_message: Callable[[str, str, Mapping[str, Any]], Mapping[str, Any]] | None = None
    screen_read: Callable[[str, str | None, str], Mapping[str, Any]] | None = None
    screen_recent: Callable[[str, int], Mapping[str, Any]] | None = None


def default_tool_runtime_adapters_v2() -> ToolRuntimeAdaptersV2:
    def perception_snapshot(user_id: str) -> Mapping[str, Any]:
        from perception import service as perception_service

        return perception_service.snapshot(user_id)

    def perception_pull_snapshot(user_id: str) -> Mapping[str, Any]:
        from perception import service as perception_service

        return perception_service.pull_snapshot(user_id)

    def perception_recent_apps(user_id: str, args: Mapping[str, Any]) -> Mapping[str, Any]:
        from perception import service as perception_service

        hours = args.get("hours")
        try:
            hours = float(hours) if hours is not None else None
        except (TypeError, ValueError):
            hours = None
        return perception_service.recent_apps(
            user_id,
            limit=_int_arg(args.get("limit"), default=20, lo=1, hi=100),
            hours=hours if (hours or 0) > 0 else None,
        )

    def photos_recent(user_id: str, limit: int) -> Mapping[str, Any]:
        from perception import service as perception_service

        body, code = perception_service.photos_recent(user_id, limit=limit)
        return {"status_code": code, **dict(body or {})}

    def memory_load(user_id: str) -> Sequence[Mapping[str, Any]]:
        import db

        return db.memory_load(user_id)

    return ToolRuntimeAdaptersV2(
        perception_snapshot=perception_snapshot,
        perception_pull_snapshot=perception_pull_snapshot,
        perception_recent_apps=perception_recent_apps,
        photos_recent=photos_recent,
        memory_load=memory_load,
    )


def screen_runtime_adapters_v2(api_key: str, store) -> ToolRuntimeAdaptersV2:
    """Screen adapters bound to the per-turn api_key (needed to reach the
    enclave) and gated by the fail-closed screen_caption_enabled flag."""
    from screen import caption as screen_caption
    from proactive.screen_flag_v2 import screen_caption_enabled

    def screen_read(user_id: str, frame_id: str | None, mode: str) -> Mapping[str, Any]:
        if not screen_caption_enabled(store):
            raise ToolUnavailableV2("screen_caption_disabled", "screen captioning is off for this user")
        return screen_caption.caption_frame(user_id, api_key, frame_id, mode)

    def screen_recent(user_id: str, limit: int) -> Mapping[str, Any]:
        return screen_caption.recent_frames(user_id, limit)

    return ToolRuntimeAdaptersV2(screen_read=screen_read, screen_recent=screen_recent)


def combined_runtime_adapters_v2(api_key: str, store) -> ToolRuntimeAdaptersV2:
    """Default perception/memory adapters + screen adapters bound to this turn's
    api_key/store, so the executor can reach every implemented tool."""
    import dataclasses
    import memory_readside_core

    base = default_tool_runtime_adapters_v2()
    screen = screen_runtime_adapters_v2(api_key, store)

    def memory_index(user_id: str, args: Mapping[str, Any]) -> Mapping[str, Any]:
        return memory_readside_core.memory_index_core(store, api_key, dict(args or {}))

    def memory_fetch(user_id: str, args: Mapping[str, Any]) -> Mapping[str, Any]:
        return memory_readside_core.memory_fetch_core(store, api_key, dict(args or {}))

    def memory_action(user_id: str, args: Mapping[str, Any]) -> Mapping[str, Any]:
        from accounts import runtime_auth
        from memory import actions as memory_actions

        runtime_auth.authorize_scope("memory")
        body, status = memory_actions._execute_memory_actions(store, api_key, [dict(args or {})])
        return {"status_code": status, **dict(body or {})}

    return dataclasses.replace(base, screen_read=screen.screen_read,
                               screen_recent=screen.screen_recent,
                               memory_index=memory_index,
                               memory_fetch=memory_fetch,
                               memory_action=memory_action)


class ToolExecutorV2:
    def __init__(
        self,
        *,
        catalog: ToolCatalogV2 | None = None,
        adapters: ToolRuntimeAdaptersV2 | None = None,
        budget: ToolBudgetV2 | None = None,
        trace_sink: Callable[[ToolTraceV2], None] | None = None,
    ) -> None:
        self.catalog = catalog or default_tool_catalog_v2()
        self.adapters = adapters or default_tool_runtime_adapters_v2()
        self.budget = budget or ToolBudgetV2()
        self.budget_state = ToolBudgetStateV2()
        self.trace_sink = trace_sink
        self.traces: list[ToolTraceV2] = []

    def execute(self, call: ToolCallV2) -> ToolResultV2:
        started = time.perf_counter()
        args = dict(call.args or {})
        try:
            cost_class = self.catalog.cost_class_for(call.name, args)
        except KeyError:
            return self._finish(
                call,
                FAST,
                started,
                ok=False,
                outcome="error",
                error_code="unknown_tool",
                error_message=f"unknown tool: {call.name}",
            )

        if call.name in PR3_UNIMPLEMENTED_TOOLS_V2:
            return self._finish(
                call,
                cost_class,
                started,
                ok=False,
                outcome="unavailable",
                error_code="tool_not_implemented_in_pr3",
                error_message=f"{call.name} is cataloged but not implemented in PR3.",
            )
        dynamic_unavailable = self._dynamic_unavailable(call, args)
        if dynamic_unavailable:
            code, message = dynamic_unavailable
            return self._finish(
                call,
                cost_class,
                started,
                ok=False,
                outcome="unavailable",
                error_code=code,
                error_message=message,
            )

        handoff = self._budget_handoff_reason(cost_class)
        if handoff:
            return self._finish(
                call,
                cost_class,
                started,
                ok=False,
                outcome="needs_background",
                error_code=handoff,
                error_message="tool budget reached; hand off to background",
                needs_background=True,
            )

        try:
            result = self._execute_available(call, args)
        except ToolUnavailableV2 as e:
            return self._finish(
                call,
                cost_class,
                started,
                ok=False,
                outcome="unavailable",
                error_code=e.code,
                error_message=e.message,
            )
        except ToolExecutionErrorV2 as e:
            return self._finish(
                call,
                cost_class,
                started,
                ok=False,
                outcome="error",
                result=e.result,
                error_code=e.code,
                error_message=e.message,
            )
        except Exception as e:
            return self._finish(
                call,
                cost_class,
                started,
                ok=False,
                outcome="error",
                error_code=type(e).__name__,
                error_message=str(e)[:240],
            )

        self.budget_state.note(cost_class, background=self.budget.background)
        return self._finish(call, cost_class, started, ok=True, outcome="ok", result=result)

    def _dynamic_unavailable(self, call: ToolCallV2, args: Mapping[str, Any]) -> tuple[str, str] | None:
        if call.name in {
            "perception.now",
            "perception.now_playing",
        } and not self.adapters.perception_snapshot:
            return ("perception_snapshot_adapter_missing", "perception snapshot adapter is not configured")
        if call.name in _PERCEPTION_TOOL_SIGNAL_V2 and not (
            self.adapters.perception_pull_snapshot or self.adapters.perception_snapshot
        ):
            return ("perception_snapshot_adapter_missing", "perception snapshot adapter is not configured")
        if call.name == "perception.recent_apps" and not self.adapters.perception_recent_apps:
            return ("perception_recent_apps_adapter_missing", "perception recent-apps adapter is not configured")
        if call.name == "perception.photo_recent" and not self.adapters.photos_recent:
            return ("photo_recent_adapter_missing", "photo recent adapter is not configured")
        if call.name == "memory.index" and not (self.adapters.memory_index or self.adapters.memory_load):
            return ("memory_adapter_missing", "memory.index adapter is not configured")
        if call.name == "memory.fetch" and not (self.adapters.memory_fetch or self.adapters.memory_load):
            return ("memory_adapter_missing", "memory.fetch adapter is not configured")
        if call.name == "memory.fetch" and not _string_list(args.get("ids") or args.get("id")):
            return ("memory_ids_required", "memory.fetch requires one or more ids")
        if call.name in MEMORY_WRITE_TOOL_NAMES_V2 and not self.adapters.memory_action:
            return ("memory_action_adapter_missing", f"{call.name} requires a memory action adapter")
        if call.name == "send_message":
            if not self.adapters.send_message:
                return ("send_message_adapter_missing", "send_message requires a hosted/resident output adapter")
            text = sanitize_visible_message_text_v2(args.get("text"))
            if not text:
                return ("send_message_text_required", "send_message requires non-empty text")
        if call.name == "screen.read" and not self.adapters.screen_read:
            return ("screen_adapter_missing", "screen.read requires a screen runtime adapter")
        if call.name == "screen.recent" and not self.adapters.screen_recent:
            return ("screen_adapter_missing", "screen.recent requires a screen runtime adapter")
        return None

    def _budget_handoff_reason(self, cost_class: CostClass) -> str:
        if self.budget.background:
            return (
                "background_budget_soft_handoff"
                if self.budget_state.background_calls >= self.budget.background_hard_limit
                else ""
            )
        if cost_class == SLOW and self.budget_state.slow_calls >= self.budget.slow_inline_limit:
            return "slow_budget_soft_handoff"
        if cost_class == FAST and self.budget_state.fast_calls >= self.budget.fast_hard_limit:
            return "fast_budget_soft_handoff"
        return ""

    def _execute_available(self, call: ToolCallV2, args: Mapping[str, Any]) -> Mapping[str, Any]:
        if call.name == "perception.now":
            return {"snapshot": dict(self._snapshot(call.user_id))}
        if call.name == "perception.now_playing":
            return {"now_playing": self._snapshot(call.user_id).get("now_playing")}
        if call.name == "perception.recent_apps":
            assert self.adapters.perception_recent_apps is not None
            return dict(self.adapters.perception_recent_apps(call.user_id, args))
        sig = _PERCEPTION_TOOL_SIGNAL_V2.get(call.name)
        if sig is not None:
            # One projection source of truth with the CLI-tools path: every field
            # comes from perception.agent_fields.project_signal, so new iOS signals
            # (reminders/activity/body/metabolic/cycle/mood + expanded weather/
            # sleep/vitals) reach the proactive agent automatically.
            doc = project_signal(sig, {}, self._pull_snapshot(call.user_id))
            return {call.name.split(".", 1)[1]: doc}
        if call.name == "perception.photo_recent":
            limit = _int_arg(args.get("limit"), default=10, lo=1, hi=50)
            assert self.adapters.photos_recent is not None
            return dict(self.adapters.photos_recent(call.user_id, limit))
        if call.name == "memory.index":
            if self.adapters.memory_index:
                return _normalize_memory_tool_result(self.adapters.memory_index(call.user_id, args))
            return {"memories": [_memory_index_item(memory) for memory in self._memories(call.user_id)]}
        if call.name == "memory.fetch":
            ids = _string_list(args.get("ids") or args.get("id"))
            if self.adapters.memory_fetch:
                return _normalize_memory_tool_result(self.adapters.memory_fetch(call.user_id, args))
            by_id = {str(memory.get("id") or ""): dict(memory) for memory in self._memories(call.user_id)}
            return {"memories": [by_id[item] for item in ids if item in by_id], "missing_ids": [item for item in ids if item not in by_id]}
        if call.name in MEMORY_WRITE_TOOL_NAMES_V2:
            return self._execute_memory_action_tool(call, args)
        if call.name == "send_message":
            text = sanitize_visible_message_text_v2(args.get("text"))
            assert self.adapters.send_message is not None
            return dict(self.adapters.send_message(call.user_id, text, args))
        if call.name == "sleep":
            return {"sleep": True, "reason": str(args.get("reason") or "")[:240]}
        if call.name == "screen.read":
            assert self.adapters.screen_read is not None
            mode = str(args.get("mode") or "caption").lower()
            frame_id = args.get("frame_id")
            res = dict(self.adapters.screen_read(call.user_id, frame_id, mode))
            if res.get("error"):
                raise ToolUnavailableV2(str(res["error"]), f"screen.read: {res['error']}")
            return res
        if call.name == "screen.recent":
            assert self.adapters.screen_recent is not None
            limit = _int_arg(args.get("limit"), default=10, lo=1, hi=50)
            return dict(self.adapters.screen_recent(call.user_id, limit))
        raise ToolUnavailableV2("tool_not_implemented_in_pr3", f"{call.name} is cataloged but not implemented in PR3")

    def _snapshot(self, user_id: str) -> Mapping[str, Any]:
        if not self.adapters.perception_snapshot:
            raise ToolUnavailableV2("perception_snapshot_adapter_missing", "perception snapshot adapter is not configured")
        return self.adapters.perception_snapshot(user_id)

    def _pull_snapshot(self, user_id: str) -> Mapping[str, Any]:
        if self.adapters.perception_pull_snapshot:
            return self.adapters.perception_pull_snapshot(user_id)
        return self._snapshot(user_id)

    def _memories(self, user_id: str) -> Sequence[Mapping[str, Any]]:
        if not self.adapters.memory_load:
            raise ToolUnavailableV2("memory_adapter_missing", "memory adapter is not configured")
        return self.adapters.memory_load(user_id)

    def _execute_memory_action_tool(self, call: ToolCallV2, args: Mapping[str, Any]) -> Mapping[str, Any]:
        action = dict(args or {})
        action["type"] = call.name
        if call.name in {"memory.add", "memory.supersede"}:
            envelope = action.get("envelope")
            if "memory" in action or "content" in action or "summary" in action or "description" in action:
                raise ToolExecutionErrorV2(
                    "needs_client_encryption",
                    f"{call.name} requires a pre-encrypted memory envelope; plaintext memory content is not accepted by this tool.",
                    result={
                        "status": "error",
                        "error": "needs_client_encryption",
                        "action": call.name,
                        "required": "Build a v1 memory envelope client-side, then retry with args.envelope.",
                    },
                )
            if not isinstance(envelope, Mapping):
                raise ToolExecutionErrorV2(
                    "needs_client_encryption",
                    f"{call.name} requires args.envelope because memory writes must cross the client encryption boundary before tool execution.",
                    result={
                        "status": "error",
                        "error": "needs_client_encryption",
                        "action": call.name,
                        "required": "Provide a sealed v1 envelope with body_ct, nonce, K_user, visibility, owner_user_id, type, and occurred_at.",
                    },
                )
            action["envelope"] = dict(envelope)
        assert self.adapters.memory_action is not None
        response = dict(self.adapters.memory_action(call.user_id, action) or {})
        status_code = _int_arg(response.get("status_code"), default=200, lo=100, hi=599)
        if status_code >= 400 or str(response.get("status") or "").lower() == "error":
            code = _memory_action_error_code(response)
            raise ToolExecutionErrorV2(code, _memory_action_error_message(response, code), result=response)
        return response

    def _finish(
        self,
        call: ToolCallV2,
        cost_class: CostClass,
        started: float,
        *,
        ok: bool,
        outcome: str,
        result: Mapping[str, Any] | None = None,
        error_code: str = "",
        error_message: str = "",
        needs_background: bool = False,
    ) -> ToolResultV2:
        trace = ToolTraceV2(
            call_id=call.call_id,
            name=call.name,
            cost_class=cost_class,
            outcome=outcome,
            latency_ms=max(0.0, (time.perf_counter() - started) * 1000.0),
            wake_id=call.wake_id,
            turn_id=call.turn_id,
            user_id=call.user_id,
            error_code=error_code,
        )
        self.traces.append(trace)
        if self.trace_sink:
            self.trace_sink(trace)
        return ToolResultV2(
            ok=ok,
            outcome=outcome,
            result=dict(result or {}),
            error_code=error_code,
            error_message=error_message,
            needs_background=needs_background,
            trace=trace,
        )


class ToolUnavailableV2(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ToolExecutionErrorV2(Exception):
    def __init__(self, code: str, message: str, *, result: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.result = dict(result or {})


def _int_arg(value: Any, *, default: int, lo: int, hi: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(lo, min(hi, parsed))


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, Sequence):
        return [str(item) for item in value if str(item)]
    return []


def _memory_index_item(memory: Mapping[str, Any]) -> dict[str, Any]:
    title = str(memory.get("title") or memory.get("summary") or memory.get("bucket") or "")[:160]
    mem_type = str(memory.get("type") or memory.get("bucket") or "memory")[:80]
    return {
        "id": str(memory.get("id") or ""),
        "type": mem_type,
        "title": title,
        "occurred_at": str(memory.get("occurred_at") or ""),
        "updated_at": str(memory.get("updated_at") or ""),
        "is_archived": bool(memory.get("is_archived")),
    }


def _normalize_memory_tool_result(result: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(result or {})
    if "memories" not in out and isinstance(out.get("items"), list):
        out["memories"] = list(out.get("items") or [])
    return out


def _memory_action_error_code(response: Mapping[str, Any]) -> str:
    error = str(response.get("error") or "").strip()
    if error:
        return error[:120]
    results = response.get("results")
    if isinstance(results, Sequence) and results:
        first = results[0]
        if isinstance(first, Mapping) and first.get("error"):
            return str(first.get("error"))[:120]
    return "memory_action_failed"


def _memory_action_error_message(response: Mapping[str, Any], code: str) -> str:
    if response.get("required"):
        return str(response.get("required"))[:240]
    results = response.get("results")
    if isinstance(results, Sequence) and results:
        first = results[0]
        if isinstance(first, Mapping):
            if first.get("required"):
                return str(first.get("required"))[:240]
            if first.get("message"):
                return str(first.get("message"))[:240]
    return f"memory action failed: {code}"
