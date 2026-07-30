# Runtime V2 上下文预算与缓存效率优化 Implementation Plan

> ## ⚠️ 本计划已部分作废（2026-07-30）
>
> **Task 1–3 已实施并保留**（`1c7812e9` / `c6e1a491` / `6f8add25`），逐个评审通过。
>
> **Task 4（热路径接入）已实施后回退**，归档在分支 `archive/v2-tail-anchor-wrong-wiring`
> （`a12626aa` / `98d56a11`）。原因：本计划依据的根因定位是错的——锚点被接到了
> `compact_through_seq`，而那个值**不参与 prompt 组装**，对健康用户零缓存收益。
> 真正逐轮前移的滑动窗口在 `worker.py:3102-3107` 的 optional 窗口选择逻辑里。
>
> **完整更正、正确落点、以及必须补的验收测试见**
> `docs/superpowers/specs/2026-07-29-runtime-v2-context-cache-review.md` §11。
> **重做时不要照抄本文件的 Task 4。**
>
> 教训：Task 4 的测试只断言了「锚点保持/前移」和「边界查询被跳过」，
> 这些在接错线的情况下**照样全绿**。缺的那条是——
> **连续两回合的 prompt 前缀逐字节相同**。那才是验收标准本身。


> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 V2 chat 的 verbatim tail 从「每轮重算的滑动窗口」改成「持久化锚点 + 滞后前移」，让连续多轮请求的 prompt 前缀逐字节稳定；并顺带修掉一个让缓存效果无法被观测的计数器缺陷。

**Architecture:** 新增一张 per-user 的 `v2_chat_tail_anchor` 表存 tail 起点 seq。热路径不再每轮调 `db.chat_recent_genuine_turn_boundary_seq` 重算边界，而是读锚点；只有当锚点之后的真实用户轮数超过滞后阈值时才把锚点前移一次（hysteresis），使绝大多数回合是纯追加。tail 因此在 40–60 轮之间浮动，换取前缀稳定。

**为什么不额外做预算守卫：** 起初计划给锚点上限加一层「按 `ModelPromptLimit` 动态收敛」的守卫，理由是小窗口路由会被撑爆。复核 `prompt_frontier.py:967-972` 后否掉了：`plan_provider_round` 把工具 schema 登记为 `required=False, priority=1`，预算不足时它会被自动省略；真正 `required` 的只有 `message_context`。而 tail 变长确实会增大 `message_context`，但 `worker.py:9937-9998` 已有成熟的降级路径（exhausted → 收紧 `safe_through_seq` → 重试 → 仍不行才终态失败）。再加一层守卫属于重复防御，且会与既有降级逻辑抢位置。

**Tech Stack:** Python 3 / FastAPI / psycopg (连接池) / Alembic / pytest。全部改动在 `backend/model_api_runtime/v2/` 与 `backend/db.py`、`backend/alembic/versions/`。

## Global Constraints

- **不要自行 `git add` / `git commit`。** 本仓库规则：commit 必须由用户显式要求。每个 Task 末尾的 commit 步骤只有在用户明确说「提交」时才执行；否则把改动留在工作树并向用户报告。
- **Alembic revision id 必须 ≤ 32 字符**，且 `down_revision` 必须指向当前 head `0067_voice_turn_state`（多头会让部署静默降级）。
- **跑测试前必须起 PostgreSQL**，否则 conftest 会静默跳过大量 DB 用例、用「全绿」冒充通过：
  `docker run -d --name feedling-test-pg -e POSTGRES_PASSWORD=test -p 55432:5432 postgres:16`
  （本次执行环境该容器已在运行，无需重复创建。）
- **⚠️ 必须导出 `DATABASE_URL`，否则本 plan 涉及的测试文件会被整份 skip 而不是失败：**
  `export DATABASE_URL="postgresql://postgres:test@127.0.0.1:55432/postgres"`
  `tests/test_v2_turn_metrics.py` 与新建的 `tests/test_v2_tail_anchor_store.py` 顶部都有
  `pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), ...)`。**看到
  `skipped` 而不是 `passed` 就说明这个变量没设——那不是通过。** 每次跑测试都要确认输出里
  是 `passed` 而非 `skipped`。
- **L1 测试命令**（`docs/testing/TESTING.md` 的标准命令）：
  `python -m pytest tests/ -q --ignore=tests/e2e_model_api_test.py --ignore=tests/test_api.py`
  **本 worktree 已实测基线（commit `c353b496`，2026-07-29）：
  `7166 passed, 1 skipped, 9 xfailed, 0 failed`，耗时约 5 分 22 秒。
  没有任何 pre-existing 红** —— 所以出现任何 FAILED 都是本次改动引入的，不得以
  「本来就红」搪塞。（早期文档里「2440 passed / 7 红」的说法已过时，勿引用。）
- `python -m pyflakes backend/model_api_runtime` 在基线 `c353b496` 上**恒定输出下面这 4 条**
  （Task 1 已用 stash 前后对比验证过与本 plan 无关，**不要去修**，只要数量和内容不变即可）：
  - `serve_worker.py:1427 'proactive.controls_v2.WakeControlDecisionV2' imported but unused`
  - `serve_worker.py:2307:35 undefined name 'Any'`
  - `kill_switch.py:75 'global _cached_value' is unused`
  - `kill_switch.py:75 'global _cached_at' is unused`

  （早期文档里「恒剩 1 条」的说法已过时。⚠️ 其中 `undefined name 'Any'` 疑似真实缺陷，
  已记入 ledger 待单独处理，本 plan 不碰。）
- **`chat_messages.seq` 是全表共享的 `BIGINT GENERATED ALWAYS AS IDENTITY`**，不是每用户从 1 开始的序号。任何测试都必须用 `db.chat_seq_for_msg_id` 取真实 seq，绝不假设 `seq == index + 1`。
- **⚠️ `db.chat_recent_genuine_turn_boundary_seq` 在 `worker.py` 里有两个调用点，本 plan 只改其中一个：**
  - **6249-6259（wake lane，`max_turns=_WAKE_TAIL_MAX_TURNS`）—— 绝对不要动。**
  - **8782-8791（chat lane，`max_turns=_CHAT_TAIL_MAX_TURNS`）—— 这才是要改的。**

  两段代码结构几乎逐行相同（都是 `if seq_context:` → `chat_max_seq` → `boundary_seq` →
  `compact_through_seq = oldest_retained_seed - 1`），**唯一可靠的判别标志是 `max_turns=`
  后面那个常量名**。改之前先用
  `grep -n "chat_recent_genuine_turn_boundary_seq" backend/model_api_runtime/v2/worker.py`
  确认行号，再看上下文里的常量名。wake lane 的 tail 只有 16 轮、且唤醒是无用户消息的
  背景回合，锚点语义不适用。
- **本 plan 的所有行号基于基线 commit `2b44a267`。** 若 worktree 基线不同，先用 grep
  重新定位再动手，不要盲信行号。
- **⚠️ 新建任何表都必须同时登记进 TEE 影子库注册表**，否则 `tests/test_tee_registry_guard_enforced.py`
  等守卫测试会红（本 plan 初稿漏了这条，Task 3 实施时才发现）。需要两处：
  `backend/tee_shadow/table_registry.py` 的 `REGISTRY` 加一条 `Entry(SNAPSHOT, "<中文说明>")`，
  以及 `backend/alembic_tee/versions/` 下加一个对应的 TEE 侧迁移。
- **不要把锚点塞进 `v2_conversation_summary`。** 该表有 BEFORE UPDATE trigger `trg_v2_segmented_summary_head`，会拒绝「version 变了但 `materialized_segment_ids` 没变」的更新（`ERRCODE 55000`）。锚点更新不携带 canonical provenance，必然撞上它。
- 本 plan **不包含** `v2_summary_frontier_exhausted` 故障的修复（见 `docs/superpowers/specs/2026-07-29-runtime-v2-context-cache-review.md` §9.4）。那件事根因未确认、且优先级更高，**应先独立立案**。⚠️ 次序风险：本 plan 会让 tail 更长、摘要推进更慢，理论上可能加剧 §9.4。**Task 4 上线前应确认 §9.4 已定位或已缓解。**

---

## File Structure

| 文件 | 责任 | 动作 |
| --- | --- | --- |
| `backend/alembic/versions/0068_v2_chat_tail_anchor.py` | 建 `v2_chat_tail_anchor` 表 | 创建 |
| `backend/model_api_runtime/v2/tail_anchor.py` | **纯**锚点推进策略（无 I/O，可单测） | 创建 |
| `backend/model_api_runtime/v2/jobs_store.py` | 锚点的读写（CAS-free，单行 upsert） | 修改 |
| `backend/model_api_runtime/v2/worker.py` | 热路径接锚点；flush 兜底 | 修改 |
| `backend/db.py` | 锚点后的真实用户轮数计数 | 修改 |
| `tests/test_v2_tail_anchor_unit.py` | 纯策略单测（不需要 PG） | 创建 |
| `tests/test_v2_tail_anchor_store.py` | 锚点存取 + 计数（需要 PG） | 创建 |
| `tests/test_v2_turn_metrics.py` | 观测兜底用例 | 修改 |

新建 `tail_anchor.py` 而不是把逻辑塞进 `worker.py`：`worker.py` 已经 10,831 行，而锚点推进是一个可以完全脱离 DB/provider 测试的纯函数——与 `summary_frontier.py`／`prompt_frontier.py`「纯模块 + 注入 I/O」的既有分层一致。

---

## Task 1: 修复 exhaustion 计数器的观测缺陷

**背景（实现者需要知道）：** prod 上 `v2_turn_metrics.prompt_frontier_exhaustion_count` 恒为 0，但按 `status` 统计确实发生过 `turn_failed:prompt_frontier_exhausted`。落库 SQL（`jobs_store.py:3436-3480`）和 `flush()` 的传参都是对的，问题是 `record_prompt_frontier_exhaustion()` 的埋点分散在多条抛出路径上（`worker.py:7163/7209/9937/9997` + `tool_loop.py:649`，行号基于基线 commit `2b44a267`），覆盖不全。与其继续追加埋点，不如在 `flush()` 里以终态 status 为准做一次兜底——status 是唯一确定的事实来源。

先做这个任务，因为后面几个任务的验证都要靠这个计数器可信。

**Files:**
- Modify: `backend/model_api_runtime/v2/worker.py`（`TurnMetrics.flush`，约 2038 行）
- Test: `tests/test_v2_turn_metrics.py`

**Interfaces:**
- Consumes: `v2_prompt_frontier.PromptFrontierExhausted.code`（值为 `"prompt_frontier_exhausted"`）
- Produces: 不变量「终态 status 以 `prompt_frontier_exhausted` 结尾 ⇒ 落库 count ≥ 1」

- [ ] **Step 1: 写失败测试**

在 `tests/test_v2_turn_metrics.py` 末尾追加：

```python
def test_flush_backfills_exhaustion_count_from_terminal_status():
    """终态 status 说明 exhaustion 发生过；分散埋点漏记时，flush 必须兜底。

    prod 实证：status='turn_failed:prompt_frontier_exhausted' 的行
    prompt_frontier_exhaustion_count 仍为 0，导致该计数器不可用于判断
    「有没有触发过」。
    """
    from model_api_runtime.v2 import worker as v2_worker

    seed_user("u_tm_exh")
    tm = v2_worker.TurnMetrics(job_id=910001, user_id="u_tm_exh", lane="chat")
    assert tm.prompt_frontier_exhaustion_count == 0
    tm.flush(failed=True, status="turn_failed:prompt_frontier_exhausted")

    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT prompt_frontier_exhaustion_count, status "
            "FROM v2_turn_metrics WHERE job_id=%s",
            (910001,),
        ).fetchone()
    assert row is not None, "flush 必须落一行 metrics"
    assert row[0] == 1, f"终态 status={row[1]} 却记了 {row[0]} 次 exhaustion"


def test_flush_does_not_invent_exhaustion_for_unrelated_status():
    """兜底只认 exhaustion 终态，绝不给普通失败凭空记一次。"""
    from model_api_runtime.v2 import worker as v2_worker

    seed_user("u_tm_exh2")
    tm = v2_worker.TurnMetrics(job_id=910002, user_id="u_tm_exh2", lane="chat")
    tm.flush(failed=True, status="turn_failed:providererror")

    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT prompt_frontier_exhaustion_count FROM v2_turn_metrics "
            "WHERE job_id=%s",
            (910002,),
        ).fetchone()
    assert row[0] == 0


def test_flush_preserves_a_real_recorded_count():
    """已经埋点记过的真实次数不能被兜底覆盖成 1。"""
    from model_api_runtime.v2 import worker as v2_worker

    seed_user("u_tm_exh3")
    tm = v2_worker.TurnMetrics(job_id=910003, user_id="u_tm_exh3", lane="chat")
    tm.record_prompt_frontier_exhaustion()
    tm.record_prompt_frontier_exhaustion()
    tm.record_prompt_frontier_exhaustion()
    tm.flush(failed=True, status="turn_failed:prompt_frontier_exhausted")

    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT prompt_frontier_exhaustion_count FROM v2_turn_metrics "
            "WHERE job_id=%s",
            (910003,),
        ).fetchone()
    assert row[0] == 3
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python -m pytest tests/test_v2_turn_metrics.py -k exhaustion -v
```
预期：`test_flush_backfills_exhaustion_count_from_terminal_status` FAIL，断言 `0 == 1`。
另外两个应当已经 PASS（它们描述的是必须保持的现状）。

- [ ] **Step 3: 实现兜底**

在 `backend/model_api_runtime/v2/worker.py` 的 `TurnMetrics.flush` 中，`self._flushed = True` 之后、`latency_ms = ...` 之前插入：

```python
        # 终态 status 是 exhaustion 是否发生过的唯一确定事实来源。
        # record_prompt_frontier_exhaustion() 的埋点分散在多条抛出路径上
        # （_preflight_adaptive_builder / _run_wake / tool_loop 的 planner），
        # prod 实测存在漏记：status 为 exhausted 而 count 仍为 0，使该列无法
        # 用于判断是否触发过。以 status 兜底一次，且绝不覆盖已记录的真实次数。
        if (
            self.prompt_frontier_exhaustion_count == 0
            and str(status).endswith(
                v2_prompt_frontier.PromptFrontierExhausted.code
            )
        ):
            self.prompt_frontier_exhaustion_count = 1
```

`v2_prompt_frontier` 在 `worker.py` 顶部已 import，无需新增。

- [ ] **Step 4: 跑测试确认通过**

```bash
python -m pytest tests/test_v2_turn_metrics.py -v
```
预期：全部 PASS。

- [ ] **Step 5: 回归 + 静态检查**

```bash
python -m pytest tests/test_v2_worker.py tests/test_v2_turn_metrics.py -q
python -m pyflakes backend/model_api_runtime
```
预期：测试全绿；pyflakes 只剩那 1 条既有 unused。

- [ ] **Step 6: 提交（仅在用户明确要求时执行）**

```bash
git add backend/model_api_runtime/v2/worker.py tests/test_v2_turn_metrics.py
git commit -m "fix(v2): backfill prompt-frontier exhaustion count from terminal status"
```

---

## Task 2: tail 锚点的纯推进策略

**背景：** 当前 `worker.py:8685` 每轮都用当前 `through_seq` 重算「最近 40 轮的最老 seed seq」，窗口逐轮前移，导致摘要段之后的前缀每轮变化。新策略是：锚点持久化，只有当锚点之后的真实用户轮数超过 `max_turns_before_advance` 时才前移一次，前移到「最近 `target_turns` 轮」的边界。

这个任务只做**纯函数**，不碰 DB，便于单测覆盖全部边界。

**Files:**
- Create: `backend/model_api_runtime/v2/tail_anchor.py`
- Test: `tests/test_v2_tail_anchor_unit.py`

**Interfaces:**
- Consumes: 无（stdlib only）
- Produces:
  - `TailAnchorDecision`（frozen dataclass）：`anchor_seq: int`、`advanced: bool`、`reason: str`
  - `decide_anchor(*, current_anchor: int | None, turns_after_anchor: int, boundary_seq_for_target: int | None, target_turns: int, max_turns_before_advance: int) -> TailAnchorDecision`
  - 常量 `DEFAULT_TARGET_TURNS = 40`、`DEFAULT_MAX_TURNS_BEFORE_ADVANCE = 60`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_v2_tail_anchor_unit.py`：

```python
"""tail 锚点推进策略的纯单测（无 DB、无 provider）。

锚点的意义：verbatim tail 的起点 seq。它在多数回合保持不变，使 prompt 前缀
逐字节稳定、provider prompt cache 可复用；只有累积轮数越过滞后阈值时才前移
一次，把 prefix 的一次性失效换来长期稳定。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import tail_anchor


def test_no_anchor_yet_adopts_target_boundary():
    d = tail_anchor.decide_anchor(
        current_anchor=None,
        turns_after_anchor=0,
        boundary_seq_for_target=5000,
        target_turns=40,
        max_turns_before_advance=60,
    )
    assert d.anchor_seq == 5000
    assert d.advanced is True
    assert d.reason == "bootstrap"


def test_under_threshold_reuses_anchor_verbatim():
    """滞后区内绝不动锚点——这正是缓存命中的来源。"""
    d = tail_anchor.decide_anchor(
        current_anchor=5000,
        turns_after_anchor=59,
        boundary_seq_for_target=7000,
        target_turns=40,
        max_turns_before_advance=60,
    )
    assert d.anchor_seq == 5000
    assert d.advanced is False
    assert d.reason == "hysteresis_hold"


def test_crossing_threshold_advances_once_to_target():
    d = tail_anchor.decide_anchor(
        current_anchor=5000,
        turns_after_anchor=60,
        boundary_seq_for_target=7000,
        target_turns=40,
        max_turns_before_advance=60,
    )
    assert d.anchor_seq == 7000
    assert d.advanced is True
    assert d.reason == "threshold_advance"


def test_anchor_never_moves_backwards():
    """seq 单调；一个更旧的边界绝不能把锚点拉回去（会让 tail 变长且前缀重排）。"""
    d = tail_anchor.decide_anchor(
        current_anchor=7000,
        turns_after_anchor=99,
        boundary_seq_for_target=6000,
        target_turns=40,
        max_turns_before_advance=60,
    )
    assert d.anchor_seq == 7000
    assert d.advanced is False
    assert d.reason == "boundary_not_newer"


def test_missing_boundary_holds_anchor():
    """用户没有足够的真实用户轮（boundary 查不到）时保持不变，绝不清空。"""
    d = tail_anchor.decide_anchor(
        current_anchor=5000,
        turns_after_anchor=80,
        boundary_seq_for_target=None,
        target_turns=40,
        max_turns_before_advance=60,
    )
    assert d.anchor_seq == 5000
    assert d.advanced is False
    assert d.reason == "no_boundary"


def test_bootstrap_without_boundary_yields_no_anchor():
    d = tail_anchor.decide_anchor(
        current_anchor=None,
        turns_after_anchor=0,
        boundary_seq_for_target=None,
        target_turns=40,
        max_turns_before_advance=60,
    )
    assert d.anchor_seq == 0
    assert d.advanced is False
    assert d.reason == "no_boundary"


@pytest.mark.parametrize(
    "target,max_before",
    [(0, 60), (40, 39), (-1, 60), (40, 0)],
)
def test_invalid_limits_rejected(target, max_before):
    """max_turns_before_advance 必须严格大于 target_turns，否则没有滞后区，
    退化回逐轮滑动窗口（即当前 bug）。"""
    with pytest.raises(ValueError):
        tail_anchor.decide_anchor(
            current_anchor=5000,
            turns_after_anchor=1,
            boundary_seq_for_target=6000,
            target_turns=target,
            max_turns_before_advance=max_before,
        )


def test_defaults_have_a_real_hysteresis_band():
    assert (
        tail_anchor.DEFAULT_MAX_TURNS_BEFORE_ADVANCE
        > tail_anchor.DEFAULT_TARGET_TURNS
    )
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python -m pytest tests/test_v2_tail_anchor_unit.py -v
```
预期：全部 FAIL（`ModuleNotFoundError: model_api_runtime.v2.tail_anchor`）。

- [ ] **Step 3: 实现纯模块**

创建 `backend/model_api_runtime/v2/tail_anchor.py`：

```python
"""Hysteresis anchor for the V2 verbatim chat tail.

The tail's start seq used to be recomputed every turn from the newest
``max_turns`` genuine user turns, so the window slid forward on every new
message and the prompt prefix after the summary changed each round — provider
prompt caches require an exact prefix match, so that is a guaranteed miss.

This module keeps the start seq *pinned* until enough new turns accumulate,
then advances it once.  Most turns are therefore pure appends behind a
byte-identical prefix; the cost is that the verbatim tail floats between
``target_turns`` and ``max_turns_before_advance``.

Deliberately pure: no DB, no envelope, no provider.  The advance policy can be
tested without a running service, matching ``summary_frontier``/
``prompt_frontier``'s layering.
"""

from __future__ import annotations

from dataclasses import dataclass


DEFAULT_TARGET_TURNS = 40
DEFAULT_MAX_TURNS_BEFORE_ADVANCE = 60


@dataclass(frozen=True)
class TailAnchorDecision:
    """Where the verbatim tail starts this turn, and whether it just moved."""

    anchor_seq: int
    advanced: bool
    reason: str


def decide_anchor(
    *,
    current_anchor: int | None,
    turns_after_anchor: int,
    boundary_seq_for_target: int | None,
    target_turns: int = DEFAULT_TARGET_TURNS,
    max_turns_before_advance: int = DEFAULT_MAX_TURNS_BEFORE_ADVANCE,
) -> TailAnchorDecision:
    """Pin the tail start until the hysteresis band is crossed.

    ``boundary_seq_for_target`` is the oldest seed seq among the newest
    ``target_turns`` genuine user turns (what the old per-turn computation
    returned).  It is consulted only when an advance is actually due.
    """

    target = int(target_turns)
    ceiling = int(max_turns_before_advance)
    if target <= 0 or ceiling <= target:
        raise ValueError(
            "max_turns_before_advance must be greater than target_turns, "
            "and target_turns must be positive"
        )

    boundary = (
        int(boundary_seq_for_target)
        if boundary_seq_for_target is not None
        else None
    )

    if current_anchor is None:
        if boundary is None:
            return TailAnchorDecision(0, False, "no_boundary")
        return TailAnchorDecision(boundary, True, "bootstrap")

    anchor = int(current_anchor)
    if int(turns_after_anchor) < ceiling:
        return TailAnchorDecision(anchor, False, "hysteresis_hold")
    if boundary is None:
        return TailAnchorDecision(anchor, False, "no_boundary")
    # seq is monotonic; an older boundary would lengthen the tail AND reorder
    # the prefix — strictly worse than holding.
    if boundary <= anchor:
        return TailAnchorDecision(anchor, False, "boundary_not_newer")
    return TailAnchorDecision(boundary, True, "threshold_advance")
```

- [ ] **Step 4: 跑测试确认通过**

```bash
python -m pytest tests/test_v2_tail_anchor_unit.py -v
```
预期：全部 PASS。

- [ ] **Step 5: 提交（仅在用户明确要求时执行）**

```bash
git add backend/model_api_runtime/v2/tail_anchor.py tests/test_v2_tail_anchor_unit.py
git commit -m "feat(v2): add pure hysteresis policy for chat tail anchor"
```

---

## Task 3: 锚点持久化（迁移 + store）

**Files:**
- Create: `backend/alembic/versions/0068_v2_chat_tail_anchor.py`
- Modify: `backend/model_api_runtime/v2/jobs_store.py`（文件末尾追加两个函数）
- Test: `tests/test_v2_tail_anchor_store.py`

**Interfaces:**
- Consumes: Task 2 的 `tail_anchor.TailAnchorDecision`（本任务不直接用，只存整数）
- Produces:
  - `jobs_store.get_chat_tail_anchor(user_id: str) -> int | None`
  - `jobs_store.set_chat_tail_anchor(user_id: str, anchor_seq: int) -> None`（单调 upsert：只增不减）

- [ ] **Step 1: 写失败测试**

创建 `tests/test_v2_tail_anchor_store.py`：

```python
"""v2_chat_tail_anchor 的读写（需要 PostgreSQL fixture）。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db
from model_api_runtime.v2 import jobs_store

from conftest import seed_user

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DB-backed tail-anchor tests require the PostgreSQL test fixture",
)


def test_absent_anchor_reads_as_none():
    seed_user("u_anchor_1")
    assert jobs_store.get_chat_tail_anchor("u_anchor_1") is None


def test_set_then_get_roundtrip():
    seed_user("u_anchor_2")
    jobs_store.set_chat_tail_anchor("u_anchor_2", 4242)
    assert jobs_store.get_chat_tail_anchor("u_anchor_2") == 4242


def test_anchor_is_monotonic_never_regresses():
    """并发回合可能带着旧值回写；存储层必须自己保证只增不减，
    否则 tail 会突然变长、前缀重排，正是本次优化要消灭的现象。"""
    seed_user("u_anchor_3")
    jobs_store.set_chat_tail_anchor("u_anchor_3", 9000)
    jobs_store.set_chat_tail_anchor("u_anchor_3", 8000)
    assert jobs_store.get_chat_tail_anchor("u_anchor_3") == 9000


def test_anchor_advances_forward():
    seed_user("u_anchor_4")
    jobs_store.set_chat_tail_anchor("u_anchor_4", 100)
    jobs_store.set_chat_tail_anchor("u_anchor_4", 500)
    assert jobs_store.get_chat_tail_anchor("u_anchor_4") == 500


def test_anchor_row_is_deleted_with_the_user():
    """FK ON DELETE CASCADE：删号不留孤儿（v2_user_allowlist 曾因缺 FK 留过孤儿）。"""
    seed_user("u_anchor_5")
    jobs_store.set_chat_tail_anchor("u_anchor_5", 777)
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM users WHERE user_id=%s", ("u_anchor_5",))
        row = conn.execute(
            "SELECT count(*) FROM v2_chat_tail_anchor WHERE user_id=%s",
            ("u_anchor_5",),
        ).fetchone()
    assert row[0] == 0
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python -m pytest tests/test_v2_tail_anchor_store.py -v
```
预期：FAIL（表不存在 / `jobs_store` 无这两个函数）。

- [ ] **Step 3: 写迁移**

创建 `backend/alembic/versions/0068_v2_chat_tail_anchor.py`：

```python
"""v2 chat tail anchor: pinned verbatim-tail start seq per user.

Kept in its own table rather than as a column on v2_conversation_summary:
that head row is guarded by trg_v2_segmented_summary_head, which rejects any
update whose version changes without new canonical provenance (ERRCODE 55000).
An anchor advance carries no segment provenance, so it would always trip it.
"""
from alembic import op


revision = "0068_v2_chat_tail_anchor"
down_revision = "0067_voice_turn_state"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS v2_chat_tail_anchor (
            user_id    TEXT PRIMARY KEY
                       REFERENCES users(user_id) ON DELETE CASCADE,
            anchor_seq BIGINT NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_v2_chat_tail_anchor_seq CHECK (anchor_seq >= 0)
        )
        """
    )


def downgrade():
    op.execute("DROP TABLE IF EXISTS v2_chat_tail_anchor")
```

- [ ] **Step 4: 应用迁移并确认单头**

```bash
cd backend && python -m alembic heads
```
预期：只输出一个 head，且为 `0068_v2_chat_tail_anchor`。若出现两行，说明撞了多头，必须先解决再继续。

```bash
cd backend && python -m alembic upgrade head
```

- [ ] **Step 5: 实现 store 函数**

在 `backend/model_api_runtime/v2/jobs_store.py` 末尾追加：

```python
def get_chat_tail_anchor(user_id: str) -> int | None:
    """Pinned verbatim-tail start seq, or None when this user has no anchor yet."""
    with _pool().connection() as conn:
        row = conn.execute(
            "SELECT anchor_seq FROM v2_chat_tail_anchor WHERE user_id=%s",
            (str(user_id),),
        ).fetchone()
    return int(row[0]) if row is not None else None


def set_chat_tail_anchor(user_id: str, anchor_seq: int) -> None:
    """Advance the anchor.  Monotonic by construction: a concurrent turn
    holding a stale value can only lose, never drag the anchor backwards
    (a regressing anchor would lengthen the tail and reorder the cached
    prefix — precisely what the anchor exists to prevent)."""
    value = max(0, int(anchor_seq))
    with _pool().connection() as conn:
        conn.execute(
            "INSERT INTO v2_chat_tail_anchor (user_id, anchor_seq) "
            "VALUES (%s,%s) "
            "ON CONFLICT (user_id) DO UPDATE SET "
            "anchor_seq=GREATEST(v2_chat_tail_anchor.anchor_seq, "
            "EXCLUDED.anchor_seq), "
            "updated_at=now()",
            (str(user_id), value),
        )
```

- [ ] **Step 6: 跑测试确认通过**

```bash
python -m pytest tests/test_v2_tail_anchor_store.py -v
```
预期：全部 PASS。

- [ ] **Step 7: 提交（仅在用户明确要求时执行）**

```bash
git add backend/alembic/versions/0068_v2_chat_tail_anchor.py \
        backend/model_api_runtime/v2/jobs_store.py \
        tests/test_v2_tail_anchor_store.py
git commit -m "feat(v2): persist chat tail anchor with monotonic upsert"
```

---

## Task 4: 热路径接入锚点

**背景：** 这是本 plan 的主改动。`worker.py:8782-8791` 目前是：

```python
        if seq_context:
            through_seq = await asyncio.to_thread(db.chat_max_seq, user_id)
            oldest_retained_seed = await asyncio.to_thread(
                db.chat_recent_genuine_turn_boundary_seq,
                user_id,
                max_turns=_CHAT_TAIL_MAX_TURNS,
                through_seq=through_seq,
            )
            if oldest_retained_seed is not None and oldest_retained_seed > 1:
                compact_through_seq = oldest_retained_seed - 1
```

改成：先读锚点；只有当锚点之后的真实用户轮数越过滞后阈值时才查边界并前移。`compact_through_seq` 继续跟着最终锚点走（语义不变：锚点之前的消息才可被压缩进摘要）。

**Files:**
- Modify: `backend/db.py`（新增一个计数函数）
- Modify: `backend/model_api_runtime/v2/worker.py:8782-8791`（**chat lane**）及常量区（`_CHAT_TAIL_MAX_TURNS` 定义在 458 行）
- Test: `tests/test_v2_tail_anchor_store.py`（追加热路径行为用例）

**Interfaces:**
- Consumes: `tail_anchor.decide_anchor`（Task 2）、`jobs_store.get_chat_tail_anchor` / `set_chat_tail_anchor`（Task 3）
- Produces: `db.chat_genuine_turn_count_after_seq(user_id: str, *, after_seq: int, through_seq: int) -> int`

- [ ] **Step 1: 写失败测试**

在 `tests/test_v2_tail_anchor_store.py` 末尾追加：

```python
def _append(uid, role, text):
    """插入一条真实 chat 行并返回它的真实 seq。

    seq 是全表共享的 identity 列，绝不等于该用户自己的第几条消息。
    调用形态照抄 tests/test_v2_prompt_invariant.py:68（本进程没有活的 enclave，
    所以用明文形状的 envelope）。
    """
    import core_store

    store = core_store.get_store(uid)
    envelope = {
        "v": 1,
        "body_ct": text,
        "nonce": "n",
        "K_user": "k_test",
        "id": f"{uid}-{text}",
    }
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
    seed_user("u_anchor_cnt")
    first = _append("u_anchor_cnt", "user", "m1")
    _append("u_anchor_cnt", "assistant", "r1")
    _append("u_anchor_cnt", "user", "m2")
    _append("u_anchor_cnt", "user", "m3")
    through = db.chat_max_seq("u_anchor_cnt")

    assert db.chat_genuine_turn_count_after_seq(
        "u_anchor_cnt", after_seq=first, through_seq=through
    ) == 2
    assert db.chat_genuine_turn_count_after_seq(
        "u_anchor_cnt", after_seq=0, through_seq=through
    ) == 3
```

上面的 `_append` 已按 `tests/test_v2_prompt_invariant.py:68` 的真实调用形态写好
（`append_chat(role, kind, envelope, strict=True)`，返回带 `id`/`ts` 的 row），可直接使用。

- [ ] **Step 2: 跑测试确认失败**

```bash
python -m pytest tests/test_v2_tail_anchor_store.py -k genuine_turn_count -v
```
预期：FAIL（`db` 无 `chat_genuine_turn_count_after_seq`）。

- [ ] **Step 3: 实现计数函数**

在 `backend/db.py` 的 `chat_recent_genuine_turn_boundary_seq`（5572 行）之后追加：

```python
def chat_genuine_turn_count_after_seq(
    user_id: str,
    *,
    after_seq: int,
    through_seq: int,
) -> int:
    """Genuine user turns strictly after ``after_seq`` up to ``through_seq``.

    Same predicate as :func:`chat_recent_genuine_turn_boundary_seq` so the tail
    anchor's hysteresis counts exactly the rows that define its window.
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

- [ ] **Step 4: 跑测试确认通过**

```bash
python -m pytest tests/test_v2_tail_anchor_store.py -k genuine_turn_count -v
```
预期：PASS。

- [ ] **Step 5: 加常量**

在 `backend/model_api_runtime/v2/worker.py` 的 `_WAKE_TAIL_MAX_TURNS` 定义（461 行）之后追加：

```python
# 锚点滞后上限：verbatim tail 在 _CHAT_TAIL_MAX_TURNS 与本值之间浮动。
# 必须严格大于 _CHAT_TAIL_MAX_TURNS，否则没有滞后区、退化回逐轮滑动窗口。
_CHAT_TAIL_ANCHOR_MAX_TURNS = _positive_int_env(
    "FEEDLING_V2_CHAT_TAIL_ANCHOR_MAX_TURNS", "60"
)
if _CHAT_TAIL_ANCHOR_MAX_TURNS <= _CHAT_TAIL_MAX_TURNS:
    _CHAT_TAIL_ANCHOR_MAX_TURNS = _CHAT_TAIL_MAX_TURNS + 20
```

- [ ] **Step 6: 改热路径**

把 `worker.py:8782-8791` 的整段替换为：

```python
        if seq_context:
            through_seq = await asyncio.to_thread(db.chat_max_seq, user_id)
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
            # 只有真要前移时才付这次边界查询；滞后区内一次 DB 往返都不做。
            boundary_seq = None
            if (
                stored_anchor is None
                or turns_after_anchor >= _CHAT_TAIL_ANCHOR_MAX_TURNS
            ):
                boundary_seq = await asyncio.to_thread(
                    db.chat_recent_genuine_turn_boundary_seq,
                    user_id,
                    max_turns=_CHAT_TAIL_MAX_TURNS,
                    through_seq=through_seq,
                )
            decision = v2_tail_anchor.decide_anchor(
                current_anchor=stored_anchor,
                turns_after_anchor=turns_after_anchor,
                boundary_seq_for_target=boundary_seq,
                target_turns=_CHAT_TAIL_MAX_TURNS,
                max_turns_before_advance=_CHAT_TAIL_ANCHOR_MAX_TURNS,
            )
            if decision.advanced and decision.anchor_seq > 0:
                await asyncio.to_thread(
                    jobs_store.set_chat_tail_anchor,
                    user_id,
                    decision.anchor_seq,
                )
            oldest_retained_seed = (
                decision.anchor_seq if decision.anchor_seq > 0 else None
            )
            if oldest_retained_seed is not None and oldest_retained_seed > 1:
                compact_through_seq = oldest_retained_seed - 1
```

并在 `worker.py` 顶部 import 区（`from model_api_runtime.v2 import summary_frontier as v2_summary_frontier` 附近，约 86 行）追加：

```python
from model_api_runtime.v2 import tail_anchor as v2_tail_anchor
```

- [ ] **Step 7: 跑相关回归**

```bash
python -m pytest tests/test_v2_prompt_invariant.py tests/test_v2_worker.py \
                 tests/test_v2_tail_anchor_store.py \
                 tests/test_v2_summary_watermark_seq.py -q
```
预期：全绿。`test_v2_prompt_invariant.py` 尤其关键——它守的是「watermark 之后的每条消息都必须逐字出现在 tail 里」这个不变量，锚点改动最可能撞的就是它。

- [ ] **Step 8: 跑全量 L1**

```bash
python -m pytest tests/ -q --ignore=tests/e2e_model_api_test.py --ignore=tests/test_api.py
python -m pyflakes backend/model_api_runtime backend/db.py
```
预期：约 2440 passed / 7 个 pre-existing 红（与改动前基线一致，不得新增红）；pyflakes 只剩既有那 1 条。

- [ ] **Step 9: 提交（仅在用户明确要求时执行）**

```bash
git add backend/db.py backend/model_api_runtime/v2/worker.py \
        tests/test_v2_tail_anchor_store.py
git commit -m "perf(v2): pin chat tail anchor with hysteresis to stabilize prompt prefix"
```

---

## 上线与验证

按 `docs/testing/TESTING.md` §2，本次改动类别为「后端逻辑 + schema」，DoD 需要 L1 全绿 + 迁移单头。

**部署到 test：** push `test` 分支 → CI `deploy-test-cvm`。

**上线后的验证口径**（改动前先取一次同口径基线，48h 后复测）：

```sql
-- 单轮 turn 的跨 turn 缓存命中率：这是本次优化的直接指标
-- 改动前基线（prod 2026-07-29，30 天窗口）：<1min 间隔 35.8%，1-5min 22.5%
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

**判定标准：**
- `<1min` 桶命中率显著高于 35.8%（目标 80%+），`write_per_read` 明显低于 1.03；
- `prompt_frontier_exhaustion_count` 从此与 `status` 一致（Task 1 之后可直接用它告警）；
- 全量 `turn_failed` 比例不得上升。

**回滚：** 设 `FEEDLING_V2_CHAT_TAIL_ANCHOR_MAX_TURNS` 使其不大于 `FEEDLING_V2_CHAT_TAIL_MAX_TURNS` 即可让滞后区退化（Task 5 Step 5 的钳制会把它抬回 +20，因此真正的回滚是回退 Task 5 的 commit）。迁移不需要回滚——空表无副作用。

## 本 plan 不做的

- **`v2_summary_frontier_exhausted` 故障修复**（spec §9.4）：根因未确认，需独立立案，且应排在本 plan 之前或并行确认。
- **常驻用户画像块**（spec §5.2 / P1）：涉及产品决策与 enclave 读平面，范围独立，单独出 plan。
- **token 估算比率校准**（spec §5.3 / P2）：需按 provider 家族做校准实验，不能盲调。
- **`chat_history_search` 工具**（spec §5.5 / P3）。
- **16k 路由 80% 失败的真因**：按 status 拆分主要是 `turn_failed:providererror`（48 次），
  不是预算问题。需独立排查，且失败率要用去重口径复核（现有数字来自 join
  `model_api_routes` 的查询，多路由用户会被放大）。
- **工具 schema 在小窗口路由上被静默省略**：`tool_schemas_component(required=False)`
  意味着预算紧张时模型可能根本拿不到工具，且没有任何显式信号。值得单独确认是否需要
  可观测性或显式降级提示。
