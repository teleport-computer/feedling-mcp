"""Provider-native V2 tool-call dispatcher.

Read capabilities execute inline with bounded parallelism so their results can
feed the next model round. Write capabilities pass the provenance gate and are
enqueued as generation-fenced effects, strictly in model order; they never run
inline. Synchronous capability work is moved off the event-loop thread and is
bounded by the shared enclave semaphore.
"""
from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any

from capabilities import registry as cap_registry
from capabilities import tool_schema
from capabilities import identity as cap_identity
from model_api_runtime.v2 import provenance as _prov
from provider_types import ToolResult

async def _run_one(store, step, *, api_key, runtime_token, enclave_sem) -> tuple[str, dict]:
    """Run one synchronous capability behind the shared enclave gate."""
    t = str(step.get("type") or "")
    params = step.get("payload") or {}
    async with enclave_sem:
        result = await asyncio.to_thread(
            cap_registry.run_capability, t, store,
            api_key=api_key, runtime_token=runtime_token, params=params,
        )
    data = result.to_dict()
    return t, data


# ---------------------------------------------------------------------------
# Provider-native dispatcher. Writes are generation-fenced effects (enqueued,
# never run inline) gated by provenance.write_gate.
# ---------------------------------------------------------------------------

# 单个 tool_call 结果喂回模型前的字符上限。没有它，一个返回大 blob 的 capability
# 能把整轮对话的 context 预算吃光（BUG-1 的一般形式）。这与
# context.fold_action_results 使用相同的 blob 剥离 + 单结果截断原则，
# 只是这里的单位是一个 native tool call。
_RESULT_CHAR_CAP = 2000
_BLOB_KEYS = frozenset({"image_b64"})


def _strip_blobs(value: Any) -> Any:
    """递归剥掉 `_BLOB_KEYS`。"""
    if isinstance(value, dict):
        return {k: _strip_blobs(v) for k, v in value.items() if k not in _BLOB_KEYS}
    if isinstance(value, list):
        return [_strip_blobs(v) for v in value]
    return value


def _summarize_capability_result(data: dict) -> str:
    """把单次 `run_capability(...).to_dict()` 结果渲染成喂回模型 tool content 的
    字符串：失败只暴露稳定 error code（不回显 message，可能带解密体/URL/查询词），
    成功先剥 blob 键再 json 序列化，超 `_RESULT_CHAR_CAP` 截断——防一个肥 capability
    把整轮对话挤爆（BUG-1 defence-in-depth）。"""
    if not isinstance(data, dict):
        return str(data)[:_RESULT_CHAR_CAP]
    if not data.get("ok"):
        err = data.get("error") or {}
        code = err.get("code") if isinstance(err, dict) else None
        return f"error: {code or 'capability_failed'}"
    payload = _strip_blobs(data.get("data"))
    if not payload:
        return "ok"
    rendered = json.dumps(payload, ensure_ascii=False)
    if len(rendered) > _RESULT_CHAR_CAP:
        rendered = rendered[:_RESULT_CHAR_CAP] + "...[truncated]"
    return rendered


async def dispatch_tool_calls(
    tool_calls, *, store, api_key, runtime_token, enclave_sem,
    turn_authorization: bool, enqueue_write_effect, before_write=None,
    enqueue_workspace_batch_effect=None,
    read_parallelism: int = 4,
) -> list[ToolResult]:
    """Dispatch provider tool_calls (spec C3). READ_ACTIONS run inline-parallel (their content
    feeds back to the model); WRITE_ACTIONS pass the provenance write_gate then are ENQUEUED as
    generation-fenced effects (not run inline); `reply` is handled by the caller (tool_loop), not
    here — it never appears in READ_ACTIONS/WRITE_ACTIONS so it falls into the unknown-tool branch
    if ever passed in by mistake. Every call_id is preserved in the returned ToolResults, in the
    original tool_calls order. Never raises on a bad tool_call."""
    results_by_id: dict[str, ToolResult] = {}
    reads, writes = [], []
    for tc in tool_calls:
        if not tc.args_ok:
            results_by_id[tc.id] = ToolResult(call_id=tc.id, content=f"error: unparseable args for {tc.name}")
        elif tc.name not in cap_registry.READ_ACTIONS and tc.name not in cap_registry.WRITE_ACTIONS:
            results_by_id[tc.id] = ToolResult(call_id=tc.id, content=f"error: unknown tool {tc.name}")
        elif validation_error := tool_schema.validate_tool_args(tc.name, tc.args):
            results_by_id[tc.id] = ToolResult(
                call_id=tc.id, content=f"error: invalid args for {tc.name}: {validation_error}")
        elif tc.name in cap_registry.READ_ACTIONS:
            reads.append(tc)
        elif tc.name in cap_registry.WRITE_ACTIONS:
            writes.append(tc)

    async def _read(tc):
        step = {"type": tc.name, "payload": tc.args}
        try:
            _t, data = await _run_one(
                store, step, api_key=api_key, runtime_token=runtime_token,
                enclave_sem=enclave_sem,
            )
            content = _summarize_capability_result(data)
        except Exception:  # noqa: BLE001 — isolate one bad read; never expose its exception
            # Read calls are independent and side-effect-free.  A capability bug or
            # adapter failure must not discard successful sibling results or fail the
            # whole turn.  Keep the error vocabulary stable and privacy-safe: exception
            # text can contain decrypted data, URLs, or query terms.  Cancellation is a
            # BaseException on supported Python versions and therefore still propagates.
            content = "error: capability_failed"
        return tc.id, ToolResult(call_id=tc.id, content=content)

    read_sem = asyncio.Semaphore(max(1, int(read_parallelism)))

    async def _guarded(tc):
        async with read_sem:
            return await _read(tc)

    for cid, tr in (await asyncio.gather(*[_guarded(t) for t in reads]) if reads else []):
        results_by_id[cid] = tr

    write_index = 0
    while write_index < len(writes):
        tc = writes[write_index]

        # A contiguous workspace run may share one encrypted outbox row.  The
        # row is still one ordered mutation relative to every platform/MCP
        # mutation around it; the sink alone decides which disjoint paths may
        # overlap at the durable backend boundary.  Keeping batching opt-in
        # preserves compatibility callers and makes it impossible for this
        # preparation layer to pretend that merely concurrent encryption is a
        # concurrent durable write.
        if (
            enqueue_workspace_batch_effect is not None
            and tc.name in {"workspace_write", "workspace_delete"}
        ):
            run = []
            while write_index < len(writes):
                candidate = writes[write_index]
                if candidate.name not in {"workspace_write", "workspace_delete"}:
                    break
                allowed, reason = _prov.write_gate(
                    candidate.name,
                    turn_authorization=turn_authorization,
                )
                if not allowed:
                    results_by_id[candidate.id] = ToolResult(
                        call_id=candidate.id,
                        content=reason,
                    )
                else:
                    if before_write is not None:
                        await before_write()
                    run.append(candidate)
                write_index += 1
            if run:
                enqueued = enqueue_workspace_batch_effect(run)
                if inspect.isawaitable(enqueued):
                    await enqueued
                for candidate in run:
                    results_by_id[candidate.id] = ToolResult(
                        call_id=candidate.id,
                        content=f"queued: {candidate.name}",
                    )
            continue

        # Every non-workspace mutation remains strictly serial in provider
        # order.  A workspace run stops before reaching this branch, so it can
        # never overtake a memory/identity/schedule/MCP operation.
        allowed, reason = _prov.write_gate(tc.name, turn_authorization=turn_authorization)
        if not allowed:
            results_by_id[tc.id] = ToolResult(call_id=tc.id, content=reason)
            write_index += 1
            continue
        # LIVE-only semantic gate (Codex I1): a rename must carry
        # self_introduction in the same identity_patch call. The authoritative
        # server-side gate (card_policy.validate_rename_pairing) only fires at
        # the SINK, at end-of-turn, AFTER the model already got "queued" and
        # moved on — so a single-field rename would fail silently with no tool
        # error the model can self-correct from. Surfacing it here, before the
        # effect is enqueued, hands the model a fixable error THIS turn. Kept
        # out of tool_schema.validate_tool_args on purpose: that also gates
        # replay of already-persisted effects, where re-rejecting a legal-when-
        # written rename would terminal-discard it. The server gate remains the
        # final defense; this only front-runs its message on the live path.
        if tc.name == "identity_patch":
            pairing_error = cap_identity.rename_pairing_error(tc.args)
            if pairing_error:
                results_by_id[tc.id] = ToolResult(
                    call_id=tc.id, content=f"error: {pairing_error}")
                write_index += 1
                continue
            # Same LIVE-only + pre-enqueue rationale as the pairing check above:
            # reject a malformed / over-cap relationship_days BEFORE it is
            # enqueued, so the model gets a fixable error THIS turn instead of the
            # effect 400-ing (or OverflowError-looping) at the sink. Kept out of
            # tool_schema.validate_tool_args on purpose — that also gates replay.
            days_error = cap_identity.relationship_days_error(tc.args)
            if days_error:
                results_by_id[tc.id] = ToolResult(
                    call_id=tc.id, content=f"error: {days_error}")
                write_index += 1
                continue
        if before_write is not None:
            await before_write()
        enqueued = enqueue_write_effect(tc)  # producer -> PR A outbox (drained at turn end)
        if inspect.isawaitable(enqueued):
            await enqueued
        results_by_id[tc.id] = ToolResult(call_id=tc.id, content=f"queued: {tc.name}")
        write_index += 1

    return [results_by_id[tc.id] for tc in tool_calls]   # preserve original order + every call_id
