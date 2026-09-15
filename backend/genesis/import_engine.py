"""托管侧接 ``memory.garden_import``：模型调用、写库、读已有卡。

引擎本身（``memory/garden_import.py``）不认识 io 的 provider、加密和执行器；这里把
三样东西接上，托管的 plaintext genesis、旧的 ``/v1/history_import/upload`` 和加密分块
worker 共用这一份，不各写一套。VPS 自托管在 consumer 里接自己的三样（本地 agent 当模型、
客户端封信封、HTTP 写库），引擎是同一个。
"""
from __future__ import annotations

import os
from typing import Callable

import debug_trace
import provider_client
from genesis.llm_client import GenesisLLMClient
from memory import actions as memory_actions
from memory import garden_import

#: 导入写卡/抽候选一次调用的输出预算。两段式写卡一组 40 条候选，单段式一批 18k 字窗口，
#: 实测（DeepSeek）单次输出 1.3k–2.6k token；给到 6000 留足余量，仍受
#: ``FEEDLING_GENESIS_LLM_MAX_TOKENS_PER_CALL``（默认 8000）封顶。
IMPORT_MAX_TOKENS = 6000


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def llm_complete(
    llm: GenesisLLMClient,
    *,
    user_id: str,
    job_id: str,
    runtime: provider_client.ProviderConfig,
) -> garden_import.Complete:
    """引擎的模型端口 → ``GenesisLLMClient``（并发槽、canary、心跳、调用台账照旧）。"""

    def complete(prompt: str, purpose: str) -> tuple[str, bool]:
        try:
            result = llm.complete(
                user_id=user_id,
                job_id=job_id,
                task_id="garden-import",
                runtime=runtime,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=IMPORT_MAX_TOKENS,
                timeout=float(_env_int("FEEDLING_GENESIS_LLM_TIMEOUT_SEC", 90)),
                idempotency_key=f"{job_id}:garden_import:{purpose}",
                temperature=0.2,
            )
        except Exception as exc:  # noqa: BLE001
            if provider_client.is_output_truncation_error(exc):
                # 200 + 空回复 + 停在输出上限：交给内核的截断重问，不当 provider 故障。
                return "", True
            raise
        return result.text, provider_client.is_token_limit_stop_reason(getattr(result, "stop_reason", ""))

    return complete


def memory_action(mutation: dict, *, store=None, source: str = "") -> dict | None:
    """一条写卡指令 → 明文 memory action（``service._memory_action_from_output`` 同一套
    归一化：来源 ``genesis_import``、类型收敛、日期规整）。不合格返回 None。

    ``source``：卡上记的来源，默认 ``genesis_import``；旧上传入口沿用 ``history_import``，
    后台按来源统计的口径不因换引擎而断。"""
    from genesis import service

    item = garden_import.mutation_item(mutation)
    try:
        action = service._memory_action_from_output(item, store=store, preserve_dates=True)
    except ValueError:
        return None
    if source:
        action["memory"]["source"] = source
    if item.get("retrieval_cues"):
        action["memory"]["retrieval_cues"] = list(item["retrieval_cues"])
    target = garden_import.supersede_target(mutation)
    if target:
        action["type"] = "memory.supersede"
        action["supersedes"] = target
    return action


def store_writer(
    store,
    api_key: str | None,
    *,
    runtime_token: str = "",
    source: str = "",
    execute: Callable[..., tuple[dict, int]] | None = None,
) -> garden_import.Write:
    """引擎的写库端口 → ``memory.actions`` 执行器（和 genesis 之前落卡是同一个入口）。"""
    run = execute or memory_actions._execute_memory_actions

    def _rows(actions: list[dict]) -> list:
        body, _status = run(store, api_key, actions, runtime_token=runtime_token)
        return list((body or {}).get("results") or [])

    def write(mutations: list[dict], _idempotency_key: str) -> list[str]:
        return garden_import.write_with_executor(
            mutations, build_action=lambda m: memory_action(m, store=store, source=source),
            execute=_rows)

    return write


def existing_cards(store, api_key: str | None, *, runtime_token: str = "",
                   job_id: str = "") -> list[dict]:
    """这个人现在可见的卡（id + 摘要 + 桶），给导入会话做跨批去重的「已有记忆索引」。

    读不到时返回 [] 并留一条内容无关的轨迹：导入照常进行，只是这次去重看不到旧卡
    （和切换前托管 add_memory 完全不传旧卡的行为一样，不会更差）。"""
    import memory_readside_core

    def _post(key, candidates, *, operation, payload=None):
        return memory_readside_core.post_enclave_readside(
            key, candidates, operation=operation, payload=payload,
            runtime_token=runtime_token or None)

    try:
        body = memory_readside_core.memory_index_core(
            store, api_key, {"limit": 0}, post_enclave=_post)
        items = body.get("items") if isinstance(body, dict) else None
        return garden_import.index_cards(items if isinstance(items, list) else [])
    except Exception as exc:  # noqa: BLE001
        try:
            debug_trace.trace_event(
                store, subsystem="genesis", type="genesis.garden_import.index_unavailable",
                actor="backend", status="warning", job_id=job_id, trace_id=job_id,
                summary="existing memory index unavailable for import dedup",
                detail={"error_class": type(exc).__name__})
        except Exception:  # noqa: BLE001
            pass
        return []


__all__ = ["IMPORT_MAX_TOKENS", "existing_cards", "llm_complete", "memory_action", "store_writer"]
