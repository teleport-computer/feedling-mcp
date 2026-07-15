# Hosted Runtime V2 — 子项目 C（action queue + short planner/executor/responder）Implementation Plan

> **STATUS: HISTORICAL / SUPERSEDED.** The staged planner/responder pipeline in
> this implementation plan has been retired in favor of the unified
> provider-native tool loop. This file is retained only as an implementation
> record.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 Plan B 的「无 planner、直接 responder」最小闭环，替换成
`coalesce → planner → executor(读并行/写串行) → (安全点 replan) → responder` 全流程，
并把 tool-call 步骤经 `agent_status_events` 脱敏推到前台 long-poll。

**Architecture:** V2 worker（`serve_worker.py`，跑在 runner-CVM enclave 上下文）每回合：
合并同用户未回复消息 → 便宜预取上下文（capabilities，无 LLM）→ planner 出 1–5 个短
JSON action（official 模型走用户 BYOK key 的结构化 JSON planner；弱模型走**零 LLM** 的确定性规则
planner）→ executor 用 `capabilities.registry.run_capability` 排空（读并行、写串行、每 action 出脱敏
status 事件）→ 安全点检测新消息触发 replan → responder 用**用户自己的** key 出 model-authored 回复 →
worker 封信封落加密 `chat_messages` + `pg_notify('chat', user)`。web 层 long-poll（`chat/routes_asgi.py`
的 asyncio waiter）被唤醒，`poll_core` 返回体带上 `agent_status_events` 游标。

**Tech Stack:** Python 3.11 / asyncio（`asyncio.to_thread` 把同步 `provider_client` / `capabilities`
调用桥到线程池）、Postgres（`agent_jobs` / `agent_action_queue` / `agent_status_events` / `runtime_state`，
Plan B 的 `0014` 迁移已建表）、`provider_client`（纯 Python，`reliable_chat_completion` +
`response_format`）、Pydantic-free 防御式 JSON 解析、pytest（`tests/`，`make_client` / `seed_user`）。

## Global Constraints

以下逐字取自 spec `2026-07-08-hosted-runtime-v2-abc-design.md`，每个 Task 的要求隐含包含本节：

- **§7.3 BYOK-only 硬不变量（写进实现 + 测试断言）**：API-key 用户回合内**所有** LLM 调用（planner +
  responder + 任何需要模型的 digest 生成）**一律用该用户自己的 provider key**。**不存在平台级 LLM key
  兜底。** 弱模型的兜底是「确定性、无-LLM 的规则 planner」，**不是**换平台 key 去跑。配套 key 隔离测试：
  注入一个「平台 key 探针」断言它**从不**被 planner/responder/digest 触达；弱模型 planner 路径断言**零**
  LLM 调用（`provider_client.chat_completion` 调用次数为 0）。
- **§7.5 no-filler 铁律**：只有 `final_response` 写聊天气泡；其余全是 status 事件。runtime **绝不**自造
  assistant 文本（`小克看到了…` 这类除非模型自己写的）。responder 返回空串 = 不落气泡。
- **§9 两条红线**：(1) **脱敏**——status 只带标签 + 粗计数（如「读取 3 张记忆卡」），绝不带解密原文/记忆/
  截屏/tool 原始输出；(2) **限频/合并**——并行读瞬间冒的多条合并为一条并限频，不刷屏。
- **测试放 `tests/`**，走 `tests/conftest.py` 的 `make_client` / `seed_user`；纯单元模块须加入
  `conftest.py::_PURE_UNIT` 白名单（无 Postgres 的 dev 机也能跑）。
- **跨模块调用一律 `from pkg import module` + `module.func()`**（CONTRIBUTING §3），禁止
  `from module import func` 拿裸函数——否则 monkeypatch 定义处对已绑定的裸函数无效。类型注解用途的
  类/常量 import（如 `from core.store import UserStore`）例外。模块别名带前缀避开局部变量遮蔽
  （`core_envelope` / `core_wake_bus` / `cap_registry` / `v2_coalesce`）。
- **单文件红线**（CONTRIBUTING §2）：超 **800 行** PR 须说明为何不拆；超 **1500 行**直接拆。C 的每个新模块
  都远低于 800 行，保持单一职责。
- **依赖方向**（CONTRIBUTING）：`model_api_runtime/v2` 可向下 import `capabilities` / `provider_client` /
  `core.store` / `db`；**不得**被 `chat` 反向 import。因此 status 读侧（`chat/poll_core.py`）走 `db.py`
  最底层 SQL 原语 `db.list_agent_status_events`（与 `jobs_store.list_status_events` 读同一张表），
  **不**从 `chat` upward import `model_api_runtime.v2`。

---

## 消费接口（Plan A + Plan B 产出——本 plan 不重实现，精确假定如下签名存在）

**Plan A — capabilities facade**（executor 靠它执行读/写 action）：

```python
# backend/capabilities/types.py
@dataclass
class CapabilityResult:
    ok: bool
    data: dict
    error: dict | None
    trace: dict
    warnings: list
    def to_dict(self) -> dict: ...     # {"ok":bool,"data":{...},"error":{...}|None,"trace":{...},"warnings":[...]}
def ok(data=None, *, trace=None, warnings=None) -> CapabilityResult: ...
def err(code, message, *, retryable=False, trace=None) -> CapabilityResult: ...

# backend/capabilities/registry.py
def run_capability(action_type: str, store, *, api_key=None, runtime_token=None, params=None) -> CapabilityResult: ...
CAPABILITIES: dict[str, Callable]   # action_type -> fn
# action_type 词表（Plan A registry 的 13 个能力，NO recent_chat_digest）:
#   identity_get, memory_index, memory_fetch,
#   perception_snapshot, perception_trend, perception_history,
#   screen_recent, screen_read, photo_recent, photo_read, chat_image_read,
#   memory_write, identity_patch
# （schedule/capture/sleep 等写类由 worker/后续 lane 处理，不在 A 的读能力 registry 内）
```

> **凭证是两套，别混**（B 落地据此）：
> - **enclave-auth**（`api_key` + `runtime_token`）：给 **capabilities** 用——enclave 解密转发（memory/screen/perception 读）。**executor 保持用这套。**
> - **BYOK provider_config**（含用户自己的 provider/model/base_url/**api_key**，JIT 单次解密一次）：给 **LLM** 用——planner 的 official 调用、responder 的回复。**planner/responder 用这套，不用 enclave-auth 的 api_key/runtime_token。**

**Plan B — jobs_store + worker 骨架**（C 在其上加 planner/executor）：

```python
# backend/model_api_runtime/v2/jobs_store.py
def claim_next_job(worker_id) -> dict | None
def mark_running(job_id) -> None ; mark_completed(job_id) -> None ; mark_failed(job_id, error) -> None
def append_status_event(user_id, kind, *, job_id=None, label=None, detail=None, seq=0) -> int
def list_status_events(user_id, *, after_id=0, limit=50) -> list[dict]   # 委托 db.list_agent_status_events
def get_runtime_state(user_id) -> dict ; upsert_runtime_state(user_id, patch) -> dict
def add_actions(job_id, user_id, actions: list[dict]) -> list[int]       # action:{type,payload,visible?,requires_model_authorship?}
def next_pending_action(job_id) -> dict | None
def mark_action_running(action_id) -> None
def mark_action_done(action_id, result: dict) -> None
def mark_action_failed(action_id, error) -> None
def mark_action_skipped(action_id) -> None
def invalidate_pending_actions(job_id, *, by_job_id: int) -> int
# backend/model_api_runtime/v2/worker.py
def run_worker_loop(...) -> None
MAX_READ_ACTION_PARALLELISM: int          # 单 job 内 executor 并行读上限
ENCLAVE_SEMAPHORE: "asyncio.Semaphore"    # 跨所有 job 共享的 enclave-bound 调用闸（§11 R3）
# 注入式 turn 依赖（B 因依赖方向把「解密 BYOK provider key」上移到 worker——那里才可 import hosted）：
class TurnDeps:
    def resolve_provider(user_id) -> "provider_client.ProviderConfig": ...   # JIT 单次解密 BYOK provider/model/base_url/api_key
def _read_messages(store, *, runtime_token, since_ts) -> list[dict]           # 在 enclave 内逐条解密近期消息，返回明文 {id,role,ts,content}
```

**provider_config（B 的 `TurnDeps.resolve_provider` 产出，本 plan 消费）：** `provider_client.ProviderConfig`
实例，含用户自己的 BYOK `provider/model/base_url/api_key`（JIT 解密一次，整回合留 worker 内存，绝不落库）。
planner 的 official LLM 调用、responder 的回复**一律用它**；`.provider`/`.base_url` 也用于派生 `is_official`。

**runtime_state 约定（`runtime_state` 只存非敏感 digest，§5——不放 provider 三元组、绝不放 key）：**

```python
runtime_state = {
  "last_replied_ts": 0.0,          # coalesce 游标：此 ts 之后的用户消息未回复
  "identity": {...},               # 非敏感 persona/identity digest（responder 用）
  "action_digest": {...},          # 上一回合 executor 折叠的非敏感计数（{action_type:{ok,count}}）
}
```

**Consumes（既有代码）：**

```python
# backend/provider_client.py
class ProviderConfig(provider, model, api_key, base_url="")     # frozen dataclass
def chat_completion(config, messages, *, max_tokens=700, temperature=0.7, timeout=60.0,
                    response_format=None, require_reply=True, include_reasoning=False) -> dict  # 返回 {"reply":str, "reasoning":str, "usage":dict, ...}
def reliable_chat_completion(*args, max_attempts=3, base_delay_sec=1.0, max_delay_sec=30.0, **kwargs) -> dict  # = chat_completion + transient 重试；blocking sleep，仅 worker 侧安全
#   response_format={"type":"json_object"}：openai-wire 原生透传；anthropic/gemini 只注入软指令 → planner 必须防御式解析 + 回退
# backend/agent_runtime/spawners.py
def _is_official_identity(provider, base_url) -> bool   # True=官方原生端点(anthropic/openai 默认 base_url 或空)；否则弱/杂牌（worker 用 provider_config.provider/base_url 派生 is_official）
# backend/model_api_runtime/prompts.py
def build_foreground_chat_messages(*, context_payload: dict, recent_messages: list[dict], user_message: str) -> list[dict]
# backend/core/store.py
def get_store(user_id) -> UserStore                 # .chat_messages: list[dict]（密文 envelope）; .append_chat(role, source, env, extra=None); .notify_chat_waiters()
# backend/core/envelope.py
def _build_shared_envelope_for_store(store, plaintext: bytes, *, item_id=None) -> tuple[dict|None, str]
# backend/core/wake_bus.py
def notify(channel: str, user_id: str = "") -> None
# backend/chat/poll_core.py
def build_response(*, messages, context, consumer_id, claim, timed_out) -> dict
def pending_messages(store, *, since, consumer_id, claim) -> list
# backend/db.py（Plan B 迁移 0014 建表后，本 plan 加一条只读 SQL 原语——见 Task 9）
```

---

## Task 概览

1. **coalesce.py** — 多消息合并成一回合（§7.1）。
2. **status_stream.py** — status 脱敏 + 并行读合并 + 限频（§9 两条红线）。
3. **planner.py（确定性规则 planner）** — 弱模型零 LLM 兜底（§7.3）。
4. **planner.py（official 结构化 JSON planner）** — 用户 BYOK key + 防御式解析 + 回退（§7.2/7.3）。
5. **executor.py** — `execute_plan` 读并行/写串行 + status 事件 + 结果折叠（§7.4）。
6. **invalidation.py** — 安全点 replan 状态机（§8）。
7. **responder.py** — model-authored 回复吃 action 结果 + persona（§7.5，BYOK-only）。
8. **worker.py 集成** — 用 coalesce→planner→executor→(replan)→responder 替换 B 最小闭环 + 封信封落库 + status wake。
9. **poll_core.py + db.py + routes_asgi.py** — long-poll 返回体带 `agent_status_events` 游标（§9）。

---

## Task 1: coalesce.py — 多消息合并成一回合（§7.1）

**Files:**
- Create: `backend/model_api_runtime/v2/coalesce.py`
- Test: `tests/test_v2_coalesce.py`

**Interfaces:**
- Consumes: 无（纯函数；输入是已解密的消息 dict 列表，解密由 worker 经 **B 的 `_read_messages`** 完成——`recent_chat_digest` 不是 capability）。
- Produces:
  - `last_replied_ts(messages: list[dict]) -> float`
  - `coalesce_pending(messages: list[dict], *, since_ts: float, decrypt=_plain_content) -> tuple[list[dict], float]`
    返回 `(coalesced, cursor)`；`coalesced` 每项 `{"id":str,"ts":float,"content":str}`，按时间升序、按 id 去重、丢空内容；`cursor` = 折入的最大用户 ts（无则 0.0）。

- [ ] **Step 1: 写失败测试**

`tests/test_v2_coalesce.py`:

```python
"""V2 coalesce (§7.1): fold every unanswered user message into ONE turn.

Pure-unit (no DB, no app). Add to conftest._PURE_UNIT so a no-Postgres dev box
runs it.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from model_api_runtime.v2 import coalesce as v2_coalesce  # noqa: E402


def _msg(mid, role, ts, content):
    return {"id": mid, "role": role, "ts": ts, "content": content}


def test_three_user_messages_coalesce_into_one_turn():
    # A, B, C sent after the last assistant reply → one coalesced turn.
    messages = [
        _msg("a0", "assistant", 100.0, "hi"),
        _msg("m1", "user", 101.0, "A"),
        _msg("m2", "user", 102.0, "B"),
        _msg("m3", "user", 103.0, "C"),
    ]
    since = v2_coalesce.last_replied_ts(messages)
    assert since == 100.0
    coalesced, cursor = v2_coalesce.coalesce_pending(messages, since_ts=since)
    assert [m["content"] for m in coalesced] == ["A", "B", "C"]
    assert cursor == 103.0


def test_already_replied_messages_are_excluded():
    messages = [
        _msg("m1", "user", 101.0, "old"),
        _msg("a1", "assistant", 102.0, "answered"),
        _msg("m2", "user", 103.0, "new"),
    ]
    since = v2_coalesce.last_replied_ts(messages)  # 102.0
    coalesced, cursor = v2_coalesce.coalesce_pending(messages, since_ts=since)
    assert [m["content"] for m in coalesced] == ["new"]
    assert cursor == 103.0


def test_dedupe_by_id_and_drop_empty_and_order():
    messages = [
        _msg("m2", "user", 102.0, "second"),
        _msg("m1", "user", 101.0, "first"),
        _msg("m2", "user", 102.0, "second"),   # dup id
        _msg("m3", "user", 103.0, "   "),       # empty after strip
    ]
    coalesced, cursor = v2_coalesce.coalesce_pending(messages, since_ts=0.0)
    assert [m["content"] for m in coalesced] == ["first", "second"]
    assert cursor == 102.0


def test_injected_decrypt_is_used():
    messages = [{"id": "m1", "role": "user", "ts": 5.0, "content": "CIPHER"}]
    coalesced, _ = v2_coalesce.coalesce_pending(
        messages, since_ts=0.0, decrypt=lambda m: "PLAIN")
    assert coalesced[0]["content"] == "PLAIN"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest ../tests/test_v2_coalesce.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'model_api_runtime.v2.coalesce'`

- [ ] **Step 3: 最小实现**

`backend/model_api_runtime/v2/coalesce.py`:

```python
"""V2 多消息 coalesce（spec §7.1）。

claim 时把该用户自「上次已回复游标」以来所有未回复用户消息并成一轮：single-flight 唯一索引
保证同 user 同 lane 至多一个活跃 job，A/B/C 三条消息只产生一个模型回合（不是三条独立回复）。

纯函数、无 DB、无 LLM。输入是**已解密**的消息 dict（明文由 worker 经 **B 的 `_read_messages`** 在
enclave 内解密取得），故本模块可注入 decrypt 便于测试与复用。
"""
from __future__ import annotations

from typing import Any, Callable

# 视为用户可见（触发回合）的角色。
_USER_ROLES = frozenset({"user", "human"})
# 视为模型作者（已回复）的角色——与既有 chat 约定一致（openclaw/assistant/agent）。
_ASSISTANT_ROLES = frozenset({"openclaw", "assistant", "agent"})


def _plain_content(m: dict[str, Any]) -> str:
    return str(m.get("content") or "")


def _ts(m: dict[str, Any]) -> float:
    try:
        return float(m.get("ts") or 0)
    except (TypeError, ValueError):
        return 0.0


def last_replied_ts(messages: list[dict[str, Any]]) -> float:
    """最近一条模型作者消息的 ts（无则 0.0）。此 ts 之后的用户消息都未回复，须并入下一回合。"""
    latest = 0.0
    for m in messages:
        if str(m.get("role") or "") in _ASSISTANT_ROLES:
            ts = _ts(m)
            if ts > latest:
                latest = ts
    return latest


def coalesce_pending(
    messages: list[dict[str, Any]],
    *,
    since_ts: float,
    decrypt: Callable[[dict[str, Any]], str] = _plain_content,
) -> tuple[list[dict[str, Any]], float]:
    """把 ts > since_ts 的未回复用户消息按时间升序并成一轮。

    返回 (coalesced, cursor)。cursor = 折入的最大用户 ts（0.0 表示无），调用方记录它，
    使后续回合不再重复折入同一批。按 id 去重、丢空内容。
    """
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    cursor = 0.0
    for m in sorted(messages, key=_ts):
        if str(m.get("role") or "") not in _USER_ROLES:
            continue
        ts = _ts(m)
        if ts <= since_ts:
            continue
        mid = str(m.get("id") or "")
        if mid and mid in seen:
            continue
        content = str(decrypt(m) or "").strip()
        if not content:
            continue
        if mid:
            seen.add(mid)
        out.append({"id": mid, "ts": ts, "content": content})
        if ts > cursor:
            cursor = ts
    return out, cursor
```

同时创建包占位 `backend/model_api_runtime/v2/__init__.py`（若 Plan B 未建）：空文件。

- [ ] **Step 4: 跑测试确认通过 + 登记 PURE_UNIT**

Run: `cd backend && python -m pytest ../tests/test_v2_coalesce.py -v`
Expected: PASS（4 passed）

在 `tests/conftest.py::_PURE_UNIT` 集合里加入 `"test_v2_coalesce.py",`（无 DB，dev 机也能跑）。

- [ ] **Step 5: Commit**

```bash
git add backend/model_api_runtime/v2/__init__.py backend/model_api_runtime/v2/coalesce.py tests/test_v2_coalesce.py tests/conftest.py
git commit -m "feat(v2): multi-message coalesce into one turn (spec §7.1)"
```

---

## Task 2: status_stream.py — status 脱敏 + 并行读合并 + 限频（§9）

**Files:**
- Create: `backend/model_api_runtime/v2/status_stream.py`
- Test: `tests/test_v2_status_stream.py`

**Interfaces:**
- Consumes: 无（纯函数 + 可注入时钟的 RateLimiter）。
- Produces:
  - `ACTION_STATUS_KIND: dict[str,str]`、`status_kind_for_action(action_type) -> str`
  - `redact_status(kind, *, count=None) -> dict`（`{"kind","label","detail"}`，只标签 + 粗计数）
  - `merge_parallel_reads(kinds: list[str]) -> list[dict]`（并行读 burst 合并为 ≤1 条）
  - `class RateLimiter(*, min_interval=0.5, now=time.monotonic)` → `.allow(kind) -> bool`

- [ ] **Step 1: 写失败测试**

`tests/test_v2_status_stream.py`:

```python
"""V2 status 脱敏 + 合并 + 限频（spec §9 两条红线）。Pure-unit。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from model_api_runtime.v2 import status_stream as ss  # noqa: E402


def test_status_kind_mapping():
    assert ss.status_kind_for_action("memory_fetch") == "reading_memory"
    assert ss.status_kind_for_action("perception_snapshot") == "reading_perception"
    assert ss.status_kind_for_action("memory_write") == "capturing_memory"
    assert ss.status_kind_for_action("final_response") == "writing_reply"
    assert ss.status_kind_for_action("totally_unknown") == "processing"


def test_redact_carries_only_label_and_coarse_count():
    ev = ss.redact_status("reading_memory", count=3)
    assert ev["kind"] == "reading_memory"
    assert ev["detail"] == {"count": 3}
    # NO plaintext / bodies / ids — only a label and the coarse count.
    assert set(ev["detail"].keys()) <= {"count"}
    assert "3" in ev["label"]


def test_merge_parallel_reads_collapses_burst_to_one():
    kinds = ["reading_memory", "reading_perception", "reading_screen"]
    merged = ss.merge_parallel_reads(kinds)
    assert len(merged) == 1
    assert merged[0]["kind"] == "reading_memory"
    assert merged[0]["detail"]["kinds"] == kinds


def test_merge_passes_non_mergeable_through_in_order():
    kinds = ["reading_memory", "capturing_memory", "reading_perception"]
    merged = ss.merge_parallel_reads(kinds)
    assert [e["kind"] for e in merged] == [
        "reading_memory", "capturing_memory", "reading_perception"]


def test_rate_limiter_drops_bursts_of_same_kind():
    clock = {"t": 0.0}
    rl = ss.RateLimiter(min_interval=1.0, now=lambda: clock["t"])
    assert rl.allow("reading_memory") is True
    assert rl.allow("reading_memory") is False   # too soon
    clock["t"] = 1.5
    assert rl.allow("reading_memory") is True     # window elapsed
    # a different kind is tracked independently
    assert rl.allow("writing_reply") is True
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest ../tests/test_v2_status_stream.py -v`
Expected: FAIL — `No module named 'model_api_runtime.v2.status_stream'`

- [ ] **Step 3: 最小实现**

`backend/model_api_runtime/v2/status_stream.py`:

```python
"""V2 tool-call 步骤 status 推送的脱敏 + 合并 + 限频（spec §9 两条红线）。

红线 1（脱敏）：status 只带标签 + 粗计数，绝不带解密原文/记忆/截屏/tool 原始输出。
红线 2（限频/合并）：并行读瞬间冒的多条合并为一条并限频，不刷屏。

纯函数 + 可注入时钟的 RateLimiter；无 DB、无 I/O。executor 用它构造 status 事件的 kind/label/detail。
"""
from __future__ import annotations

import time
from typing import Any, Callable

# action.type → status kind（§9）。读类塌缩到少数 reading_* kind；写类到其动词；responder 到 writing_reply。
ACTION_STATUS_KIND: dict[str, str] = {
    "identity_get": "reading_memory",
    "memory_index": "reading_memory",
    "memory_fetch": "reading_memory",
    "perception_snapshot": "reading_perception",
    "perception_trend": "reading_perception",
    "perception_history": "reading_perception",
    "screen_recent": "reading_screen",
    "screen_read": "reading_screen",
    "photo_recent": "reading_photo",
    "photo_read": "reading_photo",
    "chat_image_read": "retrieving_chat_image",
    "memory_write": "capturing_memory",
    "capture_memory": "capturing_memory",
    "identity_patch": "updating_identity",
    "schedule_followup": "scheduling",
    "schedule_wake": "scheduling",
    "cancel_wake": "scheduling",
    "final_response": "writing_reply",
    "sleep": "sleeping",
}

# 会被合并成一条的 kind（红线 2）：并行读几乎同时冒出，app 不能看到 6 行闪烁。
_MERGEABLE = frozenset({
    "reading_memory", "reading_perception", "reading_screen",
    "reading_photo", "retrieving_chat_image",
})

# 刻意含糊的人类标签（红线 1：标签 + 粗计数，绝无原文）。
_KIND_LABEL: dict[str, str] = {
    "processing": "处理中",
    "reading_memory": "读取上下文",
    "reading_perception": "读取感知",
    "reading_screen": "查看屏幕",
    "reading_photo": "查看照片",
    "retrieving_chat_image": "读取图片",
    "capturing_memory": "记录记忆",
    "updating_identity": "更新设定",
    "scheduling": "安排提醒",
    "writing_reply": "正在回复",
    "done": "完成",
    "sleeping": "休息",
}


def status_kind_for_action(action_type: str) -> str:
    return ACTION_STATUS_KIND.get(action_type, "processing")


def redact_status(kind: str, *, count: int | None = None) -> dict[str, Any]:
    """一条 status 事件的公开 payload——只标签 + 粗计数，绝不带明文/记忆体/截屏/tool 原始输出（红线 1）。"""
    label = _KIND_LABEL.get(kind, "处理中")
    detail: dict[str, Any] = {}
    if count is not None and count > 0:
        detail["count"] = int(count)
        label = f"{label}（{int(count)}）"
    return {"kind": kind, "label": label, "detail": detail}


def merge_parallel_reads(kinds: list[str]) -> list[dict[str, Any]]:
    """把一批并行读 kind 合并成 ≤1 条（红线 2）。非可合并 kind 原样保序穿过。"""
    out: list[dict[str, Any]] = []
    group: list[str] = []
    for k in kinds:
        if k in _MERGEABLE:
            group.append(k)
        else:
            if group:
                out.append(_merge_group(group))
                group = []
            out.append(redact_status(k))
    if group:
        out.append(_merge_group(group))
    return out


def _merge_group(kinds: list[str]) -> dict[str, Any]:
    # 合并后用统一「读取上下文」标签；detail.kinds 记录粗粒度子类，绝无原文。
    return {"kind": "reading_memory", "label": "读取上下文", "detail": {"kinds": list(kinds)}}


class RateLimiter:
    """丢弃同一 kind 内快于 min_interval 的 status 冒泡，避免并行/循环 action 刷屏（红线 2）。
    可注入时钟，测试确定性。"""

    def __init__(self, *, min_interval: float = 0.5, now: Callable[[], float] = time.monotonic):
        self._min = float(min_interval)
        self._now = now
        self._last: dict[str, float] = {}

    def allow(self, kind: str) -> bool:
        t = self._now()
        last = self._last.get(kind)
        if last is not None and (t - last) < self._min:
            return False
        self._last[kind] = t
        return True
```

- [ ] **Step 4: 跑测试确认通过 + 登记 PURE_UNIT**

Run: `cd backend && python -m pytest ../tests/test_v2_status_stream.py -v`
Expected: PASS（5 passed）

`tests/conftest.py::_PURE_UNIT` 加入 `"test_v2_status_stream.py",`。

- [ ] **Step 5: Commit**

```bash
git add backend/model_api_runtime/v2/status_stream.py tests/test_v2_status_stream.py tests/conftest.py
git commit -m "feat(v2): status redaction + parallel-read merge + rate limit (spec §9)"
```

---

## Task 3: planner.py — 确定性规则 planner（弱模型零 LLM，§7.3）

**Files:**
- Create: `backend/model_api_runtime/v2/planner.py`
- Test: `tests/test_v2_planner_rule.py`

**Interfaces:**
- Consumes: `provider_client`（Task 4 的 official 分支用；本 Task 的规则路径**不调用**任何 provider 函数）。`provider_config` 由 worker 注入（B 的 `TurnDeps.resolve_provider`），是 BYOK 的 `provider_client.ProviderConfig`。
- Produces:
  - `MAX_PLAN_ACTIONS = 5`；`validate_plan(raw) -> list[dict]`（词表白名单 + ≤5 + `final_response` 唯一且末位）
  - `rule_plan(*, coalesced_messages, memory_index, lane) -> list[dict]`（**零 LLM** 确定性规则）
  - `plan(store, *, provider_config, is_official, coalesced_messages, digest, memory_index, perception_summary, runtime_state, lane, reason) -> list[dict]`（去掉 api_key/runtime_token/driver；`is_official` 由 worker 用 `_is_official_identity(...)` 派生后传入；driver 内含于 `provider_config`。`is_official=False` → `rule_plan`；Task 4 补 official 分支）

- [ ] **Step 1: 写失败测试**

`tests/test_v2_planner_rule.py`:

```python
"""V2 确定性规则 planner（spec §7.3）：弱模型走零 LLM 规则。

守 §7.3 硬不变量：弱模型路径**从不**调用任何 provider（planner 零 LLM）。
注入一个会炸的 chat_completion 探针，断言 is_official=False 时它一次都不被触达。
Pure-unit。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import provider_client  # noqa: E402
from model_api_runtime.v2 import planner as v2_planner  # noqa: E402


def _explode(*a, **k):  # platform-key probe: any LLM call is a violation.
    raise AssertionError("weak-model planner must make ZERO LLM calls (§7.3)")


def test_validate_plan_whitelists_and_caps_and_orders():
    raw = {"plan": [
        {"type": "memory_fetch", "payload": {"ids": ["m1"]}},
        {"type": "not_a_real_action"},                       # dropped
        {"type": "final_response"},                          # forced last, once
        {"type": "perception_snapshot"},
        {"type": "screen_recent"},
        {"type": "photo_recent"},
        {"type": "memory_index"},                            # would exceed 5
    ]}
    steps = v2_planner.validate_plan(raw)
    assert len(steps) <= v2_planner.MAX_PLAN_ACTIONS
    assert steps[-1]["type"] == "final_response"
    assert [s["type"] for s in steps].count("final_response") == 1
    assert "not_a_real_action" not in [s["type"] for s in steps]


def test_validate_plan_flat_shape_folds_into_payload():
    raw = {"plan": [{"type": "memory_fetch", "ids": ["m1", "m2"]}]}
    steps = v2_planner.validate_plan(raw)
    assert steps[0] == {"type": "memory_fetch", "payload": {"ids": ["m1", "m2"]}}


def test_rule_plan_chat_lane_reads_then_answers_zero_llm(monkeypatch):
    monkeypatch.setattr(provider_client, "chat_completion", _explode)
    monkeypatch.setattr(provider_client, "reliable_chat_completion", _explode)
    steps = v2_planner.rule_plan(
        coalesced_messages=[{"content": "hello"}],
        memory_index={"items": [{"id": "mem_1"}, {"id": "mem_2"}]},
        lane="chat",
    )
    assert steps[-1]["type"] == "final_response"
    assert steps[0]["type"] == "memory_fetch"
    assert steps[0]["payload"]["ids"] == ["mem_1", "mem_2"]


def test_rule_plan_wake_with_no_input_sleeps():
    steps = v2_planner.rule_plan(coalesced_messages=[], memory_index={}, lane="manual_wake")
    assert steps == [{"type": "sleep", "payload": {"reason": "no_visible_input"}}]


def test_plan_weak_model_uses_rule_path_zero_llm(monkeypatch):
    monkeypatch.setattr(provider_client, "chat_completion", _explode)
    monkeypatch.setattr(provider_client, "reliable_chat_completion", _explode)
    weak_cfg = provider_client.ProviderConfig(
        provider="openrouter", model="x", api_key="sk-user-byok-real", base_url="")
    steps = v2_planner.plan(
        store=None, provider_config=weak_cfg,
        is_official=False, coalesced_messages=[{"content": "hi"}],
        digest={}, memory_index={"items": []}, perception_summary={},
        runtime_state={}, lane="chat", reason="",
    )
    assert steps[-1]["type"] == "final_response"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest ../tests/test_v2_planner_rule.py -v`
Expected: FAIL — `No module named 'model_api_runtime.v2.planner'`

- [ ] **Step 3: 最小实现**

`backend/model_api_runtime/v2/planner.py`（本 Task 只写词表 + `validate_plan` + `rule_plan` + `plan` 的规则分支；official 分支 Task 4 补）：

```python
"""V2 short planner（spec §7.2/7.3）。

official/可信模型 → 用**用户自己的 BYOK key** 的结构化 JSON planner（Task 4）；
弱/杂牌模型 → **确定性、零 LLM** 的规则 planner。**不存在平台级 LLM key 兜底**（§7.3 硬不变量）。
planner 只出 [{type,payload}]（≤5），非响应 action 不产生可见文本，需回复则含末位 final_response。
"""
from __future__ import annotations

import json
from typing import Any

import provider_client

# 封闭动作词表（§4.3，NO recent_chat_digest——它不是 capability，digest 在 worker 确定性构建）。
# 词表外一律丢弃。final_response 是唯一可见/作者 action。
_READ_ACTIONS = frozenset({
    "identity_get", "memory_index", "memory_fetch",
    "perception_snapshot", "perception_trend", "perception_history",
    "screen_recent", "screen_read", "photo_recent", "photo_read",
    "chat_image_read",
})
_WRITE_ACTIONS = frozenset({
    "memory_write", "identity_patch", "capture_memory",
    "schedule_followup", "schedule_wake", "cancel_wake", "sleep",
})

MAX_PLAN_ACTIONS = 5


def validate_plan(raw: Any) -> list[dict[str, Any]]:
    """把模型/规则输出收敛成安全、有序、≤5 的 action 列表。

    丢未知类型；至多一个 final_response 且置于末位；给 final_response 留位后截断。
    永不抛异常——垃圾 plan 退化为 []。
    """
    plan_in = raw.get("plan") if isinstance(raw, dict) else raw
    if not isinstance(plan_in, list):
        return []
    steps: list[dict[str, Any]] = []
    wants_reply = False
    for item in plan_in:
        if not isinstance(item, dict):
            continue
        t = str(item.get("type") or "").strip()
        if t == "final_response":
            wants_reply = True
            continue
        if t not in _READ_ACTIONS and t not in _WRITE_ACTIONS:
            continue
        payload = item.get("payload")
        if not isinstance(payload, dict):
            # 容忍扁平形状 {"type":"memory_fetch","ids":[...]}
            payload = {k: v for k, v in item.items() if k not in ("type", "payload")}
        steps.append({"type": t, "payload": payload})
    if wants_reply:
        steps = steps[: MAX_PLAN_ACTIONS - 1]
        steps.append({"type": "final_response", "payload": {}})
    else:
        steps = steps[:MAX_PLAN_ACTIONS]
    return steps


def rule_plan(*, coalesced_messages: list[dict], memory_index: dict, lane: str) -> list[dict[str, Any]]:
    """弱/杂牌模型的**确定性、零 LLM** planner（§7.3）。绝不调用任何 provider。

    chat lane 或有用户文本 → 便宜读近期记忆卡（若 index 里有）再 final_response；
    wake 且无可见输入、不值得回复 → sleep。parity 压在 responder（用户 key 出最终回复）。
    """
    has_user_text = any(str(m.get("content") or "").strip() for m in coalesced_messages)
    if lane == "chat" or has_user_text:
        steps: list[dict[str, Any]] = []
        items = (memory_index or {}).get("items") or []
        ids = [str(it.get("id")) for it in items[:3] if it.get("id")]
        if ids:
            steps.append({"type": "memory_fetch", "payload": {"ids": ids}})
        steps.append({"type": "final_response", "payload": {}})
        return steps
    return [{"type": "sleep", "payload": {"reason": "no_visible_input"}}]


def plan(
    store,
    *,
    provider_config: "provider_client.ProviderConfig",
    is_official: bool,
    coalesced_messages: list[dict],
    digest: dict,
    memory_index: dict,
    perception_summary: dict,
    runtime_state: dict,
    lane: str,
    reason: str,
) -> list[dict[str, Any]]:
    """回合 planner。is_official=False → 确定性规则（零 LLM）；True → 用户 BYOK provider_config 结构化 JSON planner。

    provider_config 是 worker JIT 解密的用户自己的 BYOK 凭证（含 provider/model/base_url/api_key）；driver
    内含其中，is_official 由 worker 用 _is_official_identity 派生后传入。"""
    if not is_official:
        return rule_plan(coalesced_messages=coalesced_messages, memory_index=memory_index, lane=lane)
    # official 分支在 Task 4 补入。
    return official_plan(
        provider_config=provider_config,
        coalesced_messages=coalesced_messages, digest=digest, memory_index=memory_index,
        perception_summary=perception_summary, runtime_state=runtime_state,
        lane=lane, reason=reason,
    )
```

> 注：本 Task 引用了 `official_plan`（Task 4 定义）。Task 3 的测试只走 `is_official=False` 分支与
> `validate_plan`/`rule_plan`，不触及 `official_plan`。若测试排序导致 `plan(is_official=True)` 被调，
> Task 4 完成前该分支会 `NameError`——这是刻意的：本 Task 只交付规则路径，official 路径由 Task 4 补齐并测。
> 为让模块可导入（`official_plan` 是运行期名字解析，非导入期），Task 3 无需先定义它。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest ../tests/test_v2_planner_rule.py -v`
Expected: PASS（5 passed）。关键：`test_plan_weak_model_uses_rule_path_zero_llm` 证明弱模型零 LLM。

`tests/conftest.py::_PURE_UNIT` 加入 `"test_v2_planner_rule.py",`。

- [ ] **Step 5: Commit**

```bash
git add backend/model_api_runtime/v2/planner.py tests/test_v2_planner_rule.py tests/conftest.py
git commit -m "feat(v2): deterministic zero-LLM rule planner for weak models (spec §7.3)"
```

---

## Task 4: planner.py — official 结构化 JSON planner（用户 BYOK key，§7.2）

**Files:**
- Modify: `backend/model_api_runtime/v2/planner.py`（追加 `official_plan` + `_parse_plan_json` + prompt/payload 构造）
- Test: `tests/test_v2_planner_official.py`

**Interfaces:**
- Consumes: `provider_client.reliable_chat_completion(config, messages, *, max_tokens, temperature, timeout, response_format, require_reply, max_attempts) -> dict`（返回 `{"reply": str, ...}`）；`provider_config`（BYOK `ProviderConfig`，worker 注入）直接作为 `config` 传入——**不**从 runtime_state 读 provider 三元组、**不**用 enclave-auth 的 api_key。
- Produces:
  - `official_plan(*, provider_config, coalesced_messages, digest, memory_index, perception_summary, runtime_state, lane, reason) -> list[dict]`
  - `_parse_plan_json(reply: str) -> Any`（剥 markdown fence + 取首尾花括号 + `json.loads`，永不抛）

- [ ] **Step 1: 写失败测试**

`tests/test_v2_planner_official.py`:

```python
"""V2 official 结构化 JSON planner（spec §7.2/7.3）。

守 §7.3 硬不变量：official planner 用**该用户 JIT 解密出的 BYOK key**，绝不用平台 key。
注入 chat_completion 探针断言传入的 config.api_key == 用户 key，且平台 key 哨兵从不出现。
解析失败/空 plan → 回退确定性规则（仍零额外 LLM，parity 压 responder）。Pure-unit。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import provider_client  # noqa: E402
from model_api_runtime.v2 import planner as v2_planner  # noqa: E402

_USER_KEY = "sk-user-byok-real"
_PLATFORM_SENTINEL = "sk-PLATFORM-NEVER-USE"


def _user_cfg():
    return provider_client.ProviderConfig(
        provider="openai", model="gpt-5", api_key=_USER_KEY, base_url="")


def _kwargs():
    return dict(
        provider_config=_user_cfg(),
        coalesced_messages=[{"content": "plan my week"}],
        digest={"recent": []}, memory_index={"items": [{"id": "m1"}]},
        perception_summary={"now": "morning"},
        runtime_state={"identity": {}},
        lane="chat", reason="user asked",
    )


def test_official_planner_uses_user_byok_key_never_platform(monkeypatch):
    seen = {}

    def _probe(config, messages, **kw):
        seen["api_key"] = config.api_key
        assert config.api_key != _PLATFORM_SENTINEL, "platform key must NEVER be reached (§7.3)"
        return {"reply": '{"plan":[{"type":"memory_fetch","payload":{"ids":["m1"]}},{"type":"final_response"}],"reason":"ok"}'}

    monkeypatch.setattr(provider_client, "chat_completion", _probe)  # reliable_ wraps chat_completion
    steps = v2_planner.official_plan(**_kwargs())
    assert seen["api_key"] == _USER_KEY
    assert steps[-1]["type"] == "final_response"
    assert steps[0] == {"type": "memory_fetch", "payload": {"ids": ["m1"]}}


def test_official_planner_parses_markdown_fenced_json(monkeypatch):
    def _probe(config, messages, **kw):
        return {"reply": "```json\n{\"plan\":[{\"type\":\"final_response\"}]}\n```"}
    monkeypatch.setattr(provider_client, "chat_completion", _probe)
    steps = v2_planner.official_plan(**_kwargs())
    assert steps == [{"type": "final_response", "payload": {}}]


def test_official_planner_falls_back_to_rule_on_garbage(monkeypatch):
    def _probe(config, messages, **kw):
        return {"reply": "I am not JSON at all, sorry."}
    monkeypatch.setattr(provider_client, "chat_completion", _probe)
    steps = v2_planner.official_plan(**_kwargs())
    # rule fallback for chat lane with memory_index items → memory_fetch + final_response
    assert steps[-1]["type"] == "final_response"


def test_official_planner_falls_back_to_rule_on_provider_error(monkeypatch):
    def _boom(config, messages, **kw):
        raise provider_client.ProviderError("down", status_code=503)
    monkeypatch.setattr(provider_client, "chat_completion", _boom)
    steps = v2_planner.official_plan(**_kwargs())
    assert steps[-1]["type"] == "final_response"   # never strands the turn
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest ../tests/test_v2_planner_official.py -v`
Expected: FAIL — `AttributeError: module 'model_api_runtime.v2.planner' has no attribute 'official_plan'`

- [ ] **Step 3: 最小实现（追加到 planner.py 末尾）**

```python
_PLANNER_SYSTEM = (
    "You are Feedling's turn planner. Output ONLY a JSON object "
    '{"plan":[{"type":"...","payload":{...}}],"reason":"..."}. '
    "Choose 1-5 short actions from this EXACT vocabulary: "
    "identity_get, memory_index, memory_fetch, perception_snapshot, perception_trend, "
    "perception_history, screen_recent, screen_read, photo_recent, photo_read, "
    "chat_image_read, memory_write, identity_patch, capture_memory, "
    "schedule_followup, schedule_wake, cancel_wake, sleep, final_response. "
    "Rules: prefer the SHORTEST plan; non-response actions must not produce visible text; "
    "do not mutate state without a strong reason; if a reply is warranted include "
    "final_response LAST; if a wake is not worth a visible reply use sleep. "
    "Never wrap the JSON in Markdown."
)


def _planner_user_payload(
    *, coalesced_messages, digest, memory_index, perception_summary, runtime_state, lane, reason
) -> dict:
    return {
        "lane": lane,
        "reason": reason,
        "messages": [{"content": str(m.get("content") or "")[:2000]} for m in coalesced_messages[-8:]],
        "recent_chat_digest": digest,
        "memory_index": memory_index,
        "perception_summary": perception_summary,
        "runtime_state": runtime_state or {},   # 只含非敏感 digest（无 provider 三元组、无 key）
    }


def _parse_plan_json(reply: str) -> Any:
    """从模型回复里抠出 JSON（剥 markdown fence + 取首尾花括号）。永不抛，失败返回 {}。

    provider_client 只对 openai-wire 原生透传 response_format；anthropic/gemini 仅注入软指令，
    故必须防御式解析。"""
    text = str(reply or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        nl = text.find("\n")
        if nl != -1 and text[:nl].strip().isalpha():  # 丢掉 ```json 这类语言标记行
            text = text[nl + 1:]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start: end + 1]
    try:
        return json.loads(text)
    except Exception:
        return {}


def official_plan(
    *,
    provider_config: "provider_client.ProviderConfig",
    coalesced_messages: list[dict],
    digest: dict,
    memory_index: dict,
    perception_summary: dict,
    runtime_state: dict,
    lane: str,
    reason: str,
) -> list[dict[str, Any]]:
    """轻量结构化 JSON planner，跑用户自己的 **BYOK provider_config**（§7.2/7.3）。

    解析失败 / 空 plan / provider 错误 → 回退确定性规则（不换平台 key，parity 压 responder）。
    """
    payload = _planner_user_payload(
        coalesced_messages=coalesced_messages, digest=digest,
        memory_index=memory_index, perception_summary=perception_summary,
        runtime_state=runtime_state, lane=lane, reason=reason,
    )
    messages = [
        {"role": "system", "content": _PLANNER_SYSTEM},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)[:12000]},
    ]
    try:
        result = provider_client.reliable_chat_completion(
            provider_config, messages,
            max_tokens=400, temperature=0.0, timeout=30.0,
            response_format={"type": "json_object"},
            require_reply=True, max_attempts=2,
        )
    except Exception:
        return rule_plan(coalesced_messages=coalesced_messages, memory_index=memory_index, lane=lane)
    steps = validate_plan(_parse_plan_json(result.get("reply") or ""))
    if not steps:
        return rule_plan(coalesced_messages=coalesced_messages, memory_index=memory_index, lane=lane)
    return steps
```

- [ ] **Step 4: 跑测试确认通过（含回归 Task 3）**

Run: `cd backend && python -m pytest ../tests/test_v2_planner_official.py ../tests/test_v2_planner_rule.py -v`
Expected: PASS（9 passed）。关键：`test_official_planner_uses_user_byok_key_never_platform` 守 §7.3。

`tests/conftest.py::_PURE_UNIT` 加入 `"test_v2_planner_official.py",`。

- [ ] **Step 5: Commit**

```bash
git add backend/model_api_runtime/v2/planner.py tests/test_v2_planner_official.py tests/conftest.py
git commit -m "feat(v2): official structured JSON planner on user BYOK key (spec §7.2/7.3)"
```

---

## Task 5: executor.py — 读并行/写串行排空（§7.4）

**Files:**
- Create: `backend/model_api_runtime/v2/executor.py`
- Test: `tests/test_v2_executor.py`

**Interfaces:**
- Consumes:
  - `capabilities.registry.run_capability(action_type, store, *, api_key, runtime_token, params) -> CapabilityResult`（`.to_dict()` → `{"ok","data","error",...}`）
  - `jobs_store.mark_action_running / mark_action_done / mark_action_failed / append_status_event`
  - `status_stream.status_kind_for_action / merge_parallel_reads / redact_status / RateLimiter`
- Produces:
  - `partition_plan(plan) -> tuple[list[dict], list[dict]]`（reads, writes；剔除 final_response）
  - `async execute_plan(store, job_id, *, api_key, runtime_token, plan, read_parallelism, enclave_sem) -> dict`
    返回 `{"action_results": {action_type: [result_dict,...]}, "action_digest": {action_type: {"ok","count"}}}`
    （`action_results` 含敏感 `data`，只在内存传给 responder；`action_digest` 非敏感，worker 落 runtime_state）

- [ ] **Step 1: 写失败测试**

`tests/test_v2_executor.py`:

```python
"""V2 executor（spec §7.4）：读并行、写串行、每 action 出脱敏 status、结果折叠。

用假 capabilities.registry + 假 jobs_store（monkeypatch 模块函数）驱动，纯 asyncio，无 DB。
断言：(1) 读在写之前；(2) 并行读受 read_parallelism 闸；(3) 每 action 落 status 事件；
(4) 敏感 data 只进 action_results，action_digest 只有粗计数。Pure-unit。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from capabilities import registry as cap_registry  # noqa: E402
from model_api_runtime.v2 import executor as v2_executor  # noqa: E402
from model_api_runtime.v2 import jobs_store  # noqa: E402


class _FakeResult:
    def __init__(self, ok, data):
        self._ok, self._data = ok, data

    def to_dict(self):
        return {"ok": self._ok, "data": self._data, "error": None, "trace": {}, "warnings": []}


def test_partition_excludes_final_response():
    plan = [
        {"type": "memory_fetch", "payload": {}},
        {"type": "memory_write", "payload": {}},
        {"type": "final_response", "payload": {}},
    ]
    reads, writes = v2_executor.partition_plan(plan)
    assert [r["type"] for r in reads] == ["memory_fetch"]
    assert [w["type"] for w in writes] == ["memory_write"]


def test_execute_plan_reads_parallel_writes_serial_and_status(monkeypatch):
    order = []
    status = []
    live = {"n": 0, "peak": 0}

    async def _fake_run(action_type, store, **kw):
        return None  # unused; run_capability is sync, patched below

    def _run_capability(action_type, store, *, api_key, runtime_token, params):
        order.append(action_type)
        return _FakeResult(True, {"secret_body": "PLAINTEXT-" + action_type})

    monkeypatch.setattr(cap_registry, "run_capability", _run_capability)
    monkeypatch.setattr(jobs_store, "mark_action_running", lambda *a, **k: None)
    monkeypatch.setattr(jobs_store, "mark_action_done", lambda *a, **k: None)
    monkeypatch.setattr(jobs_store, "mark_action_failed", lambda *a, **k: None)
    monkeypatch.setattr(
        jobs_store, "append_status_event",
        lambda user_id, kind, **k: status.append(kind) or 1)

    class _Store:
        user_id = "u1"

    plan = [
        {"type": "memory_fetch", "payload": {}, "_action_id": 1},
        {"type": "perception_snapshot", "payload": {}, "_action_id": 2},
        {"type": "memory_write", "payload": {}, "_action_id": 3},
        {"type": "final_response", "payload": {}},
    ]
    out = asyncio.run(v2_executor.execute_plan(
        _Store(), job_id=7, api_key="k", runtime_token="rt",
        plan=plan, read_parallelism=4, enclave_sem=asyncio.Semaphore(8)))

    # writes strictly after reads
    assert order.index("memory_write") > order.index("memory_fetch")
    assert order.index("memory_write") > order.index("perception_snapshot")
    # status carried the merged read line + the write line
    assert "reading_memory" in status and "capturing_memory" in status
    # sensitive body only in action_results, NEVER in action_digest
    assert out["action_results"]["memory_fetch"][0]["data"]["secret_body"] == "PLAINTEXT-memory_fetch"
    assert out["action_digest"]["memory_fetch"] == {"ok": 1, "count": 1}
    assert "secret_body" not in str(out["action_digest"])


def test_read_parallelism_is_bounded(monkeypatch):
    live = {"n": 0, "peak": 0}

    def _run_capability(action_type, store, *, api_key, runtime_token, params):
        live["n"] += 1
        live["peak"] = max(live["peak"], live["n"])
        # busy a moment so overlap is observable
        for _ in range(10000):
            pass
        live["n"] -= 1
        return _FakeResult(True, {})

    monkeypatch.setattr(cap_registry, "run_capability", _run_capability)
    monkeypatch.setattr(jobs_store, "mark_action_running", lambda *a, **k: None)
    monkeypatch.setattr(jobs_store, "mark_action_done", lambda *a, **k: None)
    monkeypatch.setattr(jobs_store, "mark_action_failed", lambda *a, **k: None)
    monkeypatch.setattr(jobs_store, "append_status_event", lambda *a, **k: 1)

    class _Store:
        user_id = "u1"

    plan = [{"type": "memory_fetch", "payload": {}, "_action_id": i} for i in range(6)]
    asyncio.run(v2_executor.execute_plan(
        _Store(), job_id=1, api_key="k", runtime_token="rt",
        plan=plan, read_parallelism=2, enclave_sem=asyncio.Semaphore(8)))
    assert live["peak"] <= 2
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest ../tests/test_v2_executor.py -v`
Expected: FAIL — `No module named 'model_api_runtime.v2.executor'`

- [ ] **Step 3: 最小实现**

`backend/model_api_runtime/v2/executor.py`:

```python
"""V2 executor（spec §7.4）：确定性排空 planner 出的 action。

读并行（read_parallelism 闸）、写串行 + 守卫。每 action 出脱敏 status 事件（§9）。
所有 capabilities 调用是同步的（可能内部 httpx 打 enclave），经 asyncio.to_thread 桥到线程池，
并被跨所有 job 共享的 enclave_sem 框住（§11 R3 治 enclave 串行化放大）。

结果拆两半：action_results 含敏感 data（内存传给 responder），action_digest 只粗计数（落 runtime_state）。
"""
from __future__ import annotations

import asyncio
from typing import Any

from capabilities import registry as cap_registry
from model_api_runtime.v2 import jobs_store
from model_api_runtime.v2 import status_stream

_READ_ACTIONS = frozenset({
    "identity_get", "memory_index", "memory_fetch",
    "perception_snapshot", "perception_trend", "perception_history",
    "screen_recent", "screen_read", "photo_recent", "photo_read",
    "chat_image_read",
})
# final_response 由 responder 作者，不在此执行。
_TERMINAL = frozenset({"final_response"})


def partition_plan(plan: list[dict]) -> tuple[list[dict], list[dict]]:
    """按序拆成 (reads, writes)；剔除 final_response（worker 交给 responder）。读并行、写串行。"""
    reads: list[dict] = []
    writes: list[dict] = []
    for step in plan:
        t = str(step.get("type") or "")
        if t in _TERMINAL:
            continue
        (reads if t in _READ_ACTIONS else writes).append(step)
    return reads, writes


async def _run_one(store, step, *, api_key, runtime_token, enclave_sem) -> tuple[str, dict]:
    action_id = step.get("_action_id")
    if action_id is not None:
        jobs_store.mark_action_running(action_id)
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
            jobs_store.mark_action_done(action_id, data)
        else:
            jobs_store.mark_action_failed(action_id, data.get("error") or {})
    return t, data


def _emit(limiter, job_id, user_id, events: list[dict]) -> None:
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
    """排空 plan。返回 {"action_results": {...}, "action_digest": {...}}。"""
    reads, writes = partition_plan(plan)
    results: dict[str, list[dict]] = {}
    limiter = status_stream.RateLimiter(min_interval=0.4)

    # 并行读 burst 合并成 ≤1 条 status（§9 红线 2）。
    if reads:
        _emit(limiter, job_id, store.user_id, status_stream.merge_parallel_reads(
            [status_stream.status_kind_for_action(str(s.get("type"))) for s in reads]))

    read_sem = asyncio.Semaphore(max(1, int(read_parallelism)))

    async def _guarded(step):
        async with read_sem:
            return await _run_one(store, step, api_key=api_key, runtime_token=runtime_token, enclave_sem=enclave_sem)

    read_out = await asyncio.gather(*[_guarded(s) for s in reads]) if reads else []
    for t, data in read_out:
        results.setdefault(t, []).append(data)

    # 写严格串行，每条自己一行 status。
    for step in writes:
        _emit(limiter, job_id, store.user_id, [status_stream.redact_status(
            status_stream.status_kind_for_action(str(step.get("type"))))])
        t, data = await _run_one(store, step, api_key=api_key, runtime_token=runtime_token, enclave_sem=enclave_sem)
        results.setdefault(t, []).append(data)

    return {"action_results": results, "action_digest": _digest(results)}


def _digest(results: dict[str, list[dict]]) -> dict[str, dict]:
    """非敏感粗计数——只 ok/count，绝无解密体（§5/§9）。"""
    out: dict[str, dict] = {}
    for action_type, runs in results.items():
        out[action_type] = {"ok": sum(1 for r in runs if r.get("ok")), "count": len(runs)}
    return out
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest ../tests/test_v2_executor.py -v`
Expected: PASS（3 passed）

`tests/conftest.py::_PURE_UNIT` 加入 `"test_v2_executor.py",`。
（注：本测试依赖 `capabilities.registry` 与 `model_api_runtime.v2.jobs_store` 可导入；Plan A/B 落地后
它们存在。若并行开发早于 A/B，测试 monkeypatch 的是模块函数——只需两模块**可导入**即可，函数体不必真跑。）

- [ ] **Step 5: Commit**

```bash
git add backend/model_api_runtime/v2/executor.py tests/test_v2_executor.py tests/conftest.py
git commit -m "feat(v2): executor drains plan reads-parallel/writes-serial with redacted status (spec §7.4)"
```

---

## Task 6: invalidation.py — 安全点 replan 状态机（§8）

**Files:**
- Create: `backend/model_api_runtime/v2/invalidation.py`
- Test: `tests/test_v2_invalidation.py`

**Interfaces:**
- Consumes: `coalesce.coalesce_pending`（Task 1）；`jobs_store.invalidate_pending_actions(job_id, *, by_job_id) -> int`。
- Produces:
  - `SAFE_POINTS = ("after_reads", "before_write", "before_final_response")`；`CONTINUE / REPLAN / FINISH` 常量
  - `new_visible_message_since(messages, *, cursor_ts) -> bool`
  - `evaluate(messages, *, safe_point, coalesced_cursor_ts, final_response_committed=False) -> str`
  - `invalidate(job_id, *, replan_job_id) -> int`

- [ ] **Step 1: 写失败测试**

`tests/test_v2_invalidation.py`:

```python
"""V2 replan/invalidation 安全点状态机（spec §8）。Pure-unit（invalidate() 测调用契约）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from model_api_runtime.v2 import invalidation as v2_inval  # noqa: E402
from model_api_runtime.v2 import jobs_store  # noqa: E402


def _u(mid, ts, content="hi"):
    return {"id": mid, "role": "user", "ts": ts, "content": content}


def test_new_visible_message_after_cursor_triggers_replan():
    # plan built after folding messages up to ts=102; a new user msg at 105 arrives.
    messages = [_u("m1", 101.0), _u("m2", 102.0), _u("m3", 105.0)]
    assert v2_inval.new_visible_message_since(messages, cursor_ts=102.0) is True
    decision = v2_inval.evaluate(messages, safe_point="after_reads", coalesced_cursor_ts=102.0)
    assert decision == v2_inval.REPLAN


def test_no_new_message_continues():
    messages = [_u("m1", 101.0), _u("m2", 102.0)]
    decision = v2_inval.evaluate(messages, safe_point="before_write", coalesced_cursor_ts=102.0)
    assert decision == v2_inval.CONTINUE


def test_committed_final_response_finishes_not_replans():
    messages = [_u("m1", 101.0), _u("m3", 105.0)]  # new msg exists
    decision = v2_inval.evaluate(
        messages, safe_point="before_final_response",
        coalesced_cursor_ts=102.0, final_response_committed=True)
    assert decision == v2_inval.FINISH   # default: never abort a useful in-flight reply


def test_unknown_safe_point_raises():
    try:
        v2_inval.evaluate([], safe_point="not_a_point", coalesced_cursor_ts=0.0)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_invalidate_delegates_to_jobs_store(monkeypatch):
    calls = {}
    monkeypatch.setattr(
        jobs_store, "invalidate_pending_actions",
        lambda job_id, *, by_job_id: calls.update(job_id=job_id, by=by_job_id) or 3)
    n = v2_inval.invalidate(42, replan_job_id=42)
    assert n == 3
    assert calls == {"job_id": 42, "by": 42}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest ../tests/test_v2_invalidation.py -v`
Expected: FAIL — `No module named 'model_api_runtime.v2.invalidation'`

- [ ] **Step 3: 最小实现**

`backend/model_api_runtime/v2/invalidation.py`:

```python
"""V2 replan / invalidation 安全点状态机（spec §8）。

安全点：读批完成后 / 写操作前 / final_response 前。运行中新可见用户消息到达 → 现有 plan 的 pending
actions 置 invalidated，在下一个安全点带 A+B+C 上下文重规划。single-flight 唯一索引保证同 user 同 lane
至多一个活跃 job，故这是**同一 job 内**的重规划（replan_job_id = 该 job 自身）。

final_response 流式中被打断 → 默认写完（已产出有用回答则保留，§8 默认 finish）。
"""
from __future__ import annotations

from typing import Any

from model_api_runtime.v2 import coalesce as v2_coalesce
from model_api_runtime.v2 import jobs_store

# 三个允许重规划的安全点（§8）。在别处重规划会撕裂半应用的写。
SAFE_POINTS = ("after_reads", "before_write", "before_final_response")

CONTINUE = "continue"
REPLAN = "replan"
FINISH = "finish"


def new_visible_message_since(messages: list[dict[str, Any]], *, cursor_ts: float) -> bool:
    """cursor_ts（当前 plan 折入的最大用户 ts）之后是否又来了新用户可见消息？= invalidation 触发条件。"""
    _, new_cursor = v2_coalesce.coalesce_pending(messages, since_ts=cursor_ts)
    return new_cursor > cursor_ts


def evaluate(
    messages: list[dict[str, Any]],
    *,
    safe_point: str,
    coalesced_cursor_ts: float,
    final_response_committed: bool = False,
) -> str:
    """安全点上的状态机裁决（§8）。

    - final_response 已提交/流式中 → FINISH（默认不打断有用回答）。
    - 自 plan 构建以来有新可见用户消息 → REPLAN（在此安全点带 A+B+C 重规划）。
    - 否则 → CONTINUE。
    """
    if safe_point not in SAFE_POINTS:
        raise ValueError(f"unknown safe point: {safe_point}")
    if safe_point == "before_final_response" and final_response_committed:
        return FINISH
    if new_visible_message_since(messages, cursor_ts=coalesced_cursor_ts):
        return REPLAN
    return CONTINUE


def invalidate(job_id: int, *, replan_job_id: int) -> int:
    """把当前 plan 的 pending actions 置 invalidated，worker 跳过并重规划（§8）。返回置无效条数。
    within-job replan 时 replan_job_id = job_id 自身（记录哪个 job 取代了它们）。"""
    return jobs_store.invalidate_pending_actions(job_id, by_job_id=replan_job_id)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest ../tests/test_v2_invalidation.py -v`
Expected: PASS（5 passed）。关键：`test_new_visible_message_after_cursor_triggers_replan`（并发新消息触发 invalidation）+ `test_unknown_safe_point_raises`（replan 只在安全点发生）。

`tests/conftest.py::_PURE_UNIT` 加入 `"test_v2_invalidation.py",`。

- [ ] **Step 5: Commit**

```bash
git add backend/model_api_runtime/v2/invalidation.py tests/test_v2_invalidation.py tests/conftest.py
git commit -m "feat(v2): safe-point replan/invalidation state machine (spec §8)"
```

---

## Task 7: responder.py — model-authored 回复吃 action 结果（§7.5，BYOK-only）

**Files:**
- Create/Modify: `backend/model_api_runtime/v2/responder.py`（Plan B 有则扩展 `respond` 签名加 `action_results`；无则全量创建）
- Test: `tests/test_v2_responder.py`

**Interfaces:**
- Consumes:
  - `provider_client.reliable_chat_completion(config, messages, *, ...) -> {"reply": str, ...}`（`config` = 注入的 BYOK `provider_config`）
  - `model_api_runtime.prompts.build_foreground_chat_messages(*, context_payload, recent_messages, user_message) -> list[dict]`
- Produces（对齐 B 的签名）:
  - `respond(*, provider_config, coalesced_messages, runtime_state, action_results) -> str`（BYOK-only；无 store/api_key/runtime_token；返回回复文本，空串=不落气泡）

- [ ] **Step 1: 写失败测试**

`tests/test_v2_responder.py`:

```python
"""V2 responder（spec §7.5）：model-authored 回复，吃 executor action 结果 + persona。

守 §7.3：responder 也用**用户自己的** BYOK key，平台 key 哨兵从不出现。
守 §7.5：no-filler——responder 返回文本由 worker 落气泡；这里不自造 filler。Pure-unit。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import provider_client  # noqa: E402
from model_api_runtime.v2 import responder as v2_responder  # noqa: E402

_USER_KEY = "sk-user-byok-real"
_PLATFORM_SENTINEL = "sk-PLATFORM-NEVER-USE"


def _user_cfg():
    return provider_client.ProviderConfig(
        provider="anthropic", model="claude-x", api_key=_USER_KEY, base_url="")


def _rs():
    return {"identity": {"agent_name": "小克"}}   # non-sensitive digest only


def test_responder_uses_user_key_and_folds_action_results(monkeypatch):
    seen = {}

    def _probe(config, messages, **kw):
        seen["api_key"] = config.api_key
        assert config.api_key != _PLATFORM_SENTINEL, "platform key must NEVER be reached (§7.3)"
        # the action data must be visible to the model (it authors the reply)
        blob = "".join(str(m.get("content") or "") for m in messages)
        seen["saw_memory"] = "REMEMBERED-FACT" in blob
        return {"reply": "  你昨天提过这件事。  "}

    monkeypatch.setattr(provider_client, "chat_completion", _probe)
    reply = v2_responder.respond(
        provider_config=_user_cfg(),
        coalesced_messages=[{"content": "still on for tmr?"}],
        runtime_state=_rs(),
        action_results={"memory_fetch": [{"ok": True, "data": {"cards": ["REMEMBERED-FACT"]}}]},
    )
    assert seen["api_key"] == _USER_KEY
    assert seen["saw_memory"] is True
    assert reply == "你昨天提过这件事。"   # trimmed, model-authored


def test_responder_empty_reply_returns_empty_string(monkeypatch):
    monkeypatch.setattr(provider_client, "chat_completion", lambda c, m, **k: {"reply": "   "})
    reply = v2_responder.respond(
        provider_config=_user_cfg(),
        coalesced_messages=[{"content": "hi"}], runtime_state=_rs(), action_results=None)
    assert reply == ""
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest ../tests/test_v2_responder.py -v`
Expected: FAIL — `No module named 'model_api_runtime.v2.responder'`（或 B 版 `respond` 无 `action_results` kwarg → TypeError）

- [ ] **Step 3: 最小实现**

`backend/model_api_runtime/v2/responder.py`:

```python
"""V2 responder（spec §7.5）：用注入的**用户自己的 BYOK provider_config** 出 model-authored 回复。

no-filler 铁律：只有 responder 的回复文本会被 worker 落进气泡；runtime 绝不自造 assistant 文本。
BYOK-only（§7.3）：绝无平台级 LLM key 兜底，provider_config 是 worker JIT 解密的用户凭证。复用既有
model_api 线的 prompt（build_foreground_chat_messages），不另起炉灶——只把 executor 的 action 结果折进
context_payload。
"""
from __future__ import annotations

from typing import Any

import provider_client
from model_api_runtime import prompts as model_api_prompts

_RESPONDER_MAX_TOKENS = 700


def _context_from_actions(action_results: dict | None) -> dict[str, Any]:
    """把 executor 的 action 结果折进 responder 的 runtime context。只取 capability 的 data
    （已由 A 层 cap/redact），responder 是模型、可看用户自己的记忆/感知；status 事件永远看不到（§9）。"""
    ctx: dict[str, Any] = {}
    for action_type, runs in (action_results or {}).items():
        payloads = [r.get("data") for r in runs if r.get("ok") and r.get("data")]
        if payloads:
            ctx[action_type] = payloads if len(payloads) > 1 else payloads[0]
    return ctx


def _recent_messages(coalesced_messages: list[dict]) -> list[dict[str, Any]]:
    return [{"role": "user", "content": str(m.get("content") or "")} for m in coalesced_messages]


def respond(
    *,
    provider_config: "provider_client.ProviderConfig",
    coalesced_messages: list[dict],
    runtime_state: dict,
    action_results: dict | None = None,
) -> str:
    """model-authored 回复文本。空串 = 不落气泡（如 sleep / 模型无话可说）。用注入的 BYOK provider_config。"""
    context_payload = {
        "identity": (runtime_state or {}).get("identity") or {},
        "action_context": _context_from_actions(action_results),
        "action_digest": (runtime_state or {}).get("action_digest") or {},
    }
    last_user = str(coalesced_messages[-1].get("content") or "") if coalesced_messages else ""
    messages = model_api_prompts.build_foreground_chat_messages(
        context_payload=context_payload,
        recent_messages=_recent_messages(coalesced_messages),
        user_message=last_user,
    )
    result = provider_client.reliable_chat_completion(
        provider_config, messages,
        max_tokens=_RESPONDER_MAX_TOKENS, temperature=0.7, timeout=60.0,
        require_reply=True, max_attempts=3,
    )
    return str(result.get("reply") or "").strip()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest ../tests/test_v2_responder.py -v`
Expected: PASS（2 passed）

`tests/conftest.py::_PURE_UNIT` 加入 `"test_v2_responder.py",`（依赖 `model_api_runtime.prompts` 可导入，无 DB）。

- [ ] **Step 5: Commit**

```bash
git add backend/model_api_runtime/v2/responder.py tests/test_v2_responder.py tests/conftest.py
git commit -m "feat(v2): responder folds action results, user BYOK key only (spec §7.5/7.3)"
```

---

## Task 8: worker.py 集成 — 用 planner→executor→(replan)→responder 替换 B 最小闭环

**Files:**
- Modify: `backend/model_api_runtime/v2/worker.py`（把 B 的「直接 responder」替换成全流程 `process_job`）
- Test: `tests/test_v2_worker_process_job.py`

**Interfaces:**
- Consumes: `coalesce`、`planner`、`executor`、`invalidation`、`responder`、`jobs_store`、`core.store.get_store`、`core.envelope._build_shared_envelope_for_store`、`core.wake_bus.notify`、`agent_runtime.spawners._is_official_identity`、`ENCLAVE_SEMAPHORE`、`MAX_READ_ACTION_PARALLELISM`；**B 的** `TurnDeps.resolve_provider(user_id) -> provider_config`（BYOK）与 `_read_messages(store, *, runtime_token, since_ts) -> list[dict]`（enclave 内解密明文消息）。
- **两套凭证**：`provider_config`（BYOK）只喂 planner/responder 的 LLM 调用；`api_key`+`runtime_token`（enclave-auth）喂 executor 的 capability 调用与 `_read_messages`。
- Produces:
  - `async process_job(job, *, provider_config, api_key, runtime_token, enclave_sem=None, read_parallelism=None, replan_budget=2) -> None`
  - `_write_encrypted_reply(store, text) -> dict | None`
  - `_is_official_for(provider_config) -> bool`（`_is_official_identity(provider_config.provider, provider_config.base_url)`）
  - `_coalesce_inputs(store, runtime_token, since_ts) -> tuple[list[dict], float]`（经 B 的 `_read_messages` 取明文再 `coalesce_pending`）

- [ ] **Step 1: 写失败测试**

`tests/test_v2_worker_process_job.py`:

```python
"""V2 worker 全流程集成（spec §13 第 5-8 步）：coalesce→planner→executor→(replan)→responder。

用注入式测法（monkeypatch 模块函数，照 test_agent_runtime_supervisor 的注入套路）驱动 process_job，
断言：(1) 正常回合按序调 planner/executor/responder 并封信封落库；
(2) 安全点检测到并发新消息 → 触发 invalidation 并重规划一次。DB-backed（seed_user + real store）。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import conftest  # noqa: E402  (seed_user)
import provider_client  # noqa: E402
from core import store as core_store  # noqa: E402
from model_api_runtime.v2 import worker as v2_worker  # noqa: E402
from model_api_runtime.v2 import planner as v2_planner  # noqa: E402
from model_api_runtime.v2 import executor as v2_executor  # noqa: E402
from model_api_runtime.v2 import responder as v2_responder  # noqa: E402
from model_api_runtime.v2 import jobs_store  # noqa: E402
from model_api_runtime.v2 import invalidation as v2_inval  # noqa: E402
from capabilities import registry as cap_registry  # noqa: E402

_BYOK = provider_client.ProviderConfig(
    provider="anthropic", model="claude-x", api_key="sk-user-byok", base_url="")


def _seed(uid):
    conftest.seed_user(uid, provider="anthropic")
    store = core_store.get_store(uid)
    return store


def _patch_common(monkeypatch, plan_steps):
    monkeypatch.setattr(v2_planner, "plan", lambda *a, **k: list(plan_steps))
    monkeypatch.setattr(
        cap_registry, "run_capability",
        lambda action_type, store, **k: _FakeResult({"messages": [], "items": []}))
    monkeypatch.setattr(jobs_store, "get_runtime_state", lambda uid: {"last_replied_ts": 0.0})
    monkeypatch.setattr(jobs_store, "upsert_runtime_state", lambda uid, patch: patch)
    monkeypatch.setattr(jobs_store, "add_actions", lambda job_id, uid, actions: list(range(len(actions))))
    monkeypatch.setattr(jobs_store, "append_status_event", lambda *a, **k: 1)
    monkeypatch.setattr(jobs_store, "mark_action_running", lambda *a, **k: None)
    monkeypatch.setattr(jobs_store, "mark_action_done", lambda *a, **k: None)
    monkeypatch.setattr(jobs_store, "mark_action_failed", lambda *a, **k: None)
    monkeypatch.setattr(jobs_store, "mark_completed", lambda *a, **k: None)


class _FakeResult:
    def __init__(self, data):
        self._data = data

    def to_dict(self):
        return {"ok": True, "data": self._data, "error": None, "trace": {}, "warnings": []}


def test_process_job_end_to_end_writes_reply(client, backend_env, monkeypatch):
    store = _seed("usr_worker1")
    _patch_common(monkeypatch, [
        {"type": "memory_fetch", "payload": {"ids": ["m1"]}},
        {"type": "final_response", "payload": {}},
    ])
    # a pending user message exists (coalesce reads plaintext via B's _read_messages)
    monkeypatch.setattr(v2_worker, "_coalesce_inputs", lambda store, rt, since: (
        [{"id": "m1", "ts": 10.0, "content": "hello"}], 10.0))
    monkeypatch.setattr(v2_responder, "respond", lambda *a, **k: "MODEL REPLY")
    written = {}
    monkeypatch.setattr(v2_worker, "_write_encrypted_reply",
                        lambda store, text: written.update(text=text) or {"id": "r1"})

    job = {"id": 1, "user_id": "usr_worker1", "lane": "chat", "reason": ""}
    asyncio.run(v2_worker.process_job(
        job, provider_config=_BYOK, api_key="enclave-auth-key", runtime_token="rt"))
    assert written["text"] == "MODEL REPLY"


def test_process_job_replans_on_concurrent_new_message(client, backend_env, monkeypatch):
    _seed("usr_worker2")
    calls = {"plan": 0, "invalidate": 0}

    def _count_plan(*a, **k):
        calls["plan"] += 1
        return [{"type": "final_response", "payload": {}}]

    monkeypatch.setattr(v2_planner, "plan", _count_plan)
    _patch_common(monkeypatch, [{"type": "final_response", "payload": {}}])
    monkeypatch.setattr(v2_planner, "plan", _count_plan)  # re-assert after _patch_common
    monkeypatch.setattr(v2_responder, "respond", lambda *a, **k: "R")
    monkeypatch.setattr(v2_worker, "_write_encrypted_reply", lambda store, text: {"id": "r"})

    # first coalesce sees msg up to ts=10; the safe-point check sees a NEW msg at 20 once,
    # then no more → exactly one replan.
    seq = iter([
        ([{"id": "m1", "ts": 10.0, "content": "first"}], 10.0),   # initial coalesce
        ([{"id": "m1", "ts": 10.0, "content": "first"},
          {"id": "m2", "ts": 20.0, "content": "second"}], 20.0),  # replan coalesce
    ])
    monkeypatch.setattr(v2_worker, "_coalesce_inputs", lambda store, rt, since: next(seq))

    # evaluate: REPLAN first time, CONTINUE second time.
    decisions = iter([v2_inval.REPLAN, v2_inval.CONTINUE])
    monkeypatch.setattr(v2_inval, "evaluate", lambda *a, **k: next(decisions))
    monkeypatch.setattr(v2_inval, "invalidate",
                        lambda job_id, *, replan_job_id: calls.__setitem__("invalidate", calls["invalidate"] + 1) or 0)

    job = {"id": 2, "user_id": "usr_worker2", "lane": "chat", "reason": ""}
    asyncio.run(v2_worker.process_job(
        job, provider_config=_BYOK, api_key="enclave-auth-key", runtime_token="rt"))
    assert calls["plan"] == 2         # planned once, replanned once
    assert calls["invalidate"] == 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest ../tests/test_v2_worker_process_job.py -v`
Expected: FAIL — `AttributeError: module 'model_api_runtime.v2.worker' has no attribute 'process_job'`

- [ ] **Step 3: 最小实现（追加/替换 worker.py 的 job 处理）**

在 `backend/model_api_runtime/v2/worker.py` 里加入下述内容（保留 B 的 `run_worker_loop` / 常量；把
loop 里对单 job 的处理改为 `await process_job(job, provider_config=deps.resolve_provider(uid), api_key=..., runtime_token=...)`，其中 `provider_config` 是 B 的 `TurnDeps.resolve_provider` JIT 解密的 BYOK 凭证）：

```python
import asyncio

from capabilities import registry as cap_registry
from core import envelope as core_envelope
from core import store as core_store
from core import wake_bus as core_wake_bus
from agent_runtime import spawners
from model_api_runtime.v2 import coalesce as v2_coalesce
from model_api_runtime.v2 import executor as v2_executor
from model_api_runtime.v2 import invalidation as v2_inval
from model_api_runtime.v2 import jobs_store
from model_api_runtime.v2 import planner as v2_planner
from model_api_runtime.v2 import responder as v2_responder


def _cap_data(store, action_type, *, api_key, runtime_token, params=None) -> dict:
    """便宜预取一个 capability 的 data（无 LLM，用 enclave-auth 凭证）。失败退化为 {}——planner 有 index
    更好、没有也能规划。"""
    try:
        result = cap_registry.run_capability(
            action_type, store, api_key=api_key, runtime_token=runtime_token, params=params or {})
        data = result.to_dict()
        return data.get("data") or {} if data.get("ok") else {}
    except Exception:
        return {}


def _coalesce_inputs(store, runtime_token, since_ts):
    """经 **B 的 `_read_messages`** 取 enclave 内解密的**明文**近期消息，再按 §7.1 合并。

    `_read_messages` 逐条在 enclave 内解密（worker 不 shell out、不碰 K_user）；digest 在 worker 层
    确定性构建（§7.2 无 LLM）。返回 (coalesced, cursor)。"""
    messages = _read_messages(store, runtime_token=runtime_token, since_ts=since_ts)
    return v2_coalesce.coalesce_pending(messages, since_ts=since_ts)


def _is_official_for(provider_config) -> bool:
    """从注入的 BYOK provider_config 派生 official 判定（driver/endpoint 都内含其中）。"""
    return spawners._is_official_identity(
        str(getattr(provider_config, "provider", "") or ""),
        str(getattr(provider_config, "base_url", "") or ""))


def _write_encrypted_reply(store, text: str) -> dict | None:
    """把 model-authored 回复封 shared 信封落**加密** chat_messages，并唤醒本地 chat waiter。

    照既有 model_api 线的写法（hosted/turn.py::_append_model_api_runtime_followup_message）：
    服务器只持有密文（E2E）。"""
    env, err = core_envelope._build_shared_envelope_for_store(store, text.encode("utf-8"))
    if env is None:
        return None
    row = store.append_chat("openclaw", "model_api", env)
    store.notify_chat_waiters()
    return row


async def process_job(
    job,
    *,
    provider_config,                           # BYOK ProviderConfig（planner/responder 的 LLM 调用）
    api_key: str,                              # enclave-auth（executor 的 capability + _read_messages）
    runtime_token: str,                        # enclave-auth
    enclave_sem: "asyncio.Semaphore" = None,   # 默认取模块 ENCLAVE_SEMAPHORE
    read_parallelism: int = None,              # 默认取模块 MAX_READ_ACTION_PARALLELISM
    replan_budget: int = 2,
) -> None:
    """一回合：coalesce → planner → executor → (安全点 replan) → responder（spec §7/§8）。

    两套凭证不混：provider_config=BYOK 只喂 LLM；api_key/runtime_token=enclave-auth 喂 capability。
    """
    if enclave_sem is None:
        enclave_sem = ENCLAVE_SEMAPHORE
    if read_parallelism is None:
        read_parallelism = MAX_READ_ACTION_PARALLELISM

    user_id = job["user_id"]
    lane = job.get("lane") or "chat"
    store = core_store.get_store(user_id)
    runtime_state = jobs_store.get_runtime_state(user_id)   # 只含非敏感 digest（无 provider、无 key）
    is_official = _is_official_for(provider_config)

    jobs_store.append_status_event(user_id, "processing", job_id=job["id"], label="处理中")

    since = float(runtime_state.get("last_replied_ts") or 0)
    coalesced, cursor = _coalesce_inputs(store, runtime_token, since)
    if not coalesced and lane == "chat":
        # 无未回复消息（已被别的回合吃掉）——干净收尾，不落 filler。
        jobs_store.mark_completed(job["id"])
        return

    while True:
        # 便宜预取（无 LLM，enclave-auth 凭证）：memory index + 感知摘要 + 确定性 digest。
        memory_index = _cap_data(store, "memory_index", api_key=api_key, runtime_token=runtime_token)
        perception_summary = _cap_data(store, "perception_snapshot", api_key=api_key, runtime_token=runtime_token)
        digest = {"messages": [{"content": m["content"][:400]} for m in coalesced[-6:]]}

        # planner 走 BYOK provider_config（reliable_chat_completion 内含 blocking sleep → to_thread）。
        steps = await asyncio.to_thread(
            v2_planner.plan, store,
            provider_config=provider_config, is_official=is_official,
            coalesced_messages=coalesced, digest=digest, memory_index=memory_index,
            perception_summary=perception_summary, runtime_state=runtime_state,
            lane=lane, reason=job.get("reason") or "")

        executable = [s for s in steps if s["type"] != "final_response"]
        action_ids = jobs_store.add_actions(
            job["id"], user_id,
            [{"type": s["type"], "payload": s["payload"]} for s in executable])
        for s, aid in zip(executable, action_ids):
            s["_action_id"] = aid

        # executor 用 enclave-auth 凭证（与 BYOK 独立的两套）。
        action_state = await v2_executor.execute_plan(
            store, job["id"], api_key=api_key, runtime_token=runtime_token,
            plan=steps, read_parallelism=read_parallelism, enclave_sem=enclave_sem)

        # 安全点：final_response 前。并发新消息 → replan（预算内）。
        decision = v2_inval.evaluate(
            store.chat_messages, safe_point="before_final_response", coalesced_cursor_ts=cursor)
        if decision == v2_inval.REPLAN and replan_budget > 0:
            v2_inval.invalidate(job["id"], replan_job_id=job["id"])
            replan_budget -= 1
            coalesced, cursor = _coalesce_inputs(store, runtime_token, since)
            continue
        break

    wants_reply = any(s["type"] == "final_response" for s in steps)
    if wants_reply:
        jobs_store.append_status_event(user_id, "writing_reply", job_id=job["id"], label="正在回复")
        merged_state = {**runtime_state, "action_digest": action_state["action_digest"]}
        # responder 走 BYOK provider_config。
        reply = await asyncio.to_thread(
            v2_responder.respond,
            provider_config=provider_config,
            coalesced_messages=coalesced, runtime_state=merged_state,
            action_results=action_state["action_results"])
        if reply:
            _write_encrypted_reply(store, reply)

    jobs_store.upsert_runtime_state(
        user_id, {"last_replied_ts": cursor, "action_digest": action_state["action_digest"]})
    jobs_store.append_status_event(user_id, "done", job_id=job["id"], label="完成")
    # 跨进程唤醒 web 层 parked 的 chat long-poll（worker 与 web 是不同进程/CVM，origin 不同 → 会被派发）。
    core_wake_bus.notify("chat", user_id)
    jobs_store.mark_completed(job["id"])
```

> 注：`store.chat_messages` 是密文，`v2_inval.evaluate` 只看 role/ts（不看内容），故无需解密即可判定
> 「有没有新用户消息」。`_coalesce_inputs` 才需明文（经 **B 的 `_read_messages`** enclave 解密）。测试里
> `_coalesce_inputs` / `_write_encrypted_reply` 被 monkeypatch，避免真打 enclave；`provider_config` 由
> worker loop 用 `TurnDeps.resolve_provider(user_id)` JIT 解密后传入 `process_job`。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest ../tests/test_v2_worker_process_job.py -v`
Expected: PASS（2 passed）。关键：`test_process_job_replans_on_concurrent_new_message`（并发新消息触发 invalidation 状态机 + replan 在安全点发生）。

（本测试 DB-backed，走 `client`/`backend_env`/`seed_user`；不加入 `_PURE_UNIT`。）

- [ ] **Step 5: Commit**

```bash
git add backend/model_api_runtime/v2/worker.py tests/test_v2_worker_process_job.py
git commit -m "feat(v2): worker runs coalesce->planner->executor->replan->responder (spec §13.5-8)"
```

---

## Task 9: poll_core.py + db.py + routes_asgi.py — long-poll 带 status 游标（§9）

**Files:**
- Create: `backend/db.py` 新增只读原语 `list_agent_status_events`（追加到文件；不改既有函数）
- Modify: `backend/chat/poll_core.py`（`build_response` 加 `status_events` + `status_cursor`；新增 `pending_status_events`）
- Modify: `backend/chat/routes_asgi.py::chat_poll`（读 `since_status_id`，与 pending 消息一同返回，任一非空即返回）
- Test: `tests/test_v2_status_poll.py`

**Interfaces:**
- Consumes: `db.list_agent_status_events(user_id, *, after_id, limit) -> list[dict]`（SQL 原语，读 `agent_status_events` 表——Plan B 的 `0014` 建）。
- Produces:
  - `poll_core.pending_status_events(store, *, after_id, limit=50) -> list[dict]`
  - `poll_core.build_response(..., status_events=None, status_cursor=0)` 返回体新增 `agent_status_events`（list）+ `status_cursor`（int，本批最大 id）
  - `chat_poll` 支持 `?since_status_id=N`；返回体带 status 游标；status-only 更新也能唤醒返回

- [ ] **Step 1: 写失败测试**

`tests/test_v2_status_poll.py`:

```python
"""long-poll 返回体带 agent_status_events 游标（spec §9）。DB-backed（真表 + 真路由）。

覆盖：(1) poll_core.build_response 带 status 字段；(2) db 原语按 after_id 增量取；
(3) chat_poll 在只有 status 更新（无新聊天消息）时也能返回 status 事件。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import conftest  # noqa: E402
import db  # noqa: E402
from chat import poll_core as chat_poll_core  # noqa: E402


def test_build_response_carries_status_cursor():
    resp = chat_poll_core.build_response(
        messages=[], context={"runtime_v2": {}, "client_release": {}},
        consumer_id="c1", claim=True, timed_out=True,
        status_events=[{"id": 5, "kind": "reading_memory", "label": "读取上下文", "detail": {}}],
        status_cursor=5)
    assert resp["agent_status_events"][0]["kind"] == "reading_memory"
    assert resp["status_cursor"] == 5


def test_db_list_agent_status_events_increments_by_after_id(client, backend_env):
    conftest.seed_user("usr_status1")
    a = db.append_agent_status_event("usr_status1", "processing", label="处理中", detail={})
    b = db.append_agent_status_event("usr_status1", "reading_memory", label="读取上下文", detail={"count": 3})
    all_ev = db.list_agent_status_events("usr_status1", after_id=0, limit=50)
    assert [e["kind"] for e in all_ev] == ["processing", "reading_memory"]
    after_a = db.list_agent_status_events("usr_status1", after_id=a, limit=50)
    assert [e["id"] for e in after_a] == [b]


def test_chat_poll_returns_status_only_update(client, backend_env):
    conftest.seed_user("usr_status2")
    # register the user's api key so the poll authenticates as them.
    key = conftest.seed_api_key("usr_status2")   # helper added in Task 9 conftest note
    db.append_agent_status_event("usr_status2", "reading_perception", label="读取感知", detail={})
    r = client.get("/v1/chat/poll", params={"since": 0, "timeout": 0, "since_status_id": 0},
                   headers={"Authorization": f"Bearer {key}"})
    body = r.json()
    assert any(e["kind"] == "reading_perception" for e in body["agent_status_events"])
    assert body["status_cursor"] >= 1
```

> conftest 便利：本 Task 需要一个「给 seeded user 造可用 api key」的 helper。若 `tests/conftest.py`
> 还没有 `seed_api_key`，在 Task 9 里补一个薄 helper（下方 Step 3 给出），并加入 conftest。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest ../tests/test_v2_status_poll.py -v`
Expected: FAIL — `AttributeError: module 'db' has no attribute 'append_agent_status_event'` /
`build_response() got an unexpected keyword argument 'status_events'`

- [ ] **Step 3: 最小实现**

(a) `backend/db.py` 追加两个原语（`append_*` 供测试/Plan B 共用；`list_*` 供 poll 读侧）。按本文件既有
`with _conn() as conn:` 约定书写——下例用通用游标形态，落地时对齐本仓 `db.py` 的连接助手命名：

```python
def append_agent_status_event(user_id, kind, *, job_id=None, label=None, detail=None, seq=0) -> int:
    """写一条 agent_status_events（0014 表）。返回自增 id。business-free SQL 原语（CONTRIBUTING §2）。"""
    import json as _json
    with _conn() as conn:
        row = conn.execute(
            "INSERT INTO agent_status_events (job_id, user_id, kind, label, detail_json, seq) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (job_id, user_id, kind, label, _json.dumps(detail or {}), int(seq)),
        ).fetchone()
        return int(row[0])


def list_agent_status_events(user_id, *, after_id=0, limit=50) -> list[dict]:
    """user 的 status 事件，id > after_id，升序。供 long-poll 增量游标（§9）。"""
    import json as _json
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, job_id, kind, label, detail_json, seq, "
            "extract(epoch from created_at) AS ts "
            "FROM agent_status_events WHERE user_id = %s AND id > %s "
            "ORDER BY id ASC LIMIT %s",
            (user_id, int(after_id), int(limit)),
        ).fetchall()
    out = []
    for r in rows:
        detail = r[4]
        if isinstance(detail, str):
            try:
                detail = _json.loads(detail)
            except Exception:
                detail = {}
        out.append({"id": int(r[0]), "job_id": r[1], "kind": r[2],
                    "label": r[3], "detail": detail or {}, "seq": int(r[5]), "ts": float(r[6])})
    return out
```

> 注：`jobs_store.append_status_event` / `jobs_store.list_status_events`（Plan B）应委托到这两个 `db.*`
> 原语，保证 worker 写侧与 poll 读侧读同一张表、同一形状。本 Task 只加 `db.*`；不动 `jobs_store`（B 拥有）。

(b) `backend/chat/poll_core.py` — 新增 `pending_status_events` + 扩展 `build_response`：

```python
import db   # 顶部 import 区加入（db 是最底层，chat 可向下依赖）


def pending_status_events(store: UserStore, *, after_id: int, limit: int = 50) -> list:
    """自 after_id 以来该用户的 agent_status_events（§9 tool-call 步骤前台推送）。

    读侧走 db 原语（不 upward import model_api_runtime.v2；见 Global Constraints 依赖方向）。
    """
    return db.list_agent_status_events(store.user_id, after_id=after_id, limit=limit)
```

把 `build_response` 改为（新增两个 keyword-only 参数，默认值保持既有调用方兼容）：

```python
def build_response(
    *, messages: list, context: dict, consumer_id: str, claim: bool, timed_out: bool,
    status_events: list | None = None, status_cursor: int = 0,
) -> dict:
    """The `/v1/chat/poll` response contract (locked for parity, + §9 status)."""
    events = status_events or []
    return {
        "messages": messages,
        "runtime_v2": context["runtime_v2"],
        "client_release": context["client_release"],
        "timed_out": timed_out,
        "consumer_id": consumer_id,
        "claimed": claim,
        "agent_status_events": events,
        "status_cursor": int(status_cursor or (events[-1]["id"] if events else 0)),
    }
```

(c) `backend/chat/routes_asgi.py::chat_poll` — 读 `since_status_id`，与 pending 消息一同 check，
status-only 更新也能返回。改动点（在既有函数内）：

```python
    # 既有 since / timeout / consumer_id / claim 解析之后追加：
    try:
        since_status_id = int(request.query_params.get("since_status_id", 0))
    except (TypeError, ValueError):
        since_status_id = 0

    async def _status():
        return await threadpool.run_db(
            chat_poll_core.pending_status_events, store, after_id=since_status_id, limit=50)

    def _response(messages, status_events, timed_out):
        cursor = status_events[-1]["id"] if status_events else since_status_id
        return chat_poll_core.build_response(
            messages=messages, context=context, consumer_id=consumer_id,
            claim=claim, timed_out=timed_out,
            status_events=status_events, status_cursor=cursor)
```

把原来两处 `return _response(pending, timed_out=...)` / `_response([], ...)` 改成先各取 status，
返回时带上；并把「有 pending 就返回」的条件放宽为「pending 或 status_events 任一非空即返回」：

```python
        pending = await _check()
        status_events = await _status()
        if pending or status_events:
            return _response(pending, status_events, timed_out=False)
        try:
            await asyncio.wait_for(waiter.event.wait(), timeout=max(0.0, timeout))
            notified = True
        except asyncio.TimeoutError:
            notified = False
    finally:
        registry.unregister(waiter)

    if notified:
        return _response(await _check(), await _status(), timed_out=False)
    return _response([], await _status(), timed_out=True)
```

（cap 命中的早退分支同样改成 `return _response([], [], timed_out=True)`。）

(d) `tests/conftest.py` — 若无 `seed_api_key`，加一个薄 helper（造一个能通过 `require_auth` 的 key）：

```python
def seed_api_key(user_id: str) -> str:
    """Test-only：给 seeded user 造一个可用于 Bearer 认证的 api key，并登记进 registry。
    复用注册路径的最小写入；返回明文 key 供 Authorization 头使用。"""
    import secrets
    from accounts import registry
    key = "tk_" + secrets.token_hex(16)
    with registry._users_lock:
        for u in registry._users:
            if u.get("user_id") == user_id:
                u["api_key"] = key
        registry._key_to_user[key] = user_id
    return key
```

> 若本仓的认证键形状与上述不同（如需哈希入库 / 特定字段名），以本仓 `accounts.registry` 现有注册写入为准
> 对齐——本 helper 仅示意「造一个 require_auth 能认的 key」。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest ../tests/test_v2_status_poll.py -v`
Expected: PASS（3 passed）

回归 long-poll 既有测：`cd backend && python -m pytest ../tests/test_asgi_waiters.py -v`
Expected: PASS（build_response 新增参数有默认值，既有调用方不受影响）。

- [ ] **Step 5: Commit**

```bash
git add backend/db.py backend/chat/poll_core.py backend/chat/routes_asgi.py tests/test_v2_status_poll.py tests/conftest.py
git commit -m "feat(v2): long-poll carries agent_status_events cursor (spec §9)"
```

---

## 端到端验收（全 Task 后）

- [ ] 全 V2 单测：`cd backend && python -m pytest ../tests/test_v2_*.py -v`
- [ ] 回归相关既有测：`cd backend && python -m pytest ../tests/test_asgi_waiters.py ../tests/test_model_api_prompts.py ../tests/test_hosted_agent_runtime_cutover.py -v`
- [ ] pyflakes 干净：`pyflakes backend/model_api_runtime/v2 backend/chat/poll_core.py`
- [ ] import 不成环：`cd backend && python -c "import asgi_app"`

**验收测试到 Task 映射（spec §16）：**

| 验收点 | Task |
|---|---|
| 并发新消息触发 invalidation 状态机 | Task 6（`evaluate` REPLAN）+ Task 8（`process_job` replan 一次） |
| coalesce 三条成一回合 | Task 1（`test_three_user_messages_coalesce_into_one_turn`） |
| 弱模型走确定性 planner 零 LLM | Task 3（`test_plan_weak_model_uses_rule_path_zero_llm`，platform 探针炸） |
| status 事件脱敏且合并限频 | Task 2（`merge_parallel_reads` + `RateLimiter` + `redact_status` 只标签/粗计数） |
| replan 在安全点发生 | Task 6（非安全点 `raise ValueError`；`before_final_response` committed → FINISH）+ Task 8 |
| §7.3 key 隔离（用户 key，平台 key 从不触达；弱模型零 LLM） | Task 3 / Task 4 / Task 7（平台 key 哨兵断言） |
| no-filler（只 final_response 落气泡） | Task 8（`_write_encrypted_reply` 只在 `wants_reply` + 非空 reply 时调） |

---

## Self-Review notes

- **Spec 覆盖**：§7.1→T1；§9 脱敏/限频→T2；§7.3 弱模型→T3；§7.2/7.3 official→T4；§7.4→T5；§8→T6；
  §7.5→T7；§13.5-8 worker 集成→T8；§9 推送管线→T9。§10 灰度 / §5 建表属 Plan B（本 plan 消费）。
- **类型一致**：`plan()`/`execute_plan()`/`respond()` 签名跨 Task 一致；`action_results`/`action_digest`
  在 T5 产出、T7/T8 消费，字段名一致；BYOK `provider_config` 由 worker 注入、在 T3/T4/T7/T8 一致传递
  （planner/responder 用它，executor 用独立的 enclave-auth api_key/runtime_token——两套凭证不混）。
- **无占位**：所有被调函数或在本 plan 定义（coalesce/status_stream/planner/executor/invalidation/
  responder/worker/db/poll_core）或在「消费接口」列明（capabilities/jobs_store/provider_client/envelope/
  wake_bus/prompts/spawners/cutover）。
