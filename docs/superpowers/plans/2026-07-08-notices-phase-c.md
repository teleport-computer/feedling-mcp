# Phase C：四个生产者接入通知设施 — 实现计划

> **RETIRED / DO NOT DEPLOY.** Historical implementation record; managed hosted
> supervisor references describe retired source.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 genesis / history_import / memory 退避 / runner 四个子系统的失败点接线到
`notices` 通知流（失败 emit、恢复 resolve），并给 consumer 分类器补 3 类、catalog
补 `classify_upstream` + 全部生产者 error_class。让通知中心从「只有 chat」变成
「覆盖后台全场景」。

**Architecture:** 统一模式——失败点 `notices.emit(...)`、恢复点 `notices.resolve(...)`，
全部在 emit/resolve 的 never-raise 保证下不影响原流程。catalog 新增 `classify_upstream`
(backend 侧一份与 consumer `_ERROR_CLASS_RULES` 等价的正则副本，一致性测试锁死)。
producer 专属 error_class（genesis_failed 等）全部 blame=system（是我方基础设施故障，
不引导用户改 key/充值）；上游类（quota_insufficient 等）经 classify_upstream 复用 B3
既有的 user_provider 条目。

**Tech Stack:** 既有 `backend/notices/{core,catalog}.py`、`db.log_*` 流原语、pytest
（`backend_env` + `db.*` + `seed_user` + `get_store`）。C4 触及 runner 镜像。

**Spec:** `docs/superpowers/specs/2026-07-07-unified-error-surfacing-design.md`（Phase C 节）
**对外契约:** `docs/FRONTEND_ERROR_CONTRACT.md` §四「首批会出现的通知」表

## Global Constraints

- **绝不自行 `git add`/`git commit`/`git stash`**：任务完成停在 working tree，用户提交。
- **emit/resolve 已 never-raise**：producer 直接调用即可；但 producer 侧若额外做
  `get_store(user_id)`（仅 C4 需要），该构造要包在自己的 try/except 里——通知逻辑
  绝不能让原流程（tick/worker/import）崩。
- **producer error_class 一律 blame=system**（genesis_failed/genesis_partial/
  import_failed/import_stale/memory_backoff/runner_spawn_failed/
  runner_key_decrypt_failed/runner_degraded）——它们是我方后台/基础设施故障。
  blame 三分类纪律：user_text **绝不**引导用户改 key/充值（那是 user_provider 类
  经 classify_upstream 才有的指引）。
- **severity 表**：genesis_failed/import_failed/import_stale/runner_spawn_failed/
  runner_key_decrypt_failed = `error`；genesis_partial/memory_backoff/
  runner_degraded = `warning`。
- **catalog 全部 blame ∈ VALID_BLAME**：既有 `test_every_catalog_blame_is_valid`
  遍历 `catalog.ERROR_CLASSES` 自动校验新增条目，别写错 blame 值。
- **C5 的 3 类会被 `test_catalog_consumer_parity` 强制**：consumer 加类后 catalog
  不同步补齐，该测试立刻红——Task 1 里两者必须同批改。
- **部署**：C1-C3、C5 纯 backend；**C4 改 supervisor → runner 镜像**，backend 与
  runner 镜像须同批部署（spec 部署节）。
- 测试解释器：`/Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv/bin/python -m pytest <file> -q`
  在 worktree 根目录跑。工作目录：`/Users/zhengzhihao/Projects/teleport/feedling-mcp-error-contract`。
- **producer error_class → (severity, blame, user_text)** 权威表（各任务 catalog 条目照抄）：

  | error_class | severity | blame | user_text（catalog 静态值） |
  |---|---|---|---|
  | genesis_failed | error | system | 入住蒸馏没能完成，可稍后在记忆花园重试。 |
  | genesis_partial | warning | system | 入住蒸馏完成了，但有部分记忆没能导入。 |
  | import_failed | error | system | 聊天记录导入失败了，请稍后重试。 |
  | import_stale | error | system | 聊天记录导入卡住已超时，请重新发起。 |
  | memory_backoff | warning | system | 记忆整理暂时受阻，正在自动重试。 |
  | runner_spawn_failed | error | system | 你的 AI 助手进程启动失败，我们正在处理。 |
  | runner_key_decrypt_failed | error | system | 你的 AI 助手暂时无法启动（密钥读取失败），我们正在处理。 |
  | runner_degraded | warning | system | 你的 AI 助手部分能力暂时受限，正在自动恢复。 |

---

### Task 1: C5 消费者补 3 类 + catalog 同步 + `classify_upstream` + 一致性锁

**Files:**
- Modify: `tools/chat_resident_consumer.py`（`_ERROR_CLASS_RULES` 插 3 类，:391/:392 之间）
- Modify: `backend/notices/catalog.py`（补 3 类 _CATALOG 条目 + 新增 `classify_upstream`）
- Modify: `tests/test_catalog_consumer_parity.py`（扩 classify_upstream 一致性）
- Test: `tests/test_consumer_error_classify.py`（补 3 类真实错误串用例——该文件已存在于特性，追加）

**Interfaces:**
- Produces: consumer `_ERROR_CLASS_RULES` 含 11 条（8+3）；`CONSUMER_ERROR_CLASSES`
  自动纳入（推导式，无需手改）；`catalog.classify_upstream(text: str) -> str`
  （命中返 chat 上游 error_class，未命中返 `""`）。Task 2（C1）依赖 `classify_upstream`。

- [ ] **Step 1: 写失败测试（consumer 新类 + catalog 一致性）**

`tests/test_consumer_error_classify.py` 追加（先读现有文件的 import 与 helper 复用）：

```python
def test_provider_incompatible_classified():
    from chat_resident_consumer import classify_agent_error
    n = classify_agent_error(RuntimeError("400 invalid_request_error: unsupported tool 'x'"))
    assert n.error_class == "provider_incompatible" and n.blame == "user_provider"


def test_context_overflow_classified():
    from chat_resident_consumer import classify_agent_error
    n = classify_agent_error(RuntimeError("prompt is too long: 210000 tokens > maximum context length"))
    assert n.error_class == "context_overflow" and n.blame == "user_provider"


def test_content_filtered_classified():
    from chat_resident_consumer import classify_agent_error
    n = classify_agent_error(RuntimeError("response was blocked by content_filter policy"))
    assert n.error_class == "content_filtered" and n.blame == "provider_transient"
```

`tests/test_catalog_consumer_parity.py` 追加（扩 classify_upstream 与 consumer 等价）：

```python
def test_classify_upstream_mirrors_consumer_on_samples():
    """catalog.classify_upstream 是 backend 侧的 consumer 分类器正则副本；
    用代表串锁两者对同一文本给出同一 error_class（防两份正则漂移）。"""
    from notices import catalog
    from tools.chat_resident_consumer import classify_agent_error
    samples = [
        "insufficient_quota: your credit balance is too low",
        "401 invalid x-api-key",
        "429 too many requests",
        "503 overloaded, please retry",
        "400 unsupported parameter 'tool_choice'",
        "maximum context length exceeded",
        "blocked by content policy",
    ]
    for s in samples:
        assert catalog.classify_upstream(s) == classify_agent_error(RuntimeError(s)).error_class, s
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_consumer_error_classify.py tests/test_catalog_consumer_parity.py -q`
Expected: 新用例 FAIL（consumer 无 3 类正则；catalog 无 classify_upstream）。

- [ ] **Step 3: consumer 补 3 类**

`tools/chat_resident_consumer.py` 的 `_ERROR_CLASS_RULES`，在 `model_not_found` 条目
（约 :389-391）与 `rate_limited` 条目（约 :392）之间插入（**须在宽匹配的 rate/5xx 之前**）：

```python
    ("provider_incompatible", "user_provider",
     "当前模型不支持这次请求用到的能力，换个模型或到设置里调整。",
     re.compile(r"unknown variant|not supported|unsupported (parameter|tool)"
                r"|invalid_request_error.*tool", re.I)),
    ("context_overflow", "user_provider",
     "这次对话太长超出了模型上限，可精简后再试。",
     re.compile(r"context.{0,20}(length|window)|maximum context"
                r"|too many tokens|prompt is too long", re.I)),
    ("content_filtered", "provider_transient",
     "这次回复被模型的内容策略拦下了，换个说法再试。",
     re.compile(r"content_filter|content policy|safety|blocked by", re.I)),
```

（`CONSUMER_ERROR_CLASSES` 是推导式，自动纳入这 3 类，不用改。）

- [ ] **Step 4: catalog 补 3 类 + 新增 classify_upstream**

`backend/notices/catalog.py`：`ERROR_CLASSES` 加 3 类；`_CATALOG` 加 3 条（照 consumer
的 blame/user_text 一字不差——同源纪律）：

```python
    "provider_incompatible": ("user_provider",
        "当前模型不支持这次请求用到的能力，换个模型或到设置里调整。"),
    "context_overflow": ("user_provider",
        "这次对话太长超出了模型上限，可精简后再试。"),
    "content_filtered": ("provider_transient",
        "这次回复被模型的内容策略拦下了，换个说法再试。"),
```

新增 `classify_upstream`（backend 侧的正则副本，**与 consumer `_ERROR_CLASS_RULES` +
硬编码分支等价**；一致性测试锁死。次序即优先级，须与 consumer 一致）：

```python
import re

# 与 tools/chat_resident_consumer.py 的分类器等价的 backend 侧副本。
# consumer 在 tools/ 不能 import backend（单向依赖），故此处维护一份；
# tests/test_catalog_consumer_parity.py::test_classify_upstream_mirrors_consumer
# 用代表串锁两份不漂移。次序即优先级。
_UPSTREAM_RULES = (
    ("quota_insufficient", re.compile(
        r"余额|额度|insufficient_quota|credit balance|requires more credits"
        r"|payment required|\b402\b|quota", re.I)),
    ("auth_invalid", re.compile(
        r"invalid ?(x-)?api.?key|unauthorized|authentication|\b401\b", re.I)),
    ("model_not_found", re.compile(
        r"invalid model name|model_not_found|no such model", re.I)),
    ("provider_incompatible", re.compile(
        r"unknown variant|not supported|unsupported (parameter|tool)"
        r"|invalid_request_error.*tool", re.I)),
    ("context_overflow", re.compile(
        r"context.{0,20}(length|window)|maximum context|too many tokens"
        r"|prompt is too long", re.I)),
    ("content_filtered", re.compile(
        r"content_filter|content policy|safety|blocked by", re.I)),
    ("rate_limited", re.compile(r"\b429\b|too many requests|rate.?limit", re.I)),
    ("upstream_unavailable", re.compile(
        r"\b5\d{2}\b|overloaded|timed? ?out|connection (refused|reset|error)"
        r"|unreachable|stream disconnected", re.I)),
)


def classify_upstream(text: str) -> str:
    """把上游/运行时错误文本分类到 chat 上游 error_class；未命中返 ""（调用方
    决定兜底，如 genesis 落 genesis_failed）。与 consumer classify_agent_error 的
    规则表等价（不含 turn_timeout/reply_parse_failed 那两个凭异常类型/特定串判定
    的分支——那两类不会出现在 genesis/import 的错误文本里）。"""
    t = text or ""
    lowered = t.lower()
    if re.search(r"\b404\b", t) and "model" in lowered:   # 与 consumer 裸404+model 分支对齐
        return "model_not_found"
    for klass, pat in _UPSTREAM_RULES:
        if pat.search(t):
            return klass
    return ""
```

⚠️ 实现者：`classify_upstream` 里 `model_not_found` 的裸-404 分支要放在规则表遍历
**之前**（与 consumer 的 `classify_agent_error` 次序对齐），否则 test_classify_upstream_mirrors_consumer 的样本可能在边界串上不一致。3 类新正则**必须**排在 rate_limited/upstream_unavailable 之前（宽匹配会抢）。

- [ ] **Step 5: 跑测试确认通过 + 回归**

Run: `python -m pytest tests/test_consumer_error_classify.py tests/test_catalog_consumer_parity.py tests/test_chat_notice_fanout.py tests/test_notices_core.py -q`
Expected: 全绿（parity 三个原测试 + 新 classify_upstream 一致性 + consumer 3 新类 + B3 扇出不受影响）。

---

### Task 2: C1 genesis 接入（backend/genesis/service.py + worker.py）

**Files:**
- Modify: `backend/genesis/service.py`（`mark_failed` :354 内 emit；`apply_memory_outputs` :393-418 的 ValueError 丢卡处 genesis_partial）
- Modify: `backend/genesis/worker.py`（`_process_job` :1339-1418 完成态 resolve）
- Modify: `backend/notices/catalog.py`（加 genesis_failed/genesis_partial 条目）
- Test: `tests/test_genesis_notice.py`（新建）

**Interfaces:**
- Consumes: Task 1 `catalog.classify_upstream`；`notices.core.emit/resolve`；
  `catalog.blame_for/user_text_for`。
- Produces: genesis 失败/部分/恢复三条通知路径。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_genesis_notice.py`：

```python
"""genesis 失败/部分/恢复 → user_notices（spec Phase C / C1）。

Run:  python -m pytest tests/test_genesis_notice.py -q
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
from conftest import seed_user  # noqa: E402
from core.store import get_store  # noqa: E402
from genesis import service  # noqa: E402
from notices import core as notices_core  # noqa: E402


def _uid():
    return "usr_" + uuid.uuid4().hex[:12]


def _notices(uid):
    return {r["dedupe_key"]: r for r in db.log_read_all(uid, notices_core.NOTICES_STREAM)}


def test_mark_failed_emits_genesis_notice_with_upstream_class():
    uid = _uid(); seed_user(uid); store = get_store(uid)
    # 先建一个 job 行，让 mark_failed 有东西可更新（照 genesis 既有测试的建 job 方式）
    db.genesis_create_job(uid, job_id="job_ab12", kind="plaintext")  # 若签名不同，按实际改
    service.mark_failed(store, "job_ab12", "insufficient_quota: credit balance too low")
    n = _notices(uid)["genesis:job_ab12"]
    assert n["source"] == "genesis" and n["severity"] == "error"
    assert n["error_class"] == "quota_insufficient"   # classify_upstream 命中上游类
    assert n["blame"] == "user_provider"


def test_mark_failed_unmatched_falls_back_to_genesis_failed():
    uid = _uid(); seed_user(uid); store = get_store(uid)
    db.genesis_create_job(uid, job_id="job_xy", kind="plaintext")
    service.mark_failed(store, "job_xy", "worker_failed:RuntimeError:apply_outputs_failed")
    n = _notices(uid)["genesis:job_xy"]
    assert n["error_class"] == "genesis_failed" and n["blame"] == "system"
```

⚠️ 实现者：建 job 的确切 API（`db.genesis_create_job` / `db.genesis_set_job_status`）
按 genesis 既有测试（`grep -l genesis tests/`，看 test_genesis_*.py 怎么 seed job）
照搬；若 mark_failed 在 job 不存在时 `write_genesis_state` 走空、emit 仍应发（emit 只
需要 store + job_id + error，不依赖 job 行存在）。resolve 的完成态测试见 Step 4 注。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_genesis_notice.py -q`
Expected: FAIL（mark_failed 未 emit）。

- [ ] **Step 3: catalog 加 genesis 两类**

`backend/notices/catalog.py` 的 `ERROR_CLASSES` + `_CATALOG` 加（照 Global Constraints 表）：

```python
    "genesis_failed": ("system", "入住蒸馏没能完成，可稍后在记忆花园重试。"),
    "genesis_partial": ("system", "入住蒸馏完成了，但有部分记忆没能导入。"),
```

- [ ] **Step 4: service.py mark_failed 接 emit + 完成态接 resolve**

`backend/genesis/service.py::mark_failed`，在 `write_genesis_state` 之后、`return job` 之前：

```python
    ec = catalog.classify_upstream(error) or "genesis_failed"
    notices.emit(store, source="genesis", error_class=ec,
                 blame=catalog.blame_for(ec), severity="error",
                 user_text=catalog.user_text_for(ec), detail=error,
                 dedupe_key=f"genesis:{job_id}")
```

（文件头 import：`from notices import core as notices`、`from notices import catalog`。）

`apply_memory_outputs`（:393-418）的 ValueError 静默丢卡处——记一个 dropped 计数，
函数末尾若 dropped>0 且能拿到 job_id 则 emit genesis_partial（**现场决策点**：
`apply_memory_outputs` 若签名里没有 job_id，改为把 dropped 计数**返回**给调用方
`_process_job`，在那里 emit——实现者读该函数签名后二选一，在报告说明。emit 形如）：

```python
        notices.emit(store, source="genesis", error_class="genesis_partial",
                     blame="system", severity="warning",
                     user_text=catalog.user_text_for("genesis_partial"),
                     detail=f"dropped {dropped} card(s)",
                     dedupe_key=f"genesis:{job_id}:partial")
```

完成态 resolve：`backend/genesis/worker.py::_process_job`（:1339-1418）末尾 return
`status="done"` 之前（`store` 现成）：

```python
    notices.resolve(store, "genesis:")
```

（plaintext v2 的完成点 `plaintext.py:919-921` / `781-792` 同样可挂 resolve——实现者
确认那条路径是否也经过 `_process_job`；若不经过，两处都挂。report 说明。）

- [ ] **Step 5: 跑测试确认通过 + 回归**

Run: `python -m pytest tests/test_genesis_notice.py -q`
Run: `python -m pytest tests/ -q -k "genesis" --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py 2>&1 | tail -3`
Expected: 全绿（emit/resolve 不改 genesis 既有行为）。

---

### Task 3: C2 history_import 接入（backend/hosted/history_import.py）

**Files:**
- Modify: `backend/hosted/history_import.py`（顶层失败 :3205 / stale :188 / 完成 :3174；background 失败 :3167 作 warning）
- Modify: `backend/notices/catalog.py`（加 import_failed/import_stale 条目）
- Test: `tests/test_history_import_notice.py`（新建）

**Interfaces:**
- Consumes: `notices.core.emit/resolve`、`catalog.*`。
- Produces: import 失败/卡死/恢复通知。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_history_import_notice.py`（先读 history_import 既有测试确认怎么驱动
`_run_history_import_job` / `_process_history_import_sync`；若难直接驱动，退回直接调用
带 store 的内部函数并断言 user_notices 流——测试意图是锁 emit/resolve 落流，不是跑完整
导入。实现者按实际选，report 说明）：

```python
"""history_import 失败/卡死/恢复 → user_notices（spec Phase C / C2）。
Run:  python -m pytest tests/test_history_import_notice.py -q
"""
from __future__ import annotations
import sys, uuid
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
from conftest import seed_user  # noqa: E402
from core.store import get_store  # noqa: E402
from notices import core as notices_core  # noqa: E402


def _uid():
    return "usr_" + uuid.uuid4().hex[:12]


def test_top_level_failure_emits_import_failed(monkeypatch):
    uid = _uid(); seed_user(uid); store = get_store(uid)
    from hosted import history_import as hi
    # 用最小可行方式触发顶层 except：monkeypatch 内部某步抛错后调 _run_history_import_job；
    # 或直接调用被抽出的失败处理路径。实现者按 history_import 实际结构选可行触发点。
    ...
    n = {r["dedupe_key"]: r for r in db.log_read_all(uid, notices_core.NOTICES_STREAM)}
    assert any(k.startswith("history_import:") for k in n)
    row = next(v for k, v in n.items() if k.startswith("history_import:"))
    assert row["source"] == "history_import" and row["error_class"] == "import_failed"
```

⚠️ 实现者：这个测试的触发方式依赖 history_import 内部结构，先读
`_run_history_import_job`（:3187）与 `_process_history_import_sync`（:2887），选一个
能在测试里最小驱动到「顶层 except」或「stale 分支」的入口；实在难就退回「直接调用一个
接了 emit 的小函数」并断言落流。把选择写进 report。

- [ ] **Step 2: 跑测试确认失败** → `python -m pytest tests/test_history_import_notice.py -q`

- [ ] **Step 3: catalog 加两类**

```python
    "import_failed": ("system", "聊天记录导入失败了，请稍后重试。"),
    "import_stale": ("system", "聊天记录导入卡住已超时，请重新发起。"),
```

- [ ] **Step 4: 三个写入点接线**

文件头 import `from notices import core as notices`、`from notices import catalog`。

顶层失败 `history_import.py:3205-3215` 的 except，在 `_update_history_job_phase(..., "failed")`
之后：

```python
    notices.emit(store, source="history_import", error_class="import_failed",
                 blame="system", severity="error",
                 user_text=catalog.user_text_for("import_failed"),
                 detail=f"{type(e).__name__}:{str(e)[:200]}",
                 dedupe_key=f"history_import:{job_id}")
```

stale 分支 `history_import.py:188-195`，在 `_update_history_job_phase(..., "failed")`
之后（此处 `job` 有 job_id 字段，用 `job.get("job_id")`）：

```python
    notices.emit(store, source="history_import", error_class="import_stale",
                 blame="system", severity="error",
                 user_text=catalog.user_text_for("import_stale"),
                 detail="stale_history_import_job",
                 dedupe_key=f"history_import:{job.get('job_id')}")
```

完成态 `history_import.py:3174-3184`（`status="completed"` 后、return 前）：

```python
    notices.resolve(store, "history_import:")
```

（background 失败 :3167 是「chat_ready 已达成、后台补充失败」的部分失败，**不接顶层
import_failed**——它不该让用户以为整个导入失败了；本任务**不**给它单独 emit，留作后续
若需要再评估。实现者在 report 记录这个刻意的不接线。）

- [ ] **Step 5: 跑测试 + 回归**

Run: `python -m pytest tests/test_history_import_notice.py -q`
Run: `python -m pytest tests/ -q -k "history_import or history" --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py 2>&1 | tail -3`

---

### Task 4: C3 memory 退避接入（三条 lane：capture/migrate/dream）

**Files:**
- Modify: `backend/proactive/capture_scheduler.py`（capture :394-408、migrate :362-378）
- Modify: `backend/proactive/dream_scheduler.py`（dream :298-325）
- Modify: `backend/notices/catalog.py`（加 memory_backoff）
- Test: `tests/test_memory_backoff_notice.py`（新建）

**Interfaces:**
- Consumes: `notices.core.emit/resolve`、`catalog.blame_for`。
- Produces: 三条 lane 的退避 warning + 恢复。共享一个小 helper 避免三处重复。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_memory_backoff_notice.py`：

```python
"""memory 退避（streak>=3）→ user_notices warning；恢复 resolve（spec Phase C / C3）。
Run:  python -m pytest tests/test_memory_backoff_notice.py -q
"""
from __future__ import annotations
import sys, uuid
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
from conftest import seed_user  # noqa: E402
from core.store import get_store  # noqa: E402
from notices import core as notices_core  # noqa: E402
from proactive import capture_scheduler  # noqa: E402


def _uid():
    return "usr_" + uuid.uuid4().hex[:12]


def _rows(uid):
    return {r["dedupe_key"]: r for r in db.log_read_all(uid, notices_core.NOTICES_STREAM)}


def test_capture_backoff_emits_only_at_streak_3():
    uid = _uid(); seed_user(uid); store = get_store(uid)
    job = {"job_id": "j"}
    capture_scheduler.record_capture_job_status(store, job, status="failed")  # streak 1
    capture_scheduler.record_capture_job_status(store, job, status="failed")  # streak 2
    assert "memory_backoff:capture" not in _rows(uid)                          # 前两次不发
    capture_scheduler.record_capture_job_status(store, job, status="failed")  # streak 3
    n = _rows(uid)["memory_backoff:capture"]
    assert n["source"] == "memory" and n["severity"] == "warning"
    assert "capture" in n["user_text"] and "3" in n["user_text"]              # 带 lane + streak


def test_capture_completed_resolves():
    uid = _uid(); seed_user(uid); store = get_store(uid)
    job = {"job_id": "j"}
    for _ in range(3):
        capture_scheduler.record_capture_job_status(store, job, status="failed")
    capture_scheduler.record_capture_job_status(store, job, status="completed")
    assert _rows(uid)["memory_backoff:capture"]["resolved"] is True
```

⚠️ 实现者：`record_capture_job_status` 的确切签名/入参（job 需要哪些字段）先读
`capture_scheduler.py:380-408` 确认；测试里的 `job` dict 补足它读的字段。

- [ ] **Step 2: 跑测试确认失败** → `python -m pytest tests/test_memory_backoff_notice.py -q`

- [ ] **Step 3: catalog 加 memory_backoff**

```python
    "memory_backoff": ("system", "记忆整理暂时受阻，正在自动重试。"),
```

- [ ] **Step 4: 共享 helper + 三 lane 接线**

在 `backend/proactive/capture_jobs.py`（三 lane 共用的模块，已有 `failure_backoff_sec`）
加共享 helper：

```python
_BACKOFF_NOTICE_STREAK = 3   # 前两次退避噪音价值低，第 3 次才打扰用户


def notify_backoff(store, *, lane: str, status: str, streak: int) -> None:
    """三条 maintenance lane 共用的退避通知钩子。streak>=3 的失败 emit warning
    （occurrences 天然吸收后续 +1，不刷屏）；completed 恢复 resolve。绝不影响原流程。"""
    from notices import core as notices
    from notices import catalog
    if status == "completed":
        notices.resolve(store, f"memory_backoff:{lane}")
        return
    if status == "failed" and int(streak or 0) >= _BACKOFF_NOTICE_STREAK:
        notices.emit(store, source="memory", error_class="memory_backoff",
                     blame=catalog.blame_for("memory_backoff"), severity="warning",
                     user_text=f"记忆整理（{lane}）连续失败 {streak} 次，正在退避重试。",
                     detail=f"lane={lane} streak={streak}",
                     dedupe_key=f"memory_backoff:{lane}")
```

在三个 `record_*_job_status` 各自更新完 streak 后调用它——`capture_scheduler.py` 的
capture（:394-408 分支末尾）与 migrate（:362-378），`dream_scheduler.py` 的 dream
（:298-325）。以 capture 为例，在更新 `capture_fail_streak` / 归零之后：

```python
    capture_jobs.notify_backoff(store, lane="capture", status=status_text,
                                streak=int(state.get("capture_fail_streak") or 0))
```

（migrate → `lane="migrate"` 读 `migrate_fail_streak`；dream → `lane="dream"` 读
`dream_fail_streak`。completed 分支 streak 已归零，notify_backoff 里 status=="completed"
直接 resolve 不看 streak。三处都在 `db.set_blob(state)` 写回**之后**调用。）

- [ ] **Step 5: 跑测试 + 回归**

Run: `python -m pytest tests/test_memory_backoff_notice.py -q`
Run: `python -m pytest tests/ -q -k "capture or dream or proactive or maintenance" --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py 2>&1 | tail -3`

---

### Task 5: C4 runner/supervisor 接入（backend/agent_runtime/supervisor.py）

**Files:**
- Modify: `backend/agent_runtime/supervisor.py`（tick spawn 包 try/except + emit；_write_token 降级 emit；provider key 解密调用方 emit；resolve 于成功 spawn）
- Modify: `backend/agent_runtime/leases.py`（`mark_error` 首次接线——可保持签名）
- Modify: `backend/notices/catalog.py`（加 runner 三类）
- Test: `tests/test_runner_notice.py`（新建）

**Interfaces:**
- Consumes: `notices.core.emit/resolve`、`catalog.*`、`core.store.get_store`、
  `agent_runtime.leases.mark_error`。
- Produces: runner spawn 失败/密钥解不开/降级三类通知 + 恢复 + 60s 去抖。

**Interfaces（本任务内部）:**
- Produces: `supervisor` 内 `self._notice_debounce: dict[tuple[str,str], float]` +
  `self._notice_lock: threading.Lock`；`_emit_runner_notice(user_id, error_class,
  detail, *, severity="error")`（含 60s 最小写间隔 + get_store + never-raise 包裹）。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_runner_notice.py`（supervisor 是进程内直连 DB，测试可直接构造
`Supervisor` 或直接调 `_emit_runner_notice`——先读 supervisor 既有测试
`tests/test_agent_runtime_supervisor.py` 的构造方式）：

```python
"""runner spawn 失败/密钥/降级 → user_notices + 60s 去抖（spec Phase C / C4）。
Run:  python -m pytest tests/test_runner_notice.py -q
"""
from __future__ import annotations
import sys, uuid
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
from conftest import seed_user  # noqa: E402
from notices import core as notices_core  # noqa: E402


def _uid():
    return "usr_" + uuid.uuid4().hex[:12]


def _rows(uid):
    return {r["dedupe_key"]: r for r in db.log_read_all(uid, notices_core.NOTICES_STREAM)}


def _make_sup(monkeypatch):
    from agent_runtime import supervisor as sup_mod
    # 用既有测试的构造方式；spawn_fn 用一个可抛的 stub
    return sup_mod.Supervisor(spawn_fn=lambda *a: (_ for _ in ()).throw(RuntimeError("boom")),
                              alive_fn=lambda pid: False, kill_fn=lambda pid: None,
                              owner="test-owner", lease_ttl=30)


def test_spawn_failure_emits_and_debounces(monkeypatch):
    uid = _uid(); seed_user(uid)
    sup = _make_sup(monkeypatch)
    sup._emit_runner_notice(uid, "runner_spawn_failed", "boom")
    n = _rows(uid)["runner:spawn_failed"]
    assert n["source"] == "runner" and n["error_class"] == "runner_spawn_failed"
    assert n["blame"] == "system"
    # 60s 内重复不新写（occurrences 不应再涨——去抖拦在 emit 之前）
    before = n["occurrences"]
    sup._emit_runner_notice(uid, "runner_spawn_failed", "boom2")
    assert _rows(uid)["runner:spawn_failed"]["occurrences"] == before


def test_key_decrypt_blame_system(monkeypatch):
    uid = _uid(); seed_user(uid)
    sup = _make_sup(monkeypatch)
    sup._emit_runner_notice(uid, "runner_key_decrypt_failed", "decrypt fail")
    assert _rows(uid)["runner:key_decrypt_failed"]["blame"] == "system"
```

⚠️ 实现者：`Supervisor.__init__` 的真实参数按 `tests/test_agent_runtime_supervisor.py`
照搬；`_emit_runner_notice` 的 dedupe_key 用 `runner:<后缀>`（spawn_failed/
key_decrypt_failed/degraded），去抖键用 `(user_id, error_class)`。

- [ ] **Step 2: 跑测试确认失败** → `python -m pytest tests/test_runner_notice.py -q`

- [ ] **Step 3: catalog 加 runner 三类**

```python
    "runner_spawn_failed": ("system", "你的 AI 助手进程启动失败，我们正在处理。"),
    "runner_key_decrypt_failed": ("system",
        "你的 AI 助手暂时无法启动（密钥读取失败），我们正在处理。"),
    "runner_degraded": ("system", "你的 AI 助手部分能力暂时受限，正在自动恢复。"),
```

- [ ] **Step 4: supervisor 加去抖 emit helper + resolve**

`Supervisor.__init__` 加（文件头 import `import threading`（若无）、
`from notices import core as notices`、`from notices import catalog`、
`from core.store import get_store`）：

```python
        self._notice_debounce: dict[tuple, float] = {}
        self._notice_lock = threading.Lock()
        self._notice_min_interval = float(os.environ.get("RUNNER_NOTICE_MIN_INTERVAL_SEC", "60"))
```

方法：

```python
    def _emit_runner_notice(self, user_id: str, error_class: str, detail: str,
                            *, severity: str = "error") -> None:
        """runner 通知：per-(user,error_class) 60s 最小写间隔 + never-raise。
        supervisor tick ~15s 高频重试，去抖避免每 tick 写 DB（emit 的 upsert 只吸收
        occurrences，但仍是一次 DB 往返）。"""
        try:
            now = time.monotonic()
            key = (user_id, error_class)
            with self._notice_lock:
                last = self._notice_debounce.get(key, 0.0)
                if now - last < self._notice_min_interval:
                    return
                self._notice_debounce[key] = now
            store = get_store(user_id)
            suffix = {"runner_spawn_failed": "spawn_failed",
                      "runner_key_decrypt_failed": "key_decrypt_failed",
                      "runner_degraded": "degraded"}.get(error_class, error_class)
            notices.emit(store, source="runner", error_class=error_class,
                         blame=catalog.blame_for(error_class), severity=severity,
                         user_text=catalog.user_text_for(error_class),
                         detail=str(detail)[:200], dedupe_key=f"runner:{suffix}")
        except Exception:
            log.warning("runner notice emit failed (swallowed)", exc_info=True)

    def _resolve_runner_notice(self, user_id: str) -> None:
        try:
            with self._notice_lock:            # 恢复时清去抖，允许下次故障立即再报
                for k in [k for k in self._notice_debounce if k[0] == user_id]:
                    self._notice_debounce.pop(k, None)
            notices.resolve(get_store(user_id), "runner:")
        except Exception:
            log.warning("runner notice resolve failed (swallowed)", exc_info=True)
```

`tick()` 两处 `spawn_fn` 调用（:205-212 原地 respawn、:264-272 新 spawn）**各包一层
per-user try/except**（当前裸奔，异常会连坐同批——这是接 emit 前必须做的地基）：

```python
            try:
                pid = self.spawn_fn(entry, user_id, home)
            except Exception as e:  # noqa: BLE001
                leases.mark_error(user_id, self.owner, f"spawn_failed:{type(e).__name__}:{e}")
                self._emit_runner_notice(user_id, "runner_spawn_failed",
                                         f"{type(e).__name__}:{e}")
                log.exception("spawn failed for %s", user_id)
                continue   # 新 spawn 循环内：跳过该用户，不连坐同批；respawn 分支用对应的跳出
            self._write_token(user_id, home)
            leases.renew(user_id, self.owner, ttl=self.lease_ttl, pid=pid, status="running", ...)
            self._resolve_runner_notice(user_id)   # 成功 spawn → 清 runner 通知
```

（respawn 分支 :205-212 在 `with self._lock:` 内，try/except 包 spawn_fn 那一行即可，
失败时 emit + 不 renew；实现者按两处各自的控制流微调 continue/跳出。）

provider key 解密失败：调用方 `_resolve_roster`（:538）与 `_resolve_one`（:627）在
`_decrypt_provider_key(...)` 返回空串时 emit（此处有 `user_id`/`uid`，无 store，
`_emit_runner_notice` 内部 get_store）：

```python
    plain = _decrypt_provider_key(...)
    if not plain:
        self._emit_runner_notice(uid, "runner_key_decrypt_failed", "envelope decrypt returned empty")
```

（`_resolve_one` 跑在 ThreadPoolExecutor 里——`_emit_runner_notice` 的去抖字典读写已用
`self._notice_lock` 保护，线程安全。⚠️ 若这两个函数是模块级函数不是 `Supervisor` 方法，
实现者需把 supervisor 实例或一个 emit 回调传进去——读 :517/:592 的函数签名后决定接法，
report 说明。）

runtime-token 刷新失败 `_write_token`（:121-127）的 except 里加（degraded warning）：

```python
            self._emit_runner_notice(user_id, "runner_degraded",
                                     f"token refresh failed: {e}", severity="warning")
```

（**不接** identity 拉取失败——那多数是 identity 尚未建好的正常态，非故障，避免误报。）

- [ ] **Step 5: leases.mark_error 首次接线确认**

`leases.mark_error` 保持签名（`mark_error(user_id, lease_owner, error) -> None`），
本任务只是在 spawn 失败处第一次真正调用它。无需改 leases.py（除非实现者发现要 RETURNING
才能测——本任务不强制）。若 `tests/test_agent_runtime_leases.py` 已有 mark_error 的直接
单测则复用，无则本任务的 spawn 失败测试已间接覆盖。

- [ ] **Step 6: 跑测试 + 回归**

Run: `python -m pytest tests/test_runner_notice.py -q`
Run: `python -m pytest tests/ -q -k "supervisor or runtime or lease" --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py 2>&1 | tail -3`
Expected: 全绿。特别确认 supervisor 既有测试不被新 try/except 破坏（spawn 成功路径行为不变）。

---

### Task 6: Phase C 全量基线 + 契约文档对表

**Files:**
- Modify: `docs/API_ERRORS.md`（补 8 个 producer error_class + 3 个 chat 新类到 slug 表）
- Modify: `docs/CHANGELOG.md`（Phase C 条目）
- Test: 全量回归

- [ ] **Step 1: API_ERRORS.md 补表**

把 11 个新 error_class（provider_incompatible/context_overflow/content_filtered +
genesis_failed/genesis_partial/import_failed/import_stale/memory_backoff/
runner_spawn_failed/runner_key_decrypt_failed/runner_degraded）加进 slug 契约表
（notice error_class 一节，标注它们出现在 `GET /v1/notices` 的 error_class 字段而非
HTTP error slug）。若 `tests/test_api_errors_doc.py` 的 MUST_HAVE 需扩，同步。

- [ ] **Step 2: 全量基线**

Run: `python -m pytest tests/ -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py 2>&1 | tail -5`
Expected: 通过数 = Phase C 前基线 + 各任务新增，failed 不多于 5 个 pre-existing。

- [ ] **Step 3: CHANGELOG Phase C 条目**（照既有格式，注明未部署、C4 需 runner 镜像同批）

---

## Self-Review 结论（已跑）

- **Spec 覆盖**：C1→Task 2，C2→Task 3，C3→Task 4，C4→Task 5，C5→Task 1，C6 测试
  散入各任务 + Task 6 收口。✓
- **占位符**：Task 2/3/5 各有一个显式**现场决策点**（genesis_partial 挂点/history_import
  测试触发入口/supervisor provider-key 函数是否方法）——都写明了两条路与判据，非 TBD；
  其余无占位。✓
- **类型/命名一致**：`classify_upstream` 在 Task 1 定义、Task 2 消费；11 个 error_class
  在 catalog（Task 1-5 累加）与 Global Constraints 表一致；dedupe_key 前缀
  genesis:/history_import:/memory_backoff:/runner: 与 resolve 前缀匹配对齐。✓
- **超出 spec 的必要地基**（勘察发现，已并入 Task 5）：supervisor tick 两处 spawn_fn
  当前**裸奔无 try/except**，异常连坐同批——接 emit 前必须先补 per-user try/except，这
  不是「在已有 except 加一行」。这是 C4 的主要工作量，已在 Task 5 Step 4 明确。
- **勘察修正的 spec 偏差**（已并入计划）：① C3 实际是三条 lane（capture/migrate/dream，
  dream 在 dream_scheduler.py），spec 只点了一个文件 → Task 4 覆盖三条；② streak>=3 是
  「发通知」阈值不是「进退避」阈值（进退避 streak>0 即生效）→ Task 4 emit 条件用
  streak>=3，不用 in_failure_backoff；③ identity 拉取失败多为正常态，不接 runner_degraded
  → Task 5 只接 token-refresh 失败；④ history background 失败是部分失败不接顶层
  import_failed → Task 3 刻意不接线。
- **部署**：C4 改 runner 镜像，backend+runner 同批（Global Constraints 已记）。
