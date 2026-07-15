# Batch 3 — 记忆读桶快照 + P5 并发基线 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** resident 二次上传记忆材料时复用现有桶/线索并语义去重(A4);update_identity job 带并发基线,全量替换撞车时不盲覆盖(P5)。

**Architecture:** A4 = consumer 在 distill job 开始时拉一次记忆快照(桶+线索+卡摘要),摘要喂 fact_write 现有的 `known_memories`,桶/线索名走新的可选 `terms_note` 尾注(与 floor_note 同款,默认空 = cloud 字节级零变化)。P5 = identity 外层新增 `replaced_at`(仅 init/全量 replace 打戳),sealed job 建立时快照进 metadata,pending 返回,replace 动作带 `base_identity_replaced_at` 比对,不一致 409,consumer 拿新卡重摘一次。

**Tech Stack:** Python 3.12,pytest,uv。

## Global Constraints

- **cloud 零行为变化**:genesis prompts/worker 改动 = 新增可选参数,默认值下输出逐字节等价(有测试锁)。
- **契约 B / 优先 onboarding 成功率**:P5 冲突只在"job 之后发生过 init/全量 replace"才判;patch/nudge/signature 改动**不算冲突**;冲突的恢复动作是"重摘一次",不是失败退出。
- **绝不编造**;快照只在 job 开始拉**一次**,整 job 复用(不每 window 重拉)。
- 错误码变更/新增须登记 `docs/API_ERRORS.md`。
- 工作目录 `/Users/hx/Projects/io/feedling-mcp-batch2`,分支 `feat/onboarding-batch3-bucket-p5`(基于 feat/memory-floor-gentler @42eb76c9,合并时机归 hx)。
- DB 测试命令同 Batch 2(FEEDLING_TEST_PG + uv)。

---

## File Structure

- **Modify** `backend/genesis/prompts.py` — `fact_write_messages(..., terms_note: str = "")`(与 floor_note 并列,插在 floor_note 之前、防火墙标记前)。
- **Modify** `backend/genesis/worker.py` — `_fact_write` / `build_memory_output_from_fact_candidates` 透传 `terms_note`。
- **Modify** `tools/chat_resident_consumer.py` — 新增 `_resident_memory_snapshot()`(桶+线索+摘要,一次拉取);`_resident_extract_memories` 传 `known_memories` + `terms_note`;`_process_resident_distill_once` 的 identity 路带 `base_identity_replaced_at` + 409 冲突重摘一次。
- **Modify** `backend/identity/identity_core.py` — init/replace 打 `replaced_at` 戳。
- **Modify** `backend/genesis/service.py`(或 actions 链上全量 replace 的落点,执行时确认)— 同戳。
- **Modify** `backend/genesis/genesis_core.py` — `_resident_sealed_import` 快照 `base_identity_replaced_at` 进 metadata;`resident_pending` 返回它。
- **Modify** `backend/identity/actions.py` — `identity.replace` 动作接受可选 `base_identity_replaced_at`,不一致返回 409 `identity_base_stale`。
- **Modify** `docs/API_ERRORS.md` — 登记 `identity_base_stale`。
- **Create** `tests/test_genesis_terms_note.py`、`tests/test_identity_concurrency_baseline.py`;扩 `tests/test_resident_identity_distill.py`。

---

### Task 1: genesis `terms_note` 透传(cloud 默认零变化)

**Files:**
- Modify: `backend/genesis/prompts.py`(`fact_write_messages`)
- Modify: `backend/genesis/worker.py`(`_fact_write` 与 `build_memory_output_from_fact_candidates`)
- Test: `tests/test_genesis_terms_note.py`(新建)

**Interfaces:**
- Produces: `fact_write_messages(..., *, keep_all=False, floor_note="", terms_note: str = "")`;`build_memory_output_from_fact_candidates(..., floor_note="", terms_note: str = "")`。Task 2 的 consumer 传 `terms_note`。
- Consumes: Task 2 (Batch 2) 落的 floor_note 插入逻辑(anchored-splice:floor_note 插在 `\n防火墙:` 前,keep_all 后缀锚在尾部)。terms_note 插在 floor_note **之前**(桶引导语义上更靠近 FACT_WRITE_PROMPT 里的"桶名收敛"块)。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_genesis_terms_note.py
"""Batch 3 A4: fact_write 支持可选 terms_note(现有桶/线索快照);默认空 = cloud 输出逐字节等价。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from genesis import prompts


def test_default_output_unchanged():
    base = prompts.fact_write_messages([{"summary": "s"}])
    assert prompts.fact_write_messages([{"summary": "s"}], terms_note="") == base
    assert prompts.fact_write_messages([{"summary": "s"}], terms_note="   ") == base


def test_terms_note_inserted_before_firewall():
    note = "现有的桶:工作 / 协作方式 / IO项目(先复用,别造近义或中英重复桶)"
    msgs = prompts.fact_write_messages([{"summary": "s"}], terms_note=note)
    c = msgs[0]["content"]
    assert note in c
    assert c.index(note) < c.index("防火墙")


def test_terms_note_composes_with_floor_note_and_keep_all():
    terms = "现有的桶:工作"
    floor = "参考下限 15"
    both = prompts.fact_write_messages([{"summary": "s"}], keep_all=True,
                                       floor_note=floor, terms_note=terms)
    c = both[0]["content"]
    assert terms in c and floor in c and "长期档案" in c
    # terms 在 floor 之前;keep_all 后缀仍锚在尾部
    assert c.index(terms) < c.index(floor)
    ka_only = prompts.fact_write_messages([{"summary": "s"}], keep_all=True)
    assert c.endswith(ka_only[0]["content"][ka_only[0]["content"].index("★ 本块"):] if "★ 本块" in ka_only[0]["content"] else c[-10:])
```

(最后一个断言执行时以现有 `test_genesis_floor_note.py` 的 `_floor_insert` 手法为准:剥掉两个插入后与 keep_all-only 逐字节相等 —— 实现者读现网测试仿写,别照抄上面这行的粗糙版。)

- [ ] **Step 2: 跑失败** `uv run --python 3.12 --with-requirements backend/requirements.txt --with pytest python -m pytest tests/test_genesis_terms_note.py -q` → TypeError
- [ ] **Step 3: 实现** —— `fact_write_messages` 加 `terms_note: str = ""`;非空时与 floor_note 同款 anchored-splice 插在 `\n防火墙:` 前,顺序 `terms → floor`;keep_all 后缀保持尾部锚定;默认路径字节等价。worker 两处透传(签名加 `terms_note: str = ""`)。
- [ ] **Step 4: 跑过 + 回归** `... -m pytest tests/test_genesis_terms_note.py tests/test_genesis_floor_note.py -q`
- [ ] **Step 5: pyflakes + commit** `feat(genesis): optional terms_note (existing buckets/threads snapshot) on fact_write — cloud unchanged by default`

---

### Task 2: consumer 记忆快照(A4)

**Files:**
- Modify: `tools/chat_resident_consumer.py`(新增 `_resident_memory_snapshot`,放 `_resident_floor_note` 旁;`_resident_extract_memories` 接线)
- Test: 追加 `tests/test_resident_identity_distill.py`

**Interfaces:**
- Consumes: Task 1 的 `terms_note`;现成的 `_capture_get_json`(GET)与 `/v1/memory/buckets`、`/v1/memory/threads`;`/v1/memory/index`(POST,用 `_capture_post_json` —— 执行时 grep 实名,consumer 里已有 index 调用照抄取法);`build_memory_output_from_fact_candidates(..., known_memories=...)`(现有参数)。
- Produces: `_resident_memory_snapshot() -> tuple[str, list[str]]`(terms_note 文本, known summaries 列表)。

- [ ] **Step 1: 写失败测试(追加)**

```python
def test_memory_snapshot_composes_terms_and_known(monkeypatch):
    def fake_get(path, **kw):
        if path == "/v1/memory/buckets":
            return {"buckets": [{"name": "工作", "count": 3}, {"name": "协作方式", "count": 2}]}
        if path == "/v1/memory/threads":
            return {"threads": [{"name": "查证不猜"}]}
        return {}
    monkeypatch.setattr(crc, "_capture_get_json", fake_get)
    monkeypatch.setattr(crc, "_resident_memory_index_summaries",
                        lambda: ["hx 是 Teleport 前端", "hx 的红线:优先成功率"])
    terms, known = crc._resident_memory_snapshot()
    assert "工作" in terms and "协作方式" in terms and "查证不猜" in terms
    assert "复用" in terms          # 引导语:先复用,别造近义/中英重复桶
    assert known == ["hx 是 Teleport 前端", "hx 的红线:优先成功率"]


def test_memory_snapshot_empty_garden_returns_empty(monkeypatch):
    monkeypatch.setattr(crc, "_capture_get_json", lambda path, **kw: {})
    monkeypatch.setattr(crc, "_resident_memory_index_summaries", lambda: [])
    terms, known = crc._resident_memory_snapshot()
    assert terms == "" and known == []


def test_memory_snapshot_error_returns_empty(monkeypatch):
    def boom(path, **kw):
        raise RuntimeError("api down")
    monkeypatch.setattr(crc, "_capture_get_json", boom)
    monkeypatch.setattr(crc, "_resident_memory_index_summaries", lambda: [])
    terms, known = crc._resident_memory_snapshot()
    assert terms == "" and known == []
```

- [ ] **Step 2: 跑失败**
- [ ] **Step 3: 实现** —— `_resident_memory_index_summaries()`(POST index,取每卡 summary,截 200 条、每条 160 字;取法照抄 consumer 现有 index 调用);`_resident_memory_snapshot()`(桶/线索名 + 摘要;空花园/出错返 `("", [])`,零影响);`_resident_extract_memories` 开头拉**一次**快照,循环外复用:`known_memories=known`(传给 `build_memory_output_from_fact_candidates`)+ `terms_note=terms`。terms_note 文案含"先复用现有桶/线索,别造近义或中英重复桶(如已有「工作」别再造「Work」)"。
- [ ] **Step 4: 跑过 + commit** `feat(consumer): one-shot memory snapshot on resident distill — reuse buckets/threads + semantic dedup via known_memories (A4)`

---

### Task 3: identity `replaced_at` 戳(P5 前置)

**Files:**
- Modify: `backend/identity/identity_core.py`(init ~L162 与 replace ~L238 的 identity dict 各加 `"replaced_at": now`)
- Modify: 全量 replace 的 actions 落点(执行时 trace `identity.replace` 动作 → `genesis_service.replace_identity_preserving_anchor` 或等价,найти真实保存点加同戳;patch/nudge/relationship_days_set **不加**)
- Test: `tests/test_identity_concurrency_baseline.py`(新建)

**Interfaces:**
- Produces: identity 外层字段 `replaced_at: str`(ISO;仅 init/全量 replace 刷新)。Task 4/5 消费。

- [ ] **Step 1: 失败测试** —— init 后 `replaced_at` 非空;全量 replace 后变化;`identity.profile_patch` 后**不变**;`identity.dimension_nudge` 后**不变**。(用现有 identity 测试文件的 fixture 手法,执行时参考 `tests/test_identity_actions.py`。)
- [ ] **Step 2-4: TDD 循环 + commit** `feat(identity): replaced_at stamp — full init/replace only, patches don't move it (P5 baseline)`

---

### Task 4: sealed job 快照基线 + pending 返回(P5)

**Files:**
- Modify: `backend/genesis/genesis_core.py`(`_resident_sealed_import` metadata 加 `"base_identity_replaced_at"`;`resident_pending` 的 job dict 加同名字段)
- Test: 扩现有 sealed/pending 测试(执行时 grep `resident_sealed` 相关测试文件)

**Interfaces:**
- Consumes: Task 3 的 `replaced_at`(job 创建时读当前 identity,无卡则 `""`)。
- Produces: pending job dict 多一个 `base_identity_replaced_at: str`。

- [ ] TDD 循环 + commit `feat(genesis): stamp base_identity_replaced_at on sealed update_identity jobs + return via pending (P5)`

---

### Task 5: replace 动作并发检查 + consumer 冲突重摘(P5)

**Files:**
- Modify: `backend/identity/actions.py`(`identity.replace` 动作:可选 `base_identity_replaced_at`,与当前 `replaced_at` 不一致 → `{"error": "identity_base_stale"}, 409`;缺省不带 = 不检查,兼容现有调用)
- Modify: `docs/API_ERRORS.md`(登记 `identity_base_stale | 409 | replace 带 base 基线且期间发生过全量替换`)
- Modify: `tools/chat_resident_consumer.py`(`_process_resident_distill_once` 的 update_identity 路:action 带 job 的 `base_identity_replaced_at`;`execute_identity_actions` 抛 409/`identity_base_stale` 时:重拉 `_resident_existing_identity()` → 重跑 `_resident_derive_identity` 一次 → 用**刷新后的基线**重提;二次仍冲突则放弃 + log.error,不无限)
- Test: 扩 `tests/test_identity_concurrency_baseline.py`(backend 409 路)+ `tests/test_resident_identity_distill.py`(consumer 重摘一次、二次冲突放弃)

**Interfaces:**
- Consumes: Task 3 `replaced_at`、Task 4 `base_identity_replaced_at`。
- 关键语义(全局约束的落地):**只有全量 init/replace 移动基线** → 用户 job 挂起期间改签名/patch 不触发冲突;真冲突时新一轮摘取以最新卡为 existing(merge 语义 = spec 的"supersede")。

- [ ] TDD 循环 + commit(两个 commit:backend 检查 / consumer 重摘)

---

### Task 6: 全量回归 + pyflakes(inline,控制器自跑)

- [ ] 对齐最新 origin/test(保鲜纪律)→ 全量 L1 → 失败对照预存清单 → pyflakes 改动文件零新增。

---

## Self-Review(已跑)

- **Spec 覆盖**:A4 快照一次/复用整 job/summaries→known_memories/桶线索复用(Task 1-2)✓;P5 revision 基线/pending 补返回/只全量算冲突/签名不冲突/冲突 supersede-重摘(Task 3-5)✓。验收对应 spec §6 A4/P5 两行。
- **占位符**:Task 3-5 的"执行时 trace/grep"是给实现者的定位指令,配套接口契约已给死;测试样例完整给出的是 Task 1-2(机械型),3-5 是 sonnet 档带判断的任务。
- **类型一致**:`terms_note: str = ""` 贯穿 Task 1-2;`replaced_at`/`base_identity_replaced_at` 命名贯穿 Task 3-5。
