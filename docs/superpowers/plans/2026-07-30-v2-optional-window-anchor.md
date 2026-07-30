# V2 optional 重放窗口锚定 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 optional 重放窗口的起点从「每轮重算的差值」改成「持久化锚点」，让锚点不变期间连续两回合的 prompt 前缀逐字节一致。

**Architecture:** 复用 PR #135 已交付的 `tail_anchor.py`（纯滞后策略）与 `v2_chat_tail_anchor` 表，**只换消费方**：锚点不再喂给 `compact_through_seq`（那是上次的错误落点，不影响 prompt），而是用来过滤 `eligible_optional` 的起点。窗口读取放大到滞后上限以保证锚点落在窗口内。

**Tech Stack:** Python 3 / psycopg / pytest。改动集中在 `backend/model_api_runtime/v2/worker.py` 三处 + `backend/db.py` 一个新函数。

**设计依据：** `docs/superpowers/specs/2026-07-30-v2-optional-window-anchor-design.md`（务必先读 §2 根因与 §8 测试策略）

## Global Constraints

- **不要自行 `git add` / `git commit`。** commit 必须由用户显式要求。
- **测试前必须导出：** `export DATABASE_URL="postgresql://postgres:test@127.0.0.1:55432/postgres"`
  容器 `feedling-test-pg` 已在运行。**`skipped` 不等于 `passed`。**
- **基线**：`7185 passed, 1 skipped, 9 xfailed, 0 failed`（分支 `feat/v2-tail-anchor-groundwork`）。
  已知两个 **pre-existing flaky**，与本计划无关，遇到单独重跑确认即可、不要去修：
  - `tests/test_v2_jobs_store.py` 有 3 条在全量并发下偶发红
  - `tests/test_debug_trace.py::test_debug_read_waits_for_worker_instead_of_becoming_a_second_writer`（`timeout=0.01`，10 毫秒）
  - `tests/test_identity_redistill_ipc.py::test_redistill_ipc_serve_forever_end_to_end`（CI 上的 unix socket 竞态）
- **行号会漂移**：每个 Task 都会改 `worker.py`，后续 Task 的行号与本文所写不同。**动手前一律先 grep 定位**，不要盲信行号。
- **⚠️ `db.chat_recent_genuine_turn_boundary_seq` 在 `worker.py` 有两个调用点，只准动 chat lane 那一处：**
  - `max_turns=_WAKE_TAIL_MAX_TURNS` → wake lane，**绝对不动**
  - `max_turns=_CHAT_TAIL_MAX_TURNS` → chat lane，目标
  两段结构几乎逐行相同，唯一可靠判别是 `max_turns=` 后的常量名。
- **`compact_through_seq` 的取值必须与改动前逐字节一致**（spec §6 不变量 5）。这是本次与上次错误实施的根本区别。
- `python -m pyflakes backend/model_api_runtime backend/db.py` 恒定 4 条既有告警（`serve_worker.py:1427/2307`、`kill_switch.py:75` ×2），不要去修，只要数量不变。

---

## File Structure

| 文件 | 责任 | 动作 |
| --- | --- | --- |
| `backend/db.py` | 新增锚点后真实用户轮数计数 | 修改 |
| `backend/model_api_runtime/v2/worker.py` | 常量、chat lane 接线、窗口放大、optional 过滤 | 修改 |
| `tests/test_v2_optional_anchor.py` | 验收测试 + 配套测试（新建，与 Task 3 的存储层测试分开） | 创建 |
| `backend/alembic/versions/0071_v2_chat_tail_anchor.py` | 表注释语义更正 | 修改 |

新建测试文件而不是追加进 `tests/test_v2_tail_anchor_store.py`：后者是存储层单测（读写、单调、CASCADE），本计划的测试是热路径行为测试，两者关注点不同。

---

## Task 1: 地基（常量 + 计数函数）

**背景：** `_CHAT_TAIL_ANCHOR_MAX_TURNS` 和 `db.chat_genuine_turn_count_after_seq` 都曾在上次实施中写过，随回退一并消失。它们本身没有错（错的是消费方），可从归档分支原样取回。

**Files:**
- Modify: `backend/db.py`（在 `chat_recent_genuine_turn_boundary_seq` 之后追加）
- Modify: `backend/model_api_runtime/v2/worker.py`（常量区）
- Test: `tests/test_v2_optional_anchor.py`（创建）

**Interfaces:**
- Produces: `db.chat_genuine_turn_count_after_seq(user_id: str, *, after_seq: int, through_seq: int) -> int`
- Produces: `worker._CHAT_TAIL_ANCHOR_MAX_TURNS`（int，默认 60，保证 > `_CHAT_TAIL_MAX_TURNS`）

- [ ] **Step 1: 写失败测试**

创建 `tests/test_v2_optional_anchor.py`：

```python
"""optional 重放窗口锚定的热路径行为测试。

与 tests/test_v2_tail_anchor_store.py 的分工：那里测锚点的存取（读写、单调、
CASCADE），这里测锚点如何影响 prompt 组装。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db

from conftest import seed_user

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DB-backed optional-anchor tests require the PostgreSQL test fixture",
)


def _append(uid, role, text, *, source=None):
    """插入一条真实 chat 行并返回真实 seq。

    seq 是全表共享的 identity 列，绝不等于该用户自己的第几条消息。
    """
    from core import store as core_store

    store = core_store.get_store(uid)
    envelope = {
        "v": 1,
        "body_ct": text,
        "nonce": "n",
        "K_user": "k_test",
        "id": f"{uid}-{text}",
    }
    if source is not None:
        envelope["source"] = source
    row = store.append_chat(
        role,
        "user_message" if role == "user" else "model_api",
        envelope,
        strict=True,
    )
    seq = db.chat_seq_for_msg_id(uid, row["id"])
    assert seq is not None
    return seq


def test_genuine_turn_count_after_seq_counts_only_real_user_turns():
    """与 chat_recent_genuine_turn_boundary_seq 同口径：只数 user/human，
    且排除 verify_ping / resident_maintenance 合成行。"""
    seed_user("u_optanchor_cnt")
    first = _append("u_optanchor_cnt", "user", "m1")
    _append("u_optanchor_cnt", "assistant", "r1")
    _append("u_optanchor_cnt", "user", "m2")
    _append("u_optanchor_cnt", "user", "m3")
    _append("u_optanchor_cnt", "user", "ping", source="verify_ping")
    through = db.chat_max_seq("u_optanchor_cnt")

    assert db.chat_genuine_turn_count_after_seq(
        "u_optanchor_cnt", after_seq=first, through_seq=through
    ) == 2, "verify_ping 行不得计入"
    assert db.chat_genuine_turn_count_after_seq(
        "u_optanchor_cnt", after_seq=0, through_seq=through
    ) == 3


def test_anchor_hysteresis_ceiling_exceeds_target():
    """没有滞后区就退化回逐轮滑动窗口——即本次要修的 bug 本身。"""
    from model_api_runtime.v2 import worker

    assert worker._CHAT_TAIL_ANCHOR_MAX_TURNS > worker._CHAT_TAIL_MAX_TURNS
```

- [ ] **Step 2: 跑测试确认失败**

```bash
export DATABASE_URL="postgresql://postgres:test@127.0.0.1:55432/postgres"
python -m pytest tests/test_v2_optional_anchor.py -v
```
预期：两条都 FAIL（`db` 无该函数 / `worker` 无该常量）。

- [ ] **Step 3: 加计数函数**

在 `backend/db.py` 的 `chat_recent_genuine_turn_boundary_seq` 之后追加（先用
`grep -n "def chat_recent_genuine_turn_boundary_seq" backend/db.py` 定位）：

```python
def chat_genuine_turn_count_after_seq(
    user_id: str,
    *,
    after_seq: int,
    through_seq: int,
) -> int:
    """Genuine user turns strictly after ``after_seq`` up to ``through_seq``.

    Same predicate as :func:`chat_recent_genuine_turn_boundary_seq` so the
    optional-window anchor's hysteresis counts exactly the rows that define
    its window.
    """
    lower = int(after_seq)
    upper = int(through_seq)
    if lower < 0 or upper < 0:
        raise ValueError("seq bounds must be >= 0")
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM chat_messages "
            "WHERE user_id=%s AND seq>%s AND seq<=%s "
            "AND doc->>'role' IN ('user','human') "
            "AND COALESCE(doc->>'source','') "
            "NOT IN ('verify_ping','resident_maintenance')",
            (str(user_id), lower, upper),
        ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0
```

- [ ] **Step 4: 加常量**

在 `backend/model_api_runtime/v2/worker.py` 的 `_WAKE_TAIL_MAX_TURNS` 定义之后追加
（先 `grep -n "_WAKE_TAIL_MAX_TURNS = " backend/model_api_runtime/v2/worker.py` 定位）：

```python
# optional 重放窗口的滞后上限：eligible_optional 的轮数在 _CHAT_TAIL_MAX_TURNS
# 与本值之间浮动。必须严格大于 _CHAT_TAIL_MAX_TURNS，否则没有滞后区、退化回
# 逐轮滑动窗口（即本次要修的 bug 本身）。
_CHAT_TAIL_ANCHOR_MAX_TURNS = _positive_int_env(
    "FEEDLING_V2_CHAT_TAIL_ANCHOR_MAX_TURNS", "60"
)
if _CHAT_TAIL_ANCHOR_MAX_TURNS <= _CHAT_TAIL_MAX_TURNS:
    _CHAT_TAIL_ANCHOR_MAX_TURNS = _CHAT_TAIL_MAX_TURNS + 20
```

- [ ] **Step 5: 跑测试确认通过**

```bash
python -m pytest tests/test_v2_optional_anchor.py -v
```
预期：2 passed。

- [ ] **Step 6: 提交（仅在用户明确要求时执行）**

```bash
git add backend/db.py backend/model_api_runtime/v2/worker.py tests/test_v2_optional_anchor.py
git commit -m "feat(v2): add turn-count helper and optional-window hysteresis ceiling"
```

---

## Task 2: 验收测试（先写到红）

**背景：** 这是整个计划的核心。上一次实施之所以接错位置还能全绿，正是因为缺这条测试。本 Task 的交付物是**一条正确地失败的测试**——它必须因为「前缀不稳定」而红，不能因为环境/脚手架问题而红。

**⚠️ 一个必须避开的陷阱：** 归档分支的 rig 里 `_summary_with_seq` 返回 `("", 0.0, 0, 0)`，即 watermark = 0（从未压缩）。那样**所有消息都落进 required、optional 恒为空**，本测试就什么也测不到、还会假装通过。本 Task 的 rig 必须让 watermark > 0，制造出真实的 optional 轮次。

**Files:**
- Modify: `tests/test_v2_optional_anchor.py`

**Interfaces:**
- Consumes: Task 1 的 `db.chat_genuine_turn_count_after_seq`、`worker._CHAT_TAIL_ANCHOR_MAX_TURNS`
- Produces: 测试脚手架 `_seed_chat_row` / `_fake_sealed_envelope` / `_optional_anchor_deps` / `_capture_prompts`

- [ ] **Step 1: 从归档分支取回脚手架**

```bash
git show archive/v2-tail-anchor-wrong-wiring:tests/test_v2_tail_anchor_store.py > /tmp/archive-anchor-tests.py
```

从中原样复制这四个 helper 到 `tests/test_v2_optional_anchor.py`（它们连同注释一起抄，
注释里记录了踩过的坑，有价值）：

- `_reset_anchor_hotpath_user`（清表 + `set_v2_runtime_owner`，否则 `claim_next_job` 因
  INNER JOIN `v2_runtime_state` 而静默返回 None）
- `_seed_chat_row`（真实 `chat_append_strict` 写入，**必须带明文 `content` 字段**，否则
  `coalesce_pending` 认为行为空、chat lane 走「无输入」捷径直接返回，根本到不了被测代码）
- `_fake_sealed_envelope`
- `_optional_anchor_deps`（照抄 `_seq_native_chat_deps`，**但按下一步修改 `_summary_with_seq`**）

连同它们需要的 import 一并复制（`worker`、`provider_client`、`cap_registry`、
`set_v2_runtime_owner`、`_BYOK`、`_FakeCapResult` 等，以归档文件顶部为准）。

- [ ] **Step 2: 改造 rig 使 optional 非空**

把复制过来的 `_summary_with_seq` 从 `return "", 0.0, 0, 0` 改成可配置 watermark：

```python
def _optional_anchor_deps(uid, monkeypatch, *, watermark_seq: int):
    """与归档版 _seq_native_chat_deps 相同，但摘要 watermark 可配置。

    watermark=0（归档版的写法）会让所有消息都落进 required、optional 恒为空，
    本文件的测试就什么都测不到还假装通过——这正是要避开的陷阱。
    """

    def _summary_with_seq(_uid):
        return "PRIOR SUMMARY", 0.0, 1, int(watermark_seq)

    # ...其余与归档版一致
```

- [ ] **Step 3: 写验收测试（此时应为红）**

```python
def _prefix_before_required(messages, required_first_content):
    """messages 中 required tail 第一条之前的全部消息（含 system 与摘要块）。"""
    for index, message in enumerate(messages):
        if message.get("content") == required_first_content:
            return messages[:index]
    raise AssertionError("required tail 的第一条未出现在 prompt 中")


def test_prompt_prefix_is_byte_identical_across_consecutive_turns(monkeypatch):
    """验收标准本身：锚点不变期间，连续两回合的 prompt 前缀逐字节一致。

    这条测试存在的唯一理由，是上一次实施把锚点接到了不影响 prompt 的
    compact_through_seq 上，而当时的测试（只断言锚点保持/前移、边界查询被跳过）
    照样全绿。任何「看起来接上了」的实现，都必须先过这一关。
    """
    import json

    uid = "u_optanchor_prefix"
    seed_user(uid)
    _reset_anchor_hotpath_user(uid)

    # 30 轮历史落在 watermark 之下 → 成为 optional 重放素材
    seqs = []
    for i in range(60):
        role = "user" if i % 2 == 0 else "openclaw"
        seqs.append(_seed_chat_row(uid, f"{uid}-h{i}", 1000.0 + i, role))
    watermark = seqs[-1]

    captured: list[list[dict]] = []

    async def _capture_chat_completion(config, messages, *, tools=None, **_kw):
        captured.append(json.loads(json.dumps(messages, ensure_ascii=False,
                                              sort_keys=True, default=str)))
        return {"reply": "ok", "tool_calls": [],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    deps = _optional_anchor_deps(uid, monkeypatch, watermark_seq=watermark)
    monkeypatch.setattr(provider_client, "chat_completion_async",
                        _capture_chat_completion)

    # 两个连续回合，中间只追加一条新用户消息
    for turn in range(2):
        _seed_chat_row(uid, f"{uid}-new{turn}", 2000.0 + turn, "user")
        job_id = jobs_store.enqueue_job(uid, "turn", reason="test")
        claimed = jobs_store.claim_next_job(claimed_by=f"w{turn}")
        assert claimed is not None, "job 未被 claim——rig 没搭对"
        asyncio.run(worker.process_job(claimed, deps=deps))

    assert len(captured) == 2, f"期望两次 provider 调用，实到 {len(captured)}"

    first_required = f"plaintext-{uid}-new0"
    prefix_a = _prefix_before_required(captured[0], first_required)
    prefix_b = _prefix_before_required(captured[1], first_required)

    assert json.dumps(prefix_a, sort_keys=True, ensure_ascii=False) == \
           json.dumps(prefix_b, sort_keys=True, ensure_ascii=False), (
        "两回合之间 prompt 前缀发生了变化——optional 窗口仍在逐轮滑动"
    )
```

⚠️ 上面的 `jobs_store.enqueue_job` / `claim_next_job` / `worker.process_job` 的确切调用
形态**以归档分支里那两个集成测试的真实写法为准**（它们已经跑通过），照抄它们的调用方式，
不要照抄本文这段示意代码的参数拼法。同样，`_prefix_before_required` 依赖
`_seed_chat_row` 写入的明文 `content` 形如 `plaintext-<msg_id>`，请与实际 rig 对齐。

- [ ] **Step 4: 跑测试，确认它红得对**

```bash
python -m pytest tests/test_v2_optional_anchor.py -k prefix -v
```

**预期：FAIL，且失败原因必须是最后那条断言（前缀不一致）。**

如果失败原因是 `job 未被 claim`、`期望两次 provider 调用`、`required tail 的第一条未出现`
或任何异常，说明 **rig 没搭对，不是被测行为的问题**——必须先把 rig 修到「红在最后一条断言上」
再进入 Task 3。带着一个坏掉的 rig 进下一步，等于没有验收测试。

- [ ] **Step 5: 提交（仅在用户明确要求时执行）**

```bash
git add tests/test_v2_optional_anchor.py
git commit -m "test(v2): add failing acceptance test for prompt-prefix stability"
```

---

## Task 3: 实施（让验收测试变绿）

**Files:**
- Modify: `backend/model_api_runtime/v2/worker.py`（三处）

**Interfaces:**
- Consumes: `tail_anchor.decide_anchor`、`jobs_store.get_chat_tail_anchor` / `set_chat_tail_anchor`（PR #135 已交付，**不要修改它们**）、Task 1 的计数函数与常量

- [ ] **Step 1: 确认三个改动点的当前行号**

```bash
grep -n "chat_recent_genuine_turn_boundary_seq" backend/model_api_runtime/v2/worker.py
grep -n "target_turns=_CHAT_TAIL_MAX_TURNS" backend/model_api_runtime/v2/worker.py
grep -n "optional_turns\[-optional_limit:\]" backend/model_api_runtime/v2/worker.py
grep -n "tail_target_turns=" backend/model_api_runtime/v2/worker.py
```

对照 `max_turns=` 后的常量名确认哪一处是 chat lane。**wake lane 一律不动。**

- [ ] **Step 2: chat lane 读锚点并决策**

在 chat lane 现有的 `if seq_context:` 块里（那段现在计算 `oldest_retained_seed` 和
`compact_through_seq`），**保留原有 `compact_through_seq` 的计算完全不动**，
在其后追加锚点决策：

```python
            stored_anchor = await asyncio.to_thread(
                jobs_store.get_chat_tail_anchor, user_id
            )
            turns_after_anchor = (
                await asyncio.to_thread(
                    db.chat_genuine_turn_count_after_seq,
                    user_id,
                    after_seq=int(stored_anchor),
                    through_seq=through_seq,
                )
                if stored_anchor is not None
                else 0
            )
            # 滞后区内跳过最重的那次边界查询（仍需读锚点与计数）。
            anchor_boundary_seq = None
            if (
                stored_anchor is None
                or turns_after_anchor >= _CHAT_TAIL_ANCHOR_MAX_TURNS
            ):
                anchor_boundary_seq = await asyncio.to_thread(
                    db.chat_recent_genuine_turn_boundary_seq,
                    user_id,
                    max_turns=_CHAT_TAIL_MAX_TURNS,
                    through_seq=through_seq,
                )
            anchor_decision = v2_tail_anchor.decide_anchor(
                current_anchor=stored_anchor,
                turns_after_anchor=turns_after_anchor,
                boundary_seq_for_target=anchor_boundary_seq,
                target_turns=_CHAT_TAIL_MAX_TURNS,
                max_turns_before_advance=_CHAT_TAIL_ANCHOR_MAX_TURNS,
            )
            if anchor_decision.advanced and anchor_decision.anchor_seq > 0:
                await asyncio.to_thread(
                    jobs_store.set_chat_tail_anchor,
                    user_id,
                    anchor_decision.anchor_seq,
                )
            optional_anchor_seq = (
                anchor_decision.anchor_seq
                if anchor_decision.anchor_seq > 0
                else None
            )
```

并在文件顶部 import 区（`summary_frontier as v2_summary_frontier` 附近）追加：

```python
from model_api_runtime.v2 import tail_anchor as v2_tail_anchor
```

⚠️ `optional_anchor_seq` 需要在后面 `_make_build_messages_fn` 的调用处可见；若该调用不在
同一作用域，在 `if seq_context:` 之前先 `optional_anchor_seq = None` 初始化。

- [ ] **Step 3: 放大窗口**

chat lane 调用 `_read_seq_adaptive_prompt_context` 处，把 `target_turns` 改为滞后上限：

```python
                    target_turns=_CHAT_TAIL_ANCHOR_MAX_TURNS,
```

该参数在 `_read_seq_adaptive_prompt_context` 内部**只用于 `read_recent_turns` 的窗口大小**
一处（已核实），不影响摘要读取或 `_adaptive_replay_parts` 的切分。
**wake lane 的同名调用保持 `_WAKE_TAIL_MAX_TURNS` 不变。**

- [ ] **Step 4: optional 按锚点过滤**

`_make_build_messages_fn` 增加关键字参数 `tail_anchor_seq: int | None = None`，
并把 `if target_turns is not None:` 块里的 `eligible_optional` 计算改为：

```python
    if target_turns is not None:
        if tail_anchor_seq is not None and int(tail_anchor_seq) > 0:
            # 锚点决定起点：滞后区内它逐轮不变，前缀因此逐字节稳定。
            anchor = int(tail_anchor_seq)

            def _group_start_seq(group: list[dict]) -> int | None:
                seqs = [
                    int(row["seq"])
                    for row in group
                    if row.get("seq") is not None
                ]
                return min(seqs) if seqs else None

            eligible_optional = [
                group
                for group in optional_turns
                if (_group_start_seq(group) or 0) >= anchor
            ]
        else:
            # 无锚点（老用户首次进入）：保持原有行为，与改动前逐字等价。
            required_turn_count = sum(
                1 for row in required_tail if _is_genuine_user_seed(row)
            )
            optional_limit = max(0, target_turns - required_turn_count)
            eligible_optional = (
                optional_turns[-optional_limit:] if optional_limit else []
            )
```

⚠️ 原代码中 `required_turn_count` 可能在该块之后仍被引用（例如
`effective_turns = required_turn_count + best`）。改动后必须保证它在两个分支下都有定义
——最稳妥的做法是把 `required_turn_count` 的计算提到 `if/else` 之前，两个分支共用。
改完先 `grep -n "required_turn_count" backend/model_api_runtime/v2/worker.py` 核对全部引用点。

- [ ] **Step 5: 把锚点传进去**

chat lane 调用 `_make_build_messages_fn`（或 `_chat_builder()`）的地方追加：

```python
                tail_anchor_seq=optional_anchor_seq,
```

- [ ] **Step 6: 跑验收测试**

```bash
export DATABASE_URL="postgresql://postgres:test@127.0.0.1:55432/postgres"
python -m pytest tests/test_v2_optional_anchor.py -v
```
预期：全部 PASS，Task 2 那条前缀测试转绿。

- [ ] **Step 7: 守住不变量**

```bash
python -m pytest tests/test_v2_prompt_invariant.py tests/test_v2_worker.py \
                 tests/test_v2_tail_anchor_store.py tests/test_v2_summary_watermark_seq.py -q
```
预期：全绿。`test_v2_prompt_invariant.py` 守的是「watermark 之后每条消息都逐字出现在
tail 中」，本改动只碰 watermark 之前的 optional，理应正交——若它红了，说明过滤逻辑
误伤了 required，必须停下来查。

- [ ] **Step 8: 确认 wake lane 与 compact_through_seq 未受影响**

```bash
git diff backend/model_api_runtime/v2/worker.py | grep -E "^[+-].*_WAKE_TAIL_MAX_TURNS" \
  && echo "!!! wake lane 被改动，必须回退 !!!" || echo "✓ wake lane 未触碰"
git diff backend/model_api_runtime/v2/worker.py | grep -E "^[+-].*compact_through_seq"
```
第二条命令的输出中**不得出现对 `compact_through_seq` 赋值语句的增删**（spec §6 不变量 5）。
新增的锚点代码引用它是不允许的；只允许出现纯新增的锚点相关行。

- [ ] **Step 9: 全量 L1**

```bash
python -m pytest tests/ -q --ignore=tests/e2e_model_api_test.py --ignore=tests/test_api.py
python -m pyflakes backend/model_api_runtime backend/db.py
```
预期：相对基线无新增失败（三个已知 flaky 见 Global Constraints）；pyflakes 仍为 4 条。

- [ ] **Step 10: 提交（仅在用户明确要求时执行）**

```bash
git add backend/model_api_runtime/v2/worker.py
git commit -m "perf(v2): anchor the optional-replay window to stabilize the prompt prefix"
```

---

## Task 4: 变异验证 + 配套测试 + 文档更正

**背景：** 验收测试通过还不够——必须证明它**在实现错误时会红**。上一次的教训正是「测试全绿但测的不是要测的东西」。

**Files:**
- Modify: `tests/test_v2_optional_anchor.py`
- Modify: `backend/alembic/versions/0071_v2_chat_tail_anchor.py`（注释）

- [ ] **Step 1: 变异验证（手工，必做）**

把 Task 3 Step 4 的锚点分支临时改回原实现（即让 `tail_anchor_seq` 分支也走
`optional_turns[-optional_limit:]`），然后：

```bash
python -m pytest tests/test_v2_optional_anchor.py -k prefix -v
```

**预期：FAIL。** 若仍然 PASS，说明验收测试没有鉴别力，等于没写——必须回到 Task 2 重做。

记录实际输出后用 `git checkout` 还原，并确认 `git status --porcelain` 干净。

- [ ] **Step 2: 补配套测试**

在 `tests/test_v2_optional_anchor.py` 追加三条（rig 复用 Task 2 的）：

先从归档分支原样取回 `_spy_on_boundary_query`（它**包装**而非 stub 真实实现，
stub 会掩盖「是否真的付了一次边界查询」）：

```bash
git show archive/v2-tail-anchor-wrong-wiring:tests/test_v2_tail_anchor_store.py \
  | sed -n '/^def _spy_on_boundary_query/,/^    return calls/p'
```

然后追加三条测试。`_run_one_turn` 是本 Task 抽出的小 helper，把 Task 2 里
「追加一条新消息 → enqueue → claim → process_job」那四步收敛成一处，三条测试共用：

```python
def _run_one_turn(uid, deps, monkeypatch, *, tag: str) -> None:
    """追加一条新用户消息并完整跑一个 chat 回合。

    步骤与 Task 2 的验收测试完全一致——那里已经跑通，这里只是抽出来复用，
    不要另起一套调用方式。
    """
    _seed_chat_row(uid, f"{uid}-{tag}", 3000.0 + hash(tag) % 1000, "user")
    job_id = jobs_store.enqueue_job(uid, "turn", reason="test")
    claimed = jobs_store.claim_next_job(claimed_by=f"w-{tag}")
    assert claimed is not None, f"job 未被 claim（tag={tag}）——rig 没搭对"
    asyncio.run(worker.process_job(claimed, deps=deps))


def test_anchor_holds_within_hysteresis_band(monkeypatch):
    """滞后区内：锚点值不变，且不付出边界查询的代价。

    边界查询是三次 DB 往返里最重的一次；跳过它是本设计声称的收益之一，
    所以要用 spy 证明它真的没被调用，而不是只看锚点没变。
    """
    uid = "u_optanchor_hold"
    seed_user(uid)
    _reset_anchor_hotpath_user(uid)

    seqs = [
        _seed_chat_row(uid, f"{uid}-h{i}", 1000.0 + i,
                       "user" if i % 2 == 0 else "openclaw")
        for i in range(60)
    ]
    watermark = seqs[-1]
    # 锚点落在滞后区内：其后的真实用户轮数远小于 _CHAT_TAIL_ANCHOR_MAX_TURNS
    jobs_store.set_chat_tail_anchor(uid, seqs[-6])
    before = jobs_store.get_chat_tail_anchor(uid)

    deps = _optional_anchor_deps(uid, monkeypatch, watermark_seq=watermark)
    boundary_calls = _spy_on_boundary_query(monkeypatch)
    _run_one_turn(uid, deps, monkeypatch, tag="hold")

    assert jobs_store.get_chat_tail_anchor(uid) == before, "滞后区内锚点不应移动"
    assert boundary_calls == [], (
        f"滞后区内不应查边界，实际被调用 {len(boundary_calls)} 次"
    )


def test_anchor_advances_once_past_the_ceiling(monkeypatch):
    """越过滞后上限：锚点前移一次，且边界查询确实被调用。"""
    uid = "u_optanchor_advance"
    seed_user(uid)
    _reset_anchor_hotpath_user(uid)

    # 制造远超滞后上限的用户轮数：锚点钉在最早，其后累积 > 上限
    total_turns = worker._CHAT_TAIL_ANCHOR_MAX_TURNS + 10
    seqs = []
    for i in range(total_turns * 2):
        seqs.append(_seed_chat_row(uid, f"{uid}-a{i}", 1000.0 + i,
                                   "user" if i % 2 == 0 else "openclaw"))
    watermark = seqs[-1]
    jobs_store.set_chat_tail_anchor(uid, seqs[0])
    before = jobs_store.get_chat_tail_anchor(uid)

    deps = _optional_anchor_deps(uid, monkeypatch, watermark_seq=watermark)
    boundary_calls = _spy_on_boundary_query(monkeypatch)
    _run_one_turn(uid, deps, monkeypatch, tag="advance")

    after = jobs_store.get_chat_tail_anchor(uid)
    assert after > before, f"越过上限后锚点应前移（{before} -> {after}）"
    assert boundary_calls, "前移时必须真的查一次边界"


def test_prefix_stable_again_after_an_advance(monkeypatch):
    """前移那一轮前缀变化是设计内的；此后必须重新稳定。

    这条防的是「锚点每轮都在前移」——那样每轮都合法地变，但等于没修。
    """
    import json

    uid = "u_optanchor_restable"
    seed_user(uid)
    _reset_anchor_hotpath_user(uid)

    seqs = [
        _seed_chat_row(uid, f"{uid}-r{i}", 1000.0 + i,
                       "user" if i % 2 == 0 else "openclaw")
        for i in range(60)
    ]
    watermark = seqs[-1]
    jobs_store.set_chat_tail_anchor(uid, seqs[0])  # 立刻会触发一次前移

    captured: list[list[dict]] = []

    async def _capture(config, messages, *, tools=None, **_kw):
        captured.append(json.loads(json.dumps(messages, ensure_ascii=False,
                                              sort_keys=True, default=str)))
        return {"reply": "ok", "tool_calls": [],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    deps = _optional_anchor_deps(uid, monkeypatch, watermark_seq=watermark)
    monkeypatch.setattr(provider_client, "chat_completion_async", _capture)

    _run_one_turn(uid, deps, monkeypatch, tag="adv")   # 第 1 轮：可能前移
    anchor_after_advance = jobs_store.get_chat_tail_anchor(uid)
    _run_one_turn(uid, deps, monkeypatch, tag="s1")    # 第 2 轮
    _run_one_turn(uid, deps, monkeypatch, tag="s2")    # 第 3 轮

    assert jobs_store.get_chat_tail_anchor(uid) == anchor_after_advance, (
        "前移之后锚点必须重新稳定，不能每轮都动"
    )
    assert len(captured) == 3
    prefix_2 = _prefix_before_required(captured[1], f"plaintext-{uid}-s1")
    prefix_3 = _prefix_before_required(captured[2], f"plaintext-{uid}-s1")
    assert json.dumps(prefix_2, sort_keys=True, ensure_ascii=False) == \
           json.dumps(prefix_3, sort_keys=True, ensure_ascii=False), (
        "前移之后的连续两轮，前缀应重新逐字节一致"
    )
```

⚠️ 三条测试里 `jobs_store` / `asyncio` / `provider_client` 的 import 与
`_run_one_turn` 的具体调用形态，都以 Task 2 已跑通的 rig 为准。
第三条依赖 `_prefix_before_required`（Task 2 已定义）。

- [ ] **Step 3: 更正表注释的语义**

`backend/alembic/versions/0071_v2_chat_tail_anchor.py` 的 docstring 与建表注释里
「pinned verbatim-tail start seq」的说法已过时——锚点现在钉的是 **optional 重放的起点**。
改为准确表述。**只改注释文本，不要改 DDL、revision、down_revision。**

同样更新 `jobs_store.get_chat_tail_anchor` / `set_chat_tail_anchor` 的 docstring。

- [ ] **Step 4: 跑测试**

```bash
python -m pytest tests/test_v2_optional_anchor.py tests/test_v2_tail_anchor_store.py -v
python -m pytest tests/ -q --ignore=tests/e2e_model_api_test.py --ignore=tests/test_api.py
```
预期：全绿（三个已知 flaky 除外）。

- [ ] **Step 5: 提交（仅在用户明确要求时执行）**

```bash
git add tests/test_v2_optional_anchor.py backend/alembic/versions/0071_v2_chat_tail_anchor.py \
        backend/model_api_runtime/v2/jobs_store.py
git commit -m "test(v2): prove prefix test discriminates; correct anchor semantics in docs"
```

---

## 上线与验证

**部署到 test：** push `test` 分支 → CI `deploy-test-cvm`。

**上线后 48h 的验收口径**（改动前基线：`<1min` 档 35.8%、`write/read` 1.03）：

```sql
with t as (
  select user_id, created_at, model_calls, prompt_tokens,
         cache_read_tokens, cache_miss_tokens, cache_write_tokens,
         extract(epoch from (created_at - lag(created_at)
           over (partition by user_id order by created_at))) gap_sec
  from v2_turn_metrics
  where lane='chat' and created_at > now() - interval '48 hours'
    and cache_reported_calls > 0
)
select case when gap_sec < 60 then '<1min'
            when gap_sec < 300 then '1-5min'
            else '>5min' end bucket,
       count(*) turns,
       round((100.0*sum(cache_read_tokens)
             /nullif(sum(cache_read_tokens)+sum(cache_miss_tokens),0))::numeric,1) hit_pct,
       round((sum(cache_write_tokens)::numeric
             /nullif(sum(cache_read_tokens),0))::numeric,2) write_per_read
from t where model_calls = 1
group by 1 order by 1;
```

**判定：** `<1min` 档命中率显著高于 35.8%，`write/read` 明显低于 1.03。
**必须用「单轮 turn × 间隔分档」这个口径**——整体命中率会被 turn 内复用和大 prompt 回合带偏
（见 spec §1 与前置 spec §4.2）。

**同时监控：** `tail_fallback` 是否开始出现（prompt 涨约 40% 后可能触发预算削减），
以及 `turn_failed` 比例不得上升。

**回滚：** 设 `FEEDLING_V2_CHAT_TAIL_ANCHOR_MAX_TURNS` 不大于 `FEEDLING_V2_CHAT_TAIL_MAX_TURNS`
会被钳制抬回，因此真正的回滚是回退 Task 3 的 commit。锚点表与数据留着无害（无写入方后
即为惰性数据）。

## 本计划不做的

- 不为预算二分搜索的削减路径做特殊处理（spec §7，YAGNI）
- 不动 wake lane、不动 `compact_through_seq`、不动 compaction 折叠力度
- 不删除 optional 重放（产品已确认必须保留）
- `v2_summary_frontier_exhausted` 根因、16k 路由 `providererror` 真因，均属独立议题
