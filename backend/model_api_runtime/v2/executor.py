"""V2 executor（spec §7.4）：确定性排空 planner 出的 action。

读并行（read_parallelism 闸）、写串行 + 守卫。每 action 出脱敏 status 事件（§9）。
所有 capabilities 调用是同步的（可能内部 httpx 打 enclave），经 asyncio.to_thread 桥到线程池，
并被跨所有 job 共享的 enclave_sem 框住（§11 R3 治 enclave 串行化放大）。

两套凭证：executor 只转发 enclave-auth 的 api_key/runtime_token 给
capabilities.registry.run_capability——它从不持有/转发用户 BYOK provider key
（那是 planner/responder 打模型时才用的，executor 不跟 LLM 说话）。

结果拆两半：action_results 含敏感 data（内存传给 responder，绝不落盘）；
action_digest 只粗计数（ok/count，落 runtime_state，spec §5/§9 红线）。
"""
from __future__ import annotations

import asyncio
from typing import Any

from capabilities import registry as cap_registry
from model_api_runtime.v2 import jobs_store
from model_api_runtime.v2 import status_stream

# 非 capability 的 planner 控制/延迟 action（final_response/preliminary_response 由
# responder 作者；sleep/capture_memory/schedule_followup 是 worker/别的子系统解读的控制
# 动作）。executor 只排空已注册 capability，其余（含这些已知控制类型和任何未知 type）一律
# SKIP —— 不跑、不算失败、不进 action_results/digest。
# 这个 frozenset 只用来在文档/日志里点名常见控制类型；真正判定看 _split_plan。
# 注意：schedule_wake/cancel_wake 曾在此列——Task 4 起它们是真正的 WRITE capability
# （registry.WRITE_ACTIONS），由 executor 串行跑，不再是控制动作。
_CONTROL_ACTIONS = frozenset({
    "final_response", "preliminary_response", "sleep", "capture_memory",
    "schedule_followup",
})


def _split_plan(plan: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """按序拆成 (reads, writes, skipped)。

    只有 type ∈ cap_registry.CAPABILITIES（即 READ_ACTIONS ∪ WRITE_ACTIONS）的 action
    才会真的跑：读入 reads（读并行闸），写入 writes（严格串行）。其余一律进 skipped——
    包括 final_response/preliminary_response（responder 的活）、sleep/capture_memory/
    schedule_followup（worker 或别的子系统解读的控制 action）、以及任何未知 type。
    （schedule_wake/cancel_wake 现在是 WRITE capability，走 writes 桶，不再 skip。）
    skipped 不跑 run_capability、不算 FAILURE、不进 action_results/
    action_digest——它们不是本轮 executor 的职责，误跑/误标失败会把「正常的控制流」
    喂成假失败塞回 runtime_state，污染下一轮 planner 的输入。
    """
    reads: list[dict] = []
    writes: list[dict] = []
    skipped: list[dict] = []
    for step in plan:
        t = str(step.get("type") or "")
        if t in cap_registry.READ_ACTIONS:
            reads.append(step)
        elif t in cap_registry.WRITE_ACTIONS:
            writes.append(step)
        else:
            skipped.append(step)
    return reads, writes, skipped


def partition_plan(plan: list[dict]) -> tuple[list[dict], list[dict]]:
    """按序拆成 (reads, writes)；向后兼容的薄封装——丢弃 skipped 桶。

    真正的三分（含 skipped 桶的落盘）在 execute_plan 里走 _split_plan；这个函数只保留
    给按 (reads, writes) 判读排序/并行行为的调用方/测试用。
    """
    reads, writes, _skipped = _split_plan(plan)
    return reads, writes


async def _run_one(store, step, *, api_key, runtime_token, enclave_sem) -> tuple[str, dict]:
    """跑一个 action：mark_running →（经 enclave_sem 框住的）到线程池同步跑 capability
    → mark_done/mark_failed。返回 (action_type, to_dict() 结果)。"""
    action_id = step.get("_action_id")
    if action_id is not None:
        await asyncio.to_thread(jobs_store.mark_action_running, action_id)
    t = str(step.get("type") or "")
    params = step.get("payload") or {}
    async with enclave_sem:
        result = await asyncio.to_thread(
            cap_registry.run_capability, t, store,
            api_key=api_key, runtime_token=runtime_token, params=params,
        )
    data = result.to_dict()
    if action_id is not None:
        if data.get("ok"):
            await asyncio.to_thread(jobs_store.mark_action_done, action_id, data)
        else:
            # jobs_store.mark_action_failed(action_id, error: str) — 传人可读的
            # "code: message"，不要 str(dict)（那是 repr，形如
            # "{'code': 'boom', 'message': 'nope', 'retryable': False}"，
            # 不是干净文案）。error 字典本就只含 code/message/retryable（已脱敏），
            # 这里只是格式化，不是新增敏感面。
            err = data.get("error") or {}
            await asyncio.to_thread(
                jobs_store.mark_action_failed, action_id,
                f"{err.get('code', 'error')}: {err.get('message', '')}")
    return t, data


def _emit(limiter: status_stream.RateLimiter, job_id, user_id, events: list[dict]) -> None:
    """落一批已脱敏的 status 事件，过限频闸（§9 红线 2）。"""
    for ev in events:
        if not limiter.allow(ev["kind"]):
            continue
        jobs_store.append_status_event(
            user_id, ev["kind"], job_id=job_id,
            label=ev.get("label"), detail=ev.get("detail") or {})


async def execute_plan(
    store,
    job_id,
    *,
    api_key: str,
    runtime_token: str,
    plan: list[dict],
    read_parallelism: int,
    enclave_sem: "asyncio.Semaphore",
) -> dict[str, Any]:
    """排空 plan：读并行（read_parallelism 闸）、写严格串行；非 capability 的控制/未知
    action（final_response/preliminary_response/sleep/capture_memory/schedule_followup/
    任何未知 type）一律 SKIP——不跑、不算失败。

    返回 {"action_results": {action_type: [result_dict,...]}, "action_digest": {action_type:
    {"ok","count"}}}。action_results 含敏感 data，只在内存传给 responder；action_digest
    非敏感，worker 落 runtime_state。skipped action 不出现在这两者里——它们不是
    executor 的失败，是别的子系统（responder/worker）解读的控制流。
    """
    reads, writes, skipped = _split_plan(plan)
    results: dict[str, list[dict]] = {}
    limiter = status_stream.RateLimiter(min_interval=0.4)

    # 跳过的控制/未知 action：只把带 _action_id 的队列行清成终态 skipped（DB 记账），
    # 绝不 mark_action_failed（不是失败），也绝不进 action_results/digest。
    if skipped:
        await asyncio.gather(*[
            asyncio.to_thread(jobs_store.mark_action_skipped, step["_action_id"])
            for step in skipped if step.get("_action_id") is not None
        ])

    # 并行读 burst 合并成 ≤1 条 status（§9 红线 2）——在真正发起读之前先报"读取中"。
    if reads:
        await asyncio.to_thread(
            _emit, limiter, job_id, store.user_id,
            status_stream.merge_parallel_reads(
                [status_stream.status_kind_for_action(str(s.get("type"))) for s in reads]),
        )

    read_sem = asyncio.Semaphore(max(1, int(read_parallelism)))

    async def _guarded(step):
        async with read_sem:
            return await _run_one(store, step, api_key=api_key, runtime_token=runtime_token,
                                   enclave_sem=enclave_sem)

    read_out = await asyncio.gather(*[_guarded(s) for s in reads]) if reads else []
    for t, data in read_out:
        results.setdefault(t, []).append(data)

    # 写严格串行：逐条跑完再跑下一条，每条自己一行 status（不合并，写的可见性更重要）。
    for step in writes:
        t_hint = str(step.get("type") or "")
        await asyncio.to_thread(
            _emit, limiter, job_id, store.user_id,
            [status_stream.redact_status(status_stream.status_kind_for_action(t_hint))],
        )
        t, data = await _run_one(store, step, api_key=api_key, runtime_token=runtime_token,
                                  enclave_sem=enclave_sem)
        results.setdefault(t, []).append(data)

    return {"action_results": results, "action_digest": _digest(results)}


def _digest(results: dict[str, list[dict]]) -> dict[str, dict]:
    """非敏感粗计数——只 ok/count，绝无解密体（§5/§9）。"""
    out: dict[str, dict] = {}
    for action_type, runs in results.items():
        out[action_type] = {"ok": sum(1 for r in runs if r.get("ok")), "count": len(runs)}
    return out
